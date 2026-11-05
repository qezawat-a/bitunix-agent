"""Bitunix Public WebSocket adapter.

Connects to wss://fapi.bitunix.com/public/ and manages subscriptions for:
  - market_kline_Nmin / mark_kline_Nmin   (candlestick, replaces REST polling)
  - price                                  (mark/index price + funding rate)
  - ticker                                 (24hr rolling stats per symbol)
  - tickers                                (aggregated tickers, bd/ak bid/ask)
  - depth_books | depth_book1 | depth_book5 | depth_book15
  - trade                                  (recent trades)

Channel names and field mapping follow the official docs (jrock/docs_full/) and
the official demo repo (BitunixOfficial/open-api). Anything that deviates is a
bug — the exchange silently ignores unknown channel names.

Rate limit: the server allows max **5 messages/sec including PING/PONG**;
excess disconnects and repeat offenders get IP-blocked. Every send (subscribe,
unsubscribe, ping) goes through a single throttled `_send` so we never exceed
that ceiling.

Kline pushes have NO confirm/closed flag. A candle is considered closed when its
``time`` (open time) changes relative to the previous candle for the same
(symbol, interval). Switching kline intervals requires an unsubscribe first.

Auth: none for the public channel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

log = logging.getLogger(__name__)

PUBLIC_URL = "wss://fapi.bitunix.com/public/"
HEARTBEAT_INTERVAL = 3.0          # seconds between PING frames
RECONNECT_BASE = 2.0              # seconds
RECONNECT_MAX = 60.0
# Hard server ceiling: 5 messages/sec including PING/PONG. Reserve headroom.
MAX_MSG_PER_SEC = 5

# ------------------------------------------------------------------ #
# Channel names (official ground truth)                              #
# ------------------------------------------------------------------ #

# Kline channel names embed the interval: market_kline_1min, mark_kline_5min, ...
# Valid intervals per the docs:
_VALID_INTERVALS = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "60min", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h",
    "12h": "12h", "1day": "1day", "3day": "3day", "1week": "1week", "1month": "1month",
}


def kline_channel(symbol: str, interval: str, mark: bool = False) -> str:
    """Build a kline channel name: market_kline_1min / mark_kline_1min."""
    iv = _VALID_INTERVALS.get(interval, interval)
    prefix = "mark" if mark else "market"
    return f"{prefix}_kline_{iv}"


# ------------------------------------------------------------------ #
# Event types                                                        #
# ------------------------------------------------------------------ #

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
    closed: bool = False   # inferred from candle `time` changing (no confirm flag)


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
class TickersEvent:
    """Aggregated tickers across all symbols (has bd/ak bid/ask)."""
    symbol: str
    last: float
    bid: float
    ask: float
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
    side: str      # buy | sell
    ts: int        # ISO-8601 string passed through, caller may parse


@dataclass
class MarkPriceEvent:
    symbol: str
    mark_price: float
    index_price: float
    funding_rate: float
    ts: int


# Typed callback aliases
KlineCb = Callable[[KlineEvent], Any]
TickerCb = Callable[[TickerEvent], Any]
TickersCb = Callable[[TickersEvent], Any]
DepthCb = Callable[[DepthEvent], Any]
TradeCb = Callable[[TradeEvent], Any]
MarkPriceCb = Callable[[MarkPriceEvent], Any]


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ------------------------------------------------------------------ #
# Public WebSocket client                                            #
# ------------------------------------------------------------------ #

class PublicWsClient:
    """Public WebSocket client. One instance per process.

    All outbound messages (subscribe/unsubscribe/ping) share one throttled
    sender so total throughput stays at or below MAX_MSG_PER_SEC (incl. PING).
    """

    def __init__(self, url: str = PUBLIC_URL) -> None:
        self._url = url
        self._ws: Optional[Any] = None
        self._connected = False
        self._running = False
        self._ping_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._send_times: deque[float] = deque()   # sliding window of sends
        self._pending_subs: list[dict] = []        # queued while disconnected

        # Callbacks — multiple handlers per channel type allowed.
        self._kline_cbs: list[KlineCb] = []
        self._ticker_cbs: list[TickerCb] = []
        self._tickers_cbs: list[TickersCb] = []
        self._depth_cbs: list[DepthCb] = []
        self._trade_cbs: list[TradeCb] = []
        self._mark_cbs: list[MarkPriceCb] = []

        # Active subscriptions (replayed on reconnect).
        self._subs: list[dict] = []
        # Track active kline subscriptions per symbol -> interval, to enforce
        # unsubscribe-before-resubscribe when switching intervals.
        self._kline_subs: dict[str, str] = {}   # symbol -> current interval

        # Last seen candle open time per (symbol, interval) for close inference.
        self._last_candle_time: dict[tuple, int] = {}

    # ------------------------------------------------------------------ #
    # Callback registration                                               #
    # ------------------------------------------------------------------ #

    def on_kline(self, cb: KlineCb) -> None:
        self._kline_cbs.append(cb)

    def on_ticker(self, cb: TickerCb) -> None:
        self._ticker_cbs.append(cb)

    def on_tickers(self, cb: TickersCb) -> None:
        self._tickers_cbs.append(cb)

    def on_depth(self, cb: DepthCb) -> None:
        self._depth_cbs.append(cb)

    def on_trade(self, cb: TradeCb) -> None:
        self._trade_cbs.append(cb)

    def on_mark_price(self, cb: MarkPriceCb) -> None:
        self._mark_cbs.append(cb)

    # ------------------------------------------------------------------ #
    # Send throttling (5 msg/sec including PING/PONG)                    #
    # ------------------------------------------------------------------ #

    async def _throttled_send(self, msg: dict) -> None:
        """Send ``msg`` ensuring we stay at or below MAX_MSG_PER_SEC."""
        async with self._send_lock:
            now = time.monotonic()
            # Drop timestamps older than 1s.
            while self._send_times and now - self._send_times[0] >= 1.0:
                self._send_times.popleft()
            if len(self._send_times) >= MAX_MSG_PER_SEC:
                wait = 1.0 - (now - self._send_times[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                now = time.monotonic()
                while self._send_times and now - self._send_times[0] >= 1.0:
                    self._send_times.popleft()
            if self._ws and self._connected:
                try:
                    await self._ws.send(json.dumps(msg))
                    self._send_times.append(time.monotonic())
                except Exception as e:
                    log.debug("ws_public send error: %s", e)

    # ------------------------------------------------------------------ #
    # Subscription management                                             #
    # ------------------------------------------------------------------ #

    async def subscribe_kline(self, symbol: str, interval: str, mark: bool = False) -> None:
        """Subscribe to kline channel. Switching intervals unsubscribes first."""
        ch = kline_channel(symbol, interval, mark)
        # Unsubscribe previous interval for this symbol if changing.
        prev = self._kline_subs.get(symbol)
        if prev is not None and prev != interval:
            await self.unsubscribe_kline(symbol, prev)
        sub = {"symbol": symbol, "ch": ch, "interval": interval}
        self._subs.append(sub)
        self._kline_subs[symbol] = interval
        if self._connected:
            await self._throttled_send({"op": "subscribe", "args": [sub]})

    async def unsubscribe_kline(self, symbol: str, interval: str) -> None:
        ch = kline_channel(symbol, interval, mark=False)
        sub = {"symbol": symbol, "ch": ch, "interval": interval}
        if self._connected:
            await self._throttled_send({"op": "unsubscribe", "args": [sub]})
        self._kline_subs.pop(symbol, None)
        self._subs = [s for s in self._subs
                      if not (s.get("ch") == ch and s.get("symbol") == symbol)]

    async def subscribe_ticker(self, symbol: str) -> None:
        ch = {"symbol": symbol, "ch": "ticker"}
        self._subs.append(ch)
        if self._connected:
            await self._throttled_send({"op": "subscribe", "args": [ch]})

    async def subscribe_tickers(self) -> None:
        """Aggregated tickers across all symbols."""
        ch = {"ch": "tickers"}
        self._subs.append(ch)
        if self._connected:
            await self._throttled_send({"op": "subscribe", "args": [ch]})

    async def subscribe_depth(self, symbol: str, limit: str = "book1") -> None:
        ch = {"symbol": symbol, "ch": f"depth_{limit}"}
        self._subs.append(ch)
        if self._connected:
            await self._throttled_send({"op": "subscribe", "args": [ch]})

    async def subscribe_trade(self, symbol: str) -> None:
        ch = {"symbol": symbol, "ch": "trade"}
        self._subs.append(ch)
        if self._connected:
            await self._throttled_send({"op": "subscribe", "args": [ch]})

    async def subscribe_mark_price(self, symbol: str) -> None:
        """Subscribe to mark/index price channel (official name: ``price``)."""
        ch = {"symbol": symbol, "ch": "price"}
        self._subs.append(ch)
        if self._connected:
            await self._throttled_send({"op": "subscribe", "args": [ch]})

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

    async def _ping_loop(self) -> None:
        while self._running and self._connected:
            try:
                await self._throttled_send({"op": "ping", "ping": int(time.time())})
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
                        await self._throttled_send({"op": "subscribe", "args": self._subs})

                    # Start ping (counted against the 5 msg/sec budget).
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

    async def _dispatch(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
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
            if ch.startswith("market_kline_") or ch.startswith("mark_kline_"):
                interval = msg.get("interval", "")
                await self._handle_kline(symbol, interval, msg, data, ts)
            elif ch == "ticker":
                await self._handle_ticker(symbol, data, ts)
            elif ch == "tickers":
                await self._handle_tickers(data, ts)
            elif ch.startswith("depth_"):
                await self._handle_depth(symbol, data, ts)
            elif ch == "trade":
                await self._handle_trade(symbol, data, ts)
            elif ch == "price":
                await self._handle_mark(symbol, data, ts)
        except Exception as e:
            log.debug("ws_public dispatch error ch=%s: %s", ch, e)

    async def _handle_kline(self, symbol: str, interval: str, msg: dict, data: Any, ts: int) -> None:
        # kline data is a list of candles or a single dict.
        items = data if isinstance(data, list) else [data]
        for item in items:
            candle_time = int(item.get("t") or item.get("time") or ts)
            ev = KlineEvent(
                symbol=symbol,
                interval=interval,
                open=float(item.get("o") or item.get("open") or 0),
                high=float(item.get("h") or item.get("high") or 0),
                low=float(item.get("l") or item.get("low") or 0),
                close=float(item.get("c") or item.get("close") or 0),
                volume=float(item.get("v") or item.get("baseVol") or item.get("b") or 0),
                time_ms=candle_time,
                closed=False,  # will be set below via close inference
            )

            # Close inference: a candle is "closed" when its open time changes
            # relative to the last candle we saw for this (symbol, interval).
            key = (symbol, interval)
            prev_time = self._last_candle_time.get(key)
            closed = False
            if prev_time is None:
                # First candle seen — treat as closed if it's not the latest forming one.
                closed = True
            elif candle_time != prev_time:
                closed = True
            self._last_candle_time[key] = candle_time
            ev.closed = closed

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
                last=float(item.get("la") or item.get("last") or 0),
                base_vol=float(item.get("b") or item.get("baseVol") or 0),
                quote_vol=float(item.get("q") or item.get("quoteVol") or 0),
                change_pct=float(item.get("r") or item.get("change") or 0),
                ts=ts,
            )
            for cb in self._ticker_cbs:
                asyncio.create_task(self._call(cb, ev))

    async def _handle_tickers(self, data: Any, ts: int) -> None:
        items = data if isinstance(data, list) else [data]
        for item in items:
            ev = TickersEvent(
                symbol=str(item.get("symbol") or item.get("s") or ""),
                last=float(item.get("la") or item.get("last") or item.get("c") or 0),
                bid=float(item.get("bd") or item.get("bid") or 0),
                ask=float(item.get("ak") or item.get("ask") or 0),
                change_pct=float(item.get("r") or item.get("change") or 0),
                ts=ts,
            )
            for cb in self._tickers_cbs:
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
            # Push field `s` is the side (buy/sell); `t` is an ISO-8601 string.
            ev = TradeEvent(
                symbol=symbol,
                price=float(item.get("p") or item.get("price") or 0),
                qty=float(item.get("v") or item.get("qty") or 0),
                side=str(item.get("s") or item.get("side") or ""),
                ts=int(item.get("t") or ts),
            )
            for cb in self._trade_cbs:
                asyncio.create_task(self._call(cb, ev))

    async def _handle_mark(self, symbol: str, data: Any, ts: int) -> None:
        if isinstance(data, dict):
            ev = MarkPriceEvent(
                symbol=symbol,
                mark_price=float(data.get("mp") or data.get("markPrice") or 0),
                index_price=float(data.get("ip") or data.get("indexPrice") or 0),
                funding_rate=float(data.get("fr") or data.get("fundingRate") or 0),
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
