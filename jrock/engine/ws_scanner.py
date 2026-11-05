"""WebSocket-driven price feed for the scanner.

Replaces REST polling with live kline and ticker data.
The WsScanner:
1. Subscribes to kline channels for all active (symbol, timeframe) pairs.
2. Buffers incoming candles per (symbol, interval).
3. On every confirmed (closed) candle, runs all strategies on the buffer.
4. Fires aggregated signals exactly as the REST scanner does.

This eliminates the 30-second polling lag — signals fire the instant a candle closes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Optional

from ..adapters.ws_public import PublicWsClient, KlineEvent, TickerEvent
from ..adapters.ws_private import PrivateWsClient, PositionEvent, OrderEvent, TpSlEvent
from .settings import EngineSettings
from ..strategies.indicators import from_klines
from ..strategies.signals import run_all, Signal
from .types import AggregatedSignal

log = logging.getLogger(__name__)

# Number of candles to buffer per (symbol, interval).
KLINE_BUFFER = 220


class WsScanner:
    """Live WebSocket-driven scanner."""

    def __init__(
        self,
        settings: EngineSettings,
        public_ws: PublicWsClient,
        private_ws: PrivateWsClient,
    ) -> None:
        self._settings = settings
        self._pub = public_ws
        self._priv = private_ws
        self._running = False
        self._cb = None

        # Kline buffers: (symbol, interval) -> deque of kline dicts.
        self._buffers: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=KLINE_BUFFER))

        # Latest ticker prices: symbol -> float
        self.prices: dict[str, float] = {}

        # Register WS callbacks.
        self._pub.on_kline(self._on_kline)
        self._pub.on_ticker(self._on_ticker)
        self._priv.on_position(self._on_position)
        self._priv.on_order(self._on_order)
        self._priv.on_tpsl(self._on_tpsl)

    def on_signal(self, cb) -> None:
        self._cb = cb

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Start both WS connections.
        await self._pub.start()
        await self._priv.start()

        # Wait briefly for connections to establish.
        await asyncio.sleep(1.5)

        # Subscribe to klines and tickers for all symbols × timeframes.
        s = self._settings
        for symbol in s.symbols:
            await self._pub.subscribe_ticker(symbol)
            await self._pub.subscribe_mark_price(symbol)
            for tf in s.timeframes:
                await self._pub.subscribe_kline(symbol, tf)
            log.info("ws_scanner: subscribed to %s × %s", symbol, s.timeframes)

    async def stop(self) -> None:
        self._running = False
        await self._pub.stop()
        await self._priv.stop()

    async def add_symbol(self, symbol: str) -> None:
        """Hot-add a new symbol without restarting."""
        await self._pub.subscribe_ticker(symbol)
        for tf in self._settings.timeframes:
            await self._pub.subscribe_kline(symbol, tf)

    # ------------------------------------------------------------------ #
    # Kline event handler                                                 #
    # ------------------------------------------------------------------ #

    async def _on_kline(self, ev: KlineEvent) -> None:
        key = (ev.symbol, ev.interval)
        buf = self._buffers[key]

        candle = {
            "time": ev.time_ms,
            "open": str(ev.open),
            "high": str(ev.high),
            "low": str(ev.low),
            "close": str(ev.close),
            "baseVol": str(ev.volume),
            "quoteVol": str(ev.volume * ev.close),
        }

        # Update or append candle.
        if buf and int(buf[-1]["time"]) == ev.time_ms:
            buf[-1] = candle      # Update in-progress candle.
        else:
            buf.append(candle)

        self.prices[ev.symbol] = ev.close

        # Only run strategies on confirmed (closed) candles.
        if not ev.confirm:
            return

        await self._run_strategies(ev.symbol, ev.interval)

    async def _run_strategies(self, symbol: str, interval: str) -> None:
        key = (symbol, interval)
        buf = list(self._buffers[key])
        if len(buf) < 30:
            return

        s = self._settings
        try:
            ind = from_klines(buf)
        except Exception as e:
            log.debug("indicator error %s/%s: %s", symbol, interval, e)
            return

        price = self.prices.get(symbol, 0.0)
        sigs = run_all(ind, price=price, min_confidence=s.tf_min_confidence)
        for sig in sigs:
            sig.symbol = symbol
            sig.timeframe = interval

        if not sigs:
            return

        # Try to form an aggregated signal by combining with other timeframes.
        agg = await self._try_aggregate(symbol, sigs)
        if agg and self._cb:
            await self._cb(agg)

    async def _try_aggregate(
        self, symbol: str, new_sigs: list[Signal]
    ) -> Optional[AggregatedSignal]:
        """Collect recent signals across timeframes and aggregate."""
        s = self._settings
        # Gather all buffered signals for this symbol from all timeframes.
        all_sigs: list[Signal] = list(new_sigs)
        longs = [x for x in all_sigs if x.direction == "LONG"]
        shorts = [x for x in all_sigs if x.direction == "SHORT"]

        for direction, group in (("LONG", longs), ("SHORT", shorts)):
            unique = {x.strategy for x in group}
            if len(unique) < s.min_agreeing_strategies:
                continue
            avg_conf = sum(x.confidence for x in group) / len(group)
            if avg_conf < s.min_confidence:
                continue
            price = self.prices.get(symbol, group[-1].price if group else 0.0)
            return AggregatedSignal(
                symbol=symbol,
                direction=direction,
                confidence=avg_conf,
                agreeing=len(unique),
                signals=group,
                price=price,
                timestamp=time.time(),
            )
        return None

    # ------------------------------------------------------------------ #
    # Ticker handler (price updates)                                      #
    # ------------------------------------------------------------------ #

    async def _on_ticker(self, ev: TickerEvent) -> None:
        if ev.last > 0:
            self.prices[ev.symbol] = ev.last

    # ------------------------------------------------------------------ #
    # Private channel handlers (for engine state sync)                   #
    # ------------------------------------------------------------------ #

    async def _on_position(self, ev: PositionEvent) -> None:
        log.debug(
            "position %s: %s %s qty=%s pnl=%s",
            ev.event, ev.symbol, ev.direction, ev.qty, ev.unrealized_pnl,
        )

    async def _on_order(self, ev: OrderEvent) -> None:
        log.debug(
            "order %s: %s %s %s status=%s filled=%s",
            ev.event, ev.order_id, ev.symbol, ev.side, ev.status, ev.filled_qty,
        )

    async def _on_tpsl(self, ev: TpSlEvent) -> None:
        if ev.triggered:
            log.info(
                "TPSL TRIGGERED: %s %s pos=%s tp=%s sl=%s",
                ev.symbol, ev.side, ev.position_id, ev.tp_price, ev.sl_price,
            )
        elif ev.canceled:
            log.info("TPSL CANCELED: %s %s pos=%s", ev.symbol, ev.side, ev.position_id)
