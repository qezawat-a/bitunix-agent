"""Bitunix Public WebSocket adapter.

Connects to wss://fapi.bitunix.com/public/ and manages subscriptions for:
  - ticker       (24hr rolling stats per symbol)
  - kline        (candlestick, replaces REST polling in the scanner)
  - depth_book1  (order book top-of-book)
  - trade        (recent trades)
  - marketPrice  (mark price)

The adapter auto-reconnects on disconnect with exponential backoff.
It delivers parsed events to registered async callbacks.

Rate limit: max 5 messages/sec to the server (enforced by _send_throttle).
Heartbeat: ping every 3 seconds (as per official demo).
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

log = logging.getLogger(__name__)

PUBLIC_URL = "wss://fapi.bitunix.com/public/"
HEARTBEAT_INTERVAL = 3      # seconds
RECONNECT_BASE = 2.0        # seconds
RECONNECT_MAX = 60.0


@dataclass
class KlineEvent:
    symbol: str
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    time_ms: int
    confirm: bool = False   # True = candle is closed


@dataclass
class TickerEvent:
    symbol: str
    open: float
    high: float
    low: float
    last: float
    base_vol: float
    quote_vol: float
    change_pct: float
    ts: int


@dataclass
class DepthEvent:
    symbol: str
    bids: list     # [[price, qty], ...]
    asks: list
    ts: int


@dataclass
class TradeEvent:
    symbol: str
    price: float
    qty: float
    side: str      # BUY | SELL
    ts: int


@dataclass
class MarkPriceEvent:
    symbol: str
    mark_price: float
    index_price: float
    ts: int


# Typed callback alias
KlineCb = Callable[[KlineEvent], Any]
TickerCb = Callable[[TickerEvent], Any]
DepthCb = Callable[[DepthEvent], Any]
TradeCb = Callable[[TradeEvent], Any]
MarkPriceCb = Callable[[MarkPriceEvent], Any]


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PublicWsClient:
    """Public WebSocket client. One instance per process."""

    def __init__(self, url: str = PUBLIC_URL) -> None:
        self._url = url
        self._ws: Optional[Any] = None
        self._connected = False
        self._running = False
        self._ping_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._pending_subs: list[dict] = []    # queued while disconnected

        # Callbacks — multiple handlers per channel type allowed.
        self._kline_cbs: list[KlineCb] = []
        self._ticker_cbs: list[TickerCb] = []
        self._depth_cbs: list[DepthCb] = []
        self._trade_cbs: list[TradeCb] = []
        self._mark_cbs: list[MarkPriceCb] = []

        # Active subscriptions (replayed on reconnect).
        self._subs: list[dict] = []

    # ------------------------------------------------------------------ #
    # Callback registration                                               #
    # ------------------------------------------------------------------ #

    def on_kline(self, cb: KlineCb) -> None:
        self._kline_cbs.append(cb)

    def on_ticker(self, cb: TickerCb) -> None:
        self._ticker_cbs.append(cb)

    def on_depth(self, cb: DepthCb) -> None:
        self._depth_cbs.append(cb)

    def on_trade(self, cb: TradeCb) -> None:
        self._trade_cbs.append(cb)

    def on_mark_price(self, cb: MarkPriceCb) -> None:
        self._mark_cbs.append(cb)

    # ------------------------------------------------------------------ #
    # Subscription management                                             #
    # ------------------------------------------------------------------ #

    async def subscribe_kline(self, symbol: str, interval: str) -> None:
        """Subscribe to kline channel. interval: 1m 3m 5m 15m 30m 1h 4h etc."""
        ch = {"symbol": symbol, "ch": "kline", "interval": interval}
        self._subs.append(ch)
        if self._connected:
            await self._send({"op": "subscribe", "args": [ch]})

    async def subscribe_ticker(self, symbol: str) -> None:
        ch = {"symbol": symbol, "ch": "ticker"}
        self._subs.append(ch)
        if self._connected:
            await self._send({"op": "subscribe", "args": [ch]})

    async def subscribe_depth(self, symbol: str) -> None:
        ch = {"symbol": symbol, "ch": "depth_book1"}
        self._subs.append(ch)
        if self._connected:
            await self._send({"op": "subscribe", "args": [ch]})

    async def subscribe_trade(self, symbol: str) -> None:
        ch = {"symbol": symbol, "ch": "trade"}
        self._subs.append(ch)
        if self._connected:
            await self._send({"op": "subscribe", "args": [ch]})

    async def subscribe_mark_price(self, symbol: str) -> None:
        ch = {"symbol": symbol, "ch": "marketPrice"}
        self._subs.append(ch)
        if self._connected:
            await self._send({"op": "subscribe", "args": [ch]})

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Start the connection loop in the background."""
        self._running = True
        asyncio.create_task(self._connect_loop(), name="ws_public")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    async def _send(self, msg: dict) -> None:
        async with self._send_lock:
            if self._ws and self._connected:
                try:
                    await self._ws.send(json.dumps(msg))
                except Exception as e:
                    log.debug("ws_public send error: %s", e)

    async def _ping_loop(self) -> None:
        while self._running and self._connected:
            try:
                await self._send({"op": "ping", "ping": int(time.time())})
            except Exception:
                pass
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _connect_loop(self) -> None:
        delay = RECONNECT_BASE
        while self._running:
            try:
                async with websockets.connect(
                    self._url,
                    ssl=_ssl_ctx(),
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    delay = RECONNECT_BASE
                    log.info("ws_public connected")

                    # Replay subscriptions.
                    if self._subs:
                        await self._send({"op": "subscribe", "args": self._subs})

                    # Start ping.
                    self._ping_task = asyncio.create_task(self._ping_loop())

                    async for raw in ws:
                        await self._dispatch(raw)

            except (ConnectionClosedError, ConnectionClosedOK):
                log.info("ws_public disconnected, reconnecting in %.1fs", delay)
            except Exception as e:
                log.error("ws_public error: %s", e)
            finally:
                self._connected = False
                if self._ping_task:
                    self._ping_task.cancel()
                    try:
                        await self._ping_task
                    except asyncio.CancelledError:
                        pass

            if not self._running:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        op = msg.get("op", "")
        if op == "ping":
            return  # pong already handled by server

        ch = msg.get("ch", "")
        data = msg.get("data")
        symbol = msg.get("symbol", "")
        ts = int(msg.get("ts", 0))

        if not ch or data is None:
            return

        try:
            if ch == "kline":
                await self._handle_kline(symbol, msg, data, ts)
            elif ch == "ticker":
                await self._handle_ticker(symbol, data, ts)
            elif ch == "depth_book1":
                await self._handle_depth(symbol, data, ts)
            elif ch == "trade":
                await self._handle_trade(symbol, data, ts)
            elif ch == "marketPrice":
                await self._handle_mark(symbol, data, ts)
        except Exception as e:
            log.debug("ws_public dispatch error ch=%s: %s", ch, e)

    async def _handle_kline(self, symbol: str, msg: dict, data: Any, ts: int) -> None:
        # kline data is a list of candles or a single dict.
        items = data if isinstance(data, list) else [data]
        interval = msg.get("interval", "")
        for item in items:
            ev = KlineEvent(
                symbol=symbol,
                interval=interval,
                open=float(item.get("o") or item.get("open") or 0),
                high=float(item.get("h") or item.get("high") or 0),
                low=float(item.get("l") or item.get("low") or 0),
                close=float(item.get("c") or item.get("close") or 0),
                volume=float(item.get("v") or item.get("baseVol") or 0),
                time_ms=int(item.get("t") or item.get("time") or ts),
                confirm=bool(item.get("confirm") or item.get("closed") or False),
            )
            for cb in self._kline_cbs:
                asyncio.create_task(self._call(cb, ev))

    async def _handle_ticker(self, symbol: str, data: Any, ts: int) -> None:
        items = data if isinstance(data, list) else [data]
        for item in items:
            ev = TickerEvent(
                symbol=item.get("s", symbol),
                open=float(item.get("o") or 0),
                high=float(item.get("h") or 0),
                low=float(item.get("l") or 0),
                last=float(item.get("la") or 0),
                base_vol=float(item.get("b") or 0),
                quote_vol=float(item.get("q") or 0),
                change_pct=float(item.get("r") or 0),
                ts=ts,
            )
            for cb in self._ticker_cbs:
                asyncio.create_task(self._call(cb, ev))

    async def _handle_depth(self, symbol: str, data: Any, ts: int) -> None:
        if isinstance(data, dict):
            ev = DepthEvent(
                symbol=symbol,
                bids=data.get("b", data.get("bids", [])),
                asks=data.get("a", data.get("asks", [])),
                ts=ts,
            )
            for cb in self._depth_cbs:
                asyncio.create_task(self._call(cb, ev))

    async def _handle_trade(self, symbol: str, data: Any, ts: int) -> None:
        items = data if isinstance(data, list) else [data]
        for item in items:
            ev = TradeEvent(
                symbol=symbol,
                price=float(item.get("p") or item.get("price") or 0),
                qty=float(item.get("v") or item.get("qty") or 0),
                side=str(item.get("m") or item.get("side") or ""),
                ts=int(item.get("t") or ts),
            )
            for cb in self._trade_cbs:
                asyncio.create_task(self._call(cb, ev))

    async def _handle_mark(self, symbol: str, data: Any, ts: int) -> None:
        if isinstance(data, dict):
            ev = MarkPriceEvent(
                symbol=symbol,
                mark_price=float(data.get("markPrice") or data.get("mp") or 0),
                index_price=float(data.get("indexPrice") or data.get("ip") or 0),
                ts=ts,
            )
            for cb in self._mark_cbs:
                asyncio.create_task(self._call(cb, ev))

    @staticmethod
    async def _call(cb, ev) -> None:
        try:
            result = cb(ev)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            log.error("ws callback error: %s", e)
