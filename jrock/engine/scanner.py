"""Scanner: polls multiple symbols × timeframes, runs all strategies, aggregates signals.

The scanner is the "brain" of the engine. It:
1. Fetches klines for each (symbol, timeframe) pair.
2. Computes indicators.
3. Runs all enabled strategies.
4. Aggregates signals across timeframes — a trade is only suggested when
   min_agreeing_strategies strategies agree on the same direction.
5. Emits an AggregatedSignal for each qualifying opportunity.

Rate limits: Bitunix allows 10 req/s/IP for market data. We use asyncio gather
with a semaphore to stay well under that.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from jrock.adapters.bitunix import BitunixClient
from jrock.strategies.indicators import from_klines
from jrock.strategies.signals import run_all, Signal
from .types import AggregatedSignal
from .settings import EngineSettings

log = logging.getLogger(__name__)


class Scanner:
    """Async market scanner. Runs continuously while engine is active."""

    def __init__(self, client: BitunixClient, settings: EngineSettings) -> None:
        self._client = client
        self._settings = settings
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_signals: list[AggregatedSignal] = []
        self._cb = None  # callback for new signals
        self._sem = asyncio.Semaphore(5)  # max 5 concurrent kline fetches

    def on_signal(self, cb) -> None:
        """Register a callback for aggregated signals."""
        self._cb = cb

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scanner")
        log.info("scanner started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("scanner stopped")

    @property
    def last_signals(self) -> list[AggregatedSignal]:
        return list(self._last_signals)

    async def scan_once(self) -> list[AggregatedSignal]:
        """Run one scan cycle and return aggregated signals."""
        s = self._settings
        tasks = [
            self._scan_symbol_tf(sym, tf)
            for sym in s.symbols
            for tf in s.timeframes
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Group by symbol
        by_symbol: dict[str, list[Signal]] = {}
        for r in results:
            if not isinstance(r, list):
                continue
            for sig in r:
                by_symbol.setdefault(sig.symbol, []).append(sig)

        aggregated = []
        for sym, sigs in by_symbol.items():
            agg = self._aggregate(sym, sigs)
            if agg:
                aggregated.append(agg)

        self._last_signals = aggregated
        return aggregated

    async def _scan_symbol_tf(self, symbol: str, tf: str) -> list[Signal]:
        s = self._settings
        async with self._sem:
            try:
                klines = await self._client.get_kline(symbol, tf, limit=200)
            except Exception as e:
                log.debug("kline fetch failed %s/%s: %s", symbol, tf, e)
                return []

        if not klines:
            return []

        try:
            ind = from_klines(klines)
        except Exception as e:
            log.debug("indicator compute failed %s/%s: %s", symbol, tf, e)
            return []

        price = float(ind.get("close", [0])[-1]) if ind.get("close") is not None else 0.0
        sigs = run_all(ind, price=price, min_confidence=s.tf_min_confidence)
        for sig in sigs:
            sig.symbol = symbol
            sig.timeframe = tf
        return sigs

    def _aggregate(self, symbol: str, sigs: list[Signal]) -> Optional[AggregatedSignal]:
        s = self._settings
        longs = [x for x in sigs if x.direction == "LONG" and x.confidence >= s.tf_min_confidence]
        shorts = [x for x in sigs if x.direction == "SHORT" and x.confidence >= s.tf_min_confidence]

        for direction, group in (("LONG", longs), ("SHORT", shorts)):
            # Count unique strategy names agreeing
            unique_strats = {x.strategy for x in group}
            if len(unique_strats) < s.min_agreeing_strategies:
                continue

            # Weighted confidence
            total_conf = sum(x.confidence for x in group)
            avg_conf = total_conf / len(group)
            if avg_conf < s.min_confidence:
                continue

            price = group[-1].price if group else 0.0
            return AggregatedSignal(
                symbol=symbol,
                direction=direction,
                confidence=avg_conf,
                agreeing=len(unique_strats),
                signals=group,
                price=price,
            )
        return None

    async def _loop(self) -> None:
        while self._running:
            try:
                signals = await self.scan_once()
                if signals and self._cb:
                    for sig in signals:
                        try:
                            await self._cb(sig)
                        except Exception as e:
                            log.error("signal callback error: %s", e)
                log.debug("scan complete: %d signals", len(signals))
            except Exception as e:
                log.error("scanner loop error: %s", e)
            await asyncio.sleep(self._settings.scan_interval_sec)
