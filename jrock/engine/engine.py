"""Main engine: wires scanner + executor + position monitor together.

The engine has three independently switchable components:
  - scanner: market scan loop
  - executor: places trades from scanner signals
  - autotrade: enables fully autonomous trade execution

Use engine.start() to start all enabled components, engine.stop() to halt.
All state changes persist via EngineSettings.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from jrock.adapters.bitunix import BitunixClient, BitunixError
from jrock.adapters.ws_public import PublicWsClient
from jrock.adapters.ws_private import PrivateWsClient
from .scanner import Scanner
from .ws_scanner import WsScanner
from .types import AggregatedSignal
from .executor import Executor, TradeResult
from .settings import EngineSettings, STATE_DIR

log = logging.getLogger(__name__)

REPORT_FILE = STATE_DIR / "report.jsonl"


@dataclass
class EngineStatus:
    running: bool = False
    scan_running: bool = False
    autotrade: bool = False
    symbols: list[str] = field(default_factory=list)
    open_positions: int = 0
    total_pnl: float = 0.0
    equity: float = 0.0
    last_scan_ts: float = 0.0
    last_signal: str = ""
    last_trade: str = ""

    def render(self) -> str:
        s = "  ENGINE  " + ("🟢 ON" if self.running else "🔴 OFF")
        s += f"  SCANNER {'🟢' if self.scan_running else '⚪'}"
        s += f"  AUTOTRADE {'🟢' if self.autotrade else '⚪'}\n"
        s += f"  Symbols: {', '.join(self.symbols)}\n"
        s += f"  Open positions: {self.open_positions}  Equity: {self.equity:.2f}  PnL: {self.total_pnl:.2f}\n"
        if self.last_signal:
            s += f"  Last signal: {self.last_signal}\n"
        if self.last_trade:
            s += f"  Last trade: {self.last_trade}\n"
        return s


class Engine:
    """Central engine: scanner + executor + status reporting."""

    def __init__(
        self,
        client: BitunixClient,
        settings: EngineSettings,
        confirm_fn: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> None:
        self._client = client
        self._settings = settings
        # REST-based scanner (fallback / one-shot scan)
        self._scanner = Scanner(client, settings)
        # WebSocket-based scanner (live feed, primary)
        self._pub_ws = PublicWsClient()
        self._priv_ws = PrivateWsClient()
        self._ws_scanner = WsScanner(settings, self._pub_ws, self._priv_ws)
        self._executor = Executor(client, settings, confirm_fn)
        self._status = EngineStatus(symbols=list(settings.symbols))
        self._running = False
        self._report_task: Optional[asyncio.Task] = None
        self._trailing_task: Optional[asyncio.Task] = None
        self._signal_log: list[dict] = []
        self._trades: list[dict] = []
        # Track which positions have already been moved to breakeven, so we
        # don't repeatedly modify the SL on every trailing tick.
        self._breakeven_done: set[str] = set()
        # Wire signal callbacks
        self._scanner.on_signal(self._on_signal)
        self._ws_scanner.on_signal(self._on_signal)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> str:
        """Start scanner (WebSocket live feed) and autotrade if enabled."""
        if self._running:
            return "Engine already running."
        self._running = True
        self._status.running = True
        if self._settings.scan_enabled:
            # Start WS scanner (live kline feed, primary).
            await self._ws_scanner.start()
            self._status.scan_running = True
        if self._settings.autotrade_enabled:
            self._status.autotrade = True
        if self._settings.report_enabled:
            self._report_task = asyncio.create_task(self._report_loop(), name="reporter")
        # Automatic trailing / breakeven monitor for open positions.
        self._trailing_task = asyncio.create_task(self._trailing_loop(), name="trailing")
        log.info("engine started")
        return "Engine ON. Scanner %s (WebSocket live feed). Autotrade %s." % (
            "ON" if self._settings.scan_enabled else "OFF",
            "ON" if self._settings.autotrade_enabled else "OFF",
        )

    async def stop(self) -> str:
        """Stop all engine components."""
        await self._ws_scanner.stop()
        await self._scanner.stop()
        self._status.running = False
        self._status.scan_running = False
        self._status.autotrade = False
        self._running = False
        if self._report_task:
            self._report_task.cancel()
            try:
                await self._report_task
            except asyncio.CancelledError:
                pass
        if self._trailing_task:
            self._trailing_task.cancel()
            try:
                await self._trailing_task
            except asyncio.CancelledError:
                pass
        self._breakeven_done.clear()
        log.info("engine stopped")
        return "Engine OFF."

    async def scan_on(self) -> str:
        self._settings.scan_enabled = True
        self._settings.save()
        if self._running:
            await self._ws_scanner.start()
            self._status.scan_running = True
        return "Scanner ON (WebSocket live feed)."

    async def scan_off(self) -> str:
        self._settings.scan_enabled = False
        self._settings.save()
        await self._ws_scanner.stop()
        self._status.scan_running = False
        return "Scanner OFF."

    async def autotrade_on(self) -> str:
        self._settings.autotrade_enabled = True
        self._settings.save()
        self._status.autotrade = True
        if not self._running:
            return await self.start()
        return "Autotrade ON."

    async def autotrade_off(self) -> str:
        self._settings.autotrade_enabled = False
        self._settings.save()
        self._status.autotrade = False
        return "Autotrade OFF."

    # ------------------------------------------------------------------ #
    # Status & reporting                                                  #
    # ------------------------------------------------------------------ #

    async def status(self) -> EngineStatus:
        try:
            account = await self._client.get_account()
            self._status.equity = float(account.get("equity") or account.get("balance") or 0)
        except Exception:
            pass
        try:
            positions = await self._client.get_pending_positions()
            self._status.open_positions = len(positions)
            pnl = sum(float(p.get("unrealizedPnl") or p.get("pnl") or 0) for p in positions)
            self._status.total_pnl = pnl
        except Exception:
            pass
        self._status.symbols = list(self._settings.symbols)
        self._status.autotrade = self._settings.autotrade_enabled
        self._status.scan_running = self._scanner._running
        return self._status

    async def get_positions(self) -> list[dict]:
        return await self._client.get_pending_positions()

    async def get_history(self, limit: int = 50) -> list[dict]:
        return await self._client.get_history_positions(limit=limit)

    async def get_orders(self, symbol: Optional[str] = None) -> list[dict]:
        return await self._client.get_history_orders(symbol, limit=50)

    async def get_tpsl(self, symbol: Optional[str] = None) -> list[dict]:
        return await self._client.get_history_tpsl_orders(symbol, limit=50)

    async def pnl_report(self) -> dict:
        try:
            account = await self._client.get_account()
            positions = await self._client.get_pending_positions()
            pnl = sum(float(p.get("unrealizedPnl") or p.get("pnl") or 0) for p in positions)
            return {
                "equity": float(account.get("equity") or 0),
                "available": float(account.get("available") or 0),
                "unrealized_pnl": pnl,
                "open_positions": len(positions),
            }
        except Exception as e:
            return {"error": str(e)}

    async def close_position(self, symbol: str, position_id: str = "") -> dict:
        return await self._executor.close_position(symbol, position_id)

    async def update_settings(self, **kwargs) -> list[str]:
        changed = self._settings.update(**kwargs)
        # Propagate live changes to sub-components.
        self._executor._risk.max_open_positions = self._settings.max_open_positions
        self._executor._risk.max_daily_loss_pct = self._settings.max_daily_loss_pct
        return changed

    async def trigger_mid_management(self, symbol: str) -> str:
        """Manually trigger position mid-management: adjust SL to breakeven if profitable."""
        positions = await self._client.get_pending_positions(symbol)
        if not positions:
            return f"No open positions for {symbol}."
        msgs = []
        for pos in positions:
            pid = str(pos.get("positionId", ""))
            entry = float(pos.get("avgOpenPrice") or pos.get("entryPrice") or 0)
            pnl_pct = float(pos.get("roe") or pos.get("pnlPct") or 0)
            side = str(pos.get("side", ""))
            if not pid or not entry:
                continue
            if abs(pnl_pct) >= self._settings.breakeven_threshold_pct:
                # Move SL to breakeven.
                be_price = entry
                try:
                    await self._client.modify_position_tpsl_order(
                        position_id=pid,
                        symbol=symbol,
                        sl_price=str(round(be_price, 4)),
                        sl_stop_type="MARK",
                    )
                    msgs.append(f"  {symbol} {side} pos {pid}: SL moved to breakeven {be_price:.4f}")
                except Exception as e:
                    msgs.append(f"  {symbol} pos {pid}: mid-mgmt failed: {e}")
        return "\n".join(msgs) or f"No positions qualify for mid-management on {symbol}."

    async def scan_once(self) -> list[AggregatedSignal]:
        return await self._scanner.scan_once()

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    async def _on_signal(self, signal: AggregatedSignal) -> None:
        self._status.last_signal = repr(signal)
        self._status.last_scan_ts = time.time()
        self._signal_log.append({
            "ts": time.time(), "symbol": signal.symbol,
            "direction": signal.direction, "confidence": signal.confidence,
            "strategies": signal.strategy_names,
        })
        if len(self._signal_log) > 500:
            self._signal_log = self._signal_log[-500:]

        if not self._settings.autotrade_enabled:
            return

        result = await self._executor.execute_signal(signal)
        self._status.last_trade = repr(result)
        self._trades.append({
            "ts": time.time(), "symbol": result.symbol,
            "direction": result.direction, "success": result.success,
            "qty": result.qty, "entry": result.entry_price,
            "error": result.error,
        })

    async def _report_loop(self) -> None:
        while True:
            try:
                s = await self.status()
                entry = {
                    "ts": time.time(),
                    "equity": s.equity,
                    "pnl": s.total_pnl,
                    "positions": s.open_positions,
                    "last_signal": s.last_signal,
                }
                REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
                with REPORT_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception:
                pass
            await asyncio.sleep(self._settings.report_interval_sec)
    # ------------------------------------------------------------------ #
    # Automatic trailing / breakeven monitor                              #
    # ------------------------------------------------------------------ #

    async def _trailing_loop(self) -> None:
        """Background monitor: moves SL to breakeven at breakeven_threshold_pct
        ROI and trails the stop at trailing_stop_roi_pct for open positions.

        Runs once per report interval. Only acts when autotrade is ON so the
        operator remains in control of live risk adjustments.
        """
        while True:
            try:
                if not self._settings.autotrade_enabled:
                    await asyncio.sleep(self._settings.report_interval_sec)
                    continue

                positions = await self._client.get_pending_positions()
                if not positions:
                    await asyncio.sleep(self._settings.report_interval_sec)
                    continue

                be_threshold = self._settings.breakeven_threshold_pct
                trail_roi = self._settings.trailing_stop_roi_pct

                for pos in positions:
                    pid = str(pos.get("positionId", ""))
                    symbol = pos.get("symbol", "")
                    side = str(pos.get("side", "")).upper()
                    entry = float(pos.get("avgOpenPrice") or pos.get("entryPrice") or 0)
                    pnl_pct = float(pos.get("roe") or pos.get("pnlPct") or 0)
                    if not pid or not symbol or not entry:
                        continue

                    is_long = side in ("BUY", "LONG")

                    # (a) Breakeven: once ROI >= threshold, move SL to entry price.
                    if abs(pnl_pct) >= be_threshold and pid not in self._breakeven_done:
                        try:
                            await self._client.modify_position_tpsl_order(
                                position_id=pid,
                                symbol=symbol,
                                sl_price=str(round(entry, 4)),
                                sl_stop_type="LAST_PRICE",
                            )
                            self._breakeven_done.add(pid)
                            log.info(
                                "trailing: %s %s pos %s SL -> breakeven %.4f (ROI %.2f%%)",
                                symbol, side, pid, entry, pnl_pct,
                            )
                        except BitunixError as e:
                            log.warning(
                                "trailing: breakeven failed for %s pos %s: %s",
                                symbol, pid, e,
                            )
                        continue

                    # (b) Trailing: once ROI >= trail_roi, trail the stop.
                    if abs(pnl_pct) >= trail_roi:
                        try:
                            tickers = await self._client.get_tickers(symbol)
                            ticker = tickers[0] if isinstance(tickers, list) and tickers else tickers
                            cur = float(ticker.get("lastPrice") or ticker.get("last") or entry)
                        except Exception:
                            cur = entry

                        if is_long:
                            sl_price = cur * (1 - trail_roi / 100.0)
                            existing_sl = float(pos.get("stopPrice") or pos.get("slPrice") or 0)
                            if existing_sl and sl_price <= existing_sl:
                                continue
                        else:
                            sl_price = cur * (1 + trail_roi / 100.0)
                            existing_sl = float(pos.get("stopPrice") or pos.get("slPrice") or 0)
                            if existing_sl and sl_price >= existing_sl:
                                continue

                        try:
                            await self._client.modify_position_tpsl_order(
                                position_id=pid,
                                symbol=symbol,
                                sl_price=str(round(sl_price, 4)),
                                sl_stop_type="LAST_PRICE",
                            )
                            log.info(
                                "trailing: %s %s pos %s SL -> %.4f (ROI %.2f%%)",
                                symbol, side, pid, sl_price, pnl_pct,
                            )
                        except BitunixError as e:
                            log.warning("trailing: trail failed for %s pos %s: %s", symbol, pid, e)

            except Exception as e:
                log.error("trailing loop error: %s", e)

            await asyncio.sleep(self._settings.report_interval_sec)

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #
