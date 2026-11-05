"""Bitunix Private WebSocket adapter.

Connects to wss://fapi.bitunix.com/private/ and delivers real-time updates for:
  - balance   (wallet changes)
  - position  (position open/close/update)
  - order     (order fills, status changes)
  - tpsl      (TP/SL trigger events)

Authentication (WS-specific signing — simpler than REST):
    digest = sha256(nonce + timestamp + api_key)
    sign   = sha256(digest + secret_key)
    login payload: {"apiKey": key, "timestamp": int(ts), "nonce": nonce, "sign": sign}

The private channel is pushed automatically after login — no explicit subscribe
needed for balance/position/order/tpsl.

The adapter fires typed callbacks and also updates an in-memory position cache
that the engine can read without a REST round-trip.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import ssl
import string
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

log = logging.getLogger(__name__)

PRIVATE_URL = "wss://fapi.bitunix.com/private/"
HEARTBEAT_INTERVAL = 3
RECONNECT_BASE = 2.0
RECONNECT_MAX = 60.0


# ------------------------------------------------------------------ #
# Event types                                                         #
# ------------------------------------------------------------------ #

@dataclass
class BalanceEvent:
    coin: str
    available: float
    frozen: float
    margin: float
    isolation_margin: float
    cross_margin: float
    raw: dict = field(default_factory=dict)


@dataclass
class PositionEvent:
    event: str          # CREATE | UPDATE | CLOSE
    position_id: str
    symbol: str
    side: str           # BUY | SELL (LONG | SHORT in hedge mode)
    leverage: int
    qty: float
    entry_value: float  # notional
    margin: float
    unrealized_pnl: float
    realized_pnl: float
    margin_mode: str
    position_mode: str
    raw: dict = field(default_factory=dict)

    @property
    def direction(self) -> str:
        return "LONG" if self.side in ("BUY", "LONG") else "SHORT"


@dataclass
class OrderEvent:
    event: str
    order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    price: float
    qty: float
    filled_qty: float
    avg_price: float
    trade_side: str
    raw: dict = field(default_factory=dict)


@dataclass
class TpSlEvent:
    event: str          # CREATE | UPDATE | CLOSE
    order_id: str
    position_id: str
    symbol: str
    side: str
    status: str         # NEW | CANCELED | SYSTEM_CANCELED | FILLED | FAILED
    tp_price: float
    sl_price: float
    tp_qty: float
    sl_qty: float
    raw: dict = field(default_factory=dict)

    @property
    def triggered(self) -> bool:
        return self.event == "CLOSE" and self.status == "FILLED"

    @property
    def canceled(self) -> bool:
        return self.event == "CLOSE" and self.status in ("CANCELED", "SYSTEM_CANCELED")


BalanceCb = Callable[[BalanceEvent], Any]
PositionCb = Callable[[PositionEvent], Any]
OrderCb = Callable[[OrderEvent], Any]
TpSlCb = Callable[[TpSlEvent], Any]


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _nonce() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=32))


def _ws_auth(api_key: str, secret_key: str) -> dict:
    """Generate WS login payload (different from REST — no body/query_str)."""
    nonce = _nonce()
    timestamp = str(int(time.time()))
    digest = hashlib.sha256((nonce + timestamp + api_key).encode()).hexdigest()
    sign = hashlib.sha256((digest + secret_key).encode()).hexdigest()
    return {
        "apiKey": api_key,
        "timestamp": int(timestamp),
        "nonce": nonce,
        "sign": sign,
    }


class PrivateWsClient:
    """Private WebSocket client. Handles auth, heartbeat, and all private channels."""

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        url: str = PRIVATE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("BITUNIX_API_KEY", "")
        self._secret_key = secret_key or os.environ.get("BITUNIX_SECRET_KEY", "")
        self._url = url
        self._ws: Optional[Any] = None
        self._connected = False
        self._authenticated = False
        self._running = False
        self._ping_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()

        # Callbacks.
        self._balance_cbs: list[BalanceCb] = []
        self._position_cbs: list[PositionCb] = []
        self._order_cbs: list[OrderCb] = []
        self._tpsl_cbs: list[TpSlCb] = []

        # Live position cache: position_id -> PositionEvent
        self.positions: dict[str, PositionEvent] = {}

        # Live balance cache: coin -> BalanceEvent
        self.balances: dict[str, BalanceEvent] = {}

    # ------------------------------------------------------------------ #
    # Callback registration                                               #
    # ------------------------------------------------------------------ #

    def on_balance(self, cb: BalanceCb) -> None:
        self._balance_cbs.append(cb)

    def on_position(self, cb: PositionCb) -> None:
        self._position_cbs.append(cb)

    def on_order(self, cb: OrderCb) -> None:
        self._order_cbs.append(cb)

    def on_tpsl(self, cb: TpSlCb) -> None:
        self._tpsl_cbs.append(cb)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._connect_loop(), name="ws_private")

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
                    log.debug("ws_private send error: %s", e)

    async def _authenticate(self) -> None:
        auth = _ws_auth(self._api_key, self._secret_key)
        await self._send({"op": "login", "args": [auth]})
        log.info("ws_private: sent login")

    async def _subscribe_all(self) -> None:
        """Subscribe to all private channels — pushed automatically after login."""
        await self._send({
            "op": "subscribe",
            "args": [
                {"ch": "balance"},
                {"ch": "position"},
                {"ch": "order"},
                {"ch": "tpsl"},
            ],
        })

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
                    self._authenticated = False
                    delay = RECONNECT_BASE
                    log.info("ws_private connected")

                    await self._authenticate()
                    self._ping_task = asyncio.create_task(self._ping_loop())

                    async for raw in ws:
                        await self._dispatch(raw)

            except (ConnectionClosedError, ConnectionClosedOK):
                log.info("ws_private disconnected, reconnecting in %.1fs", delay)
            except Exception as e:
                log.error("ws_private error: %s", e)
            finally:
                self._connected = False
                self._authenticated = False
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

        # Login confirmation.
        if op == "login":
            code = msg.get("code", 0)
            if str(code) == "0":
                self._authenticated = True
                log.info("ws_private authenticated")
                await self._subscribe_all()
            else:
                log.error("ws_private auth failed: %s", msg)
            return

        if op == "ping":
            return

        ch = msg.get("ch", "")
        data = msg.get("data")
        if not ch or data is None:
            return

        try:
            if ch == "balance":
                await self._handle_balance(data)
            elif ch == "position":
                await self._handle_position(data)
            elif ch == "order":
                await self._handle_order(data)
            elif ch == "tpsl":
                await self._handle_tpsl(data)
        except Exception as e:
            log.debug("ws_private dispatch error ch=%s: %s", ch, e)

    async def _handle_balance(self, data: Any) -> None:
        items = data if isinstance(data, list) else [data]
        for item in items:
            ev = BalanceEvent(
                coin=item.get("coin", ""),
                available=float(item.get("available") or 0),
                frozen=float(item.get("frozen") or 0),
                margin=float(item.get("margin") or 0),
                isolation_margin=float(item.get("isolationMargin") or 0),
                cross_margin=float(item.get("crossMargin") or 0),
                raw=item,
            )
            self.balances[ev.coin] = ev
            for cb in self._balance_cbs:
                asyncio.create_task(self._call(cb, ev))

    async def _handle_position(self, data: Any) -> None:
        item = data if isinstance(data, dict) else (data[0] if data else {})
        ev = PositionEvent(
            event=item.get("event", ""),
            position_id=str(item.get("positionId", "")),
            symbol=item.get("symbol", ""),
            side=item.get("side", ""),
            leverage=int(item.get("leverage") or 0),
            qty=float(item.get("qty") or 0),
            entry_value=float(item.get("entryValue") or 0),
            margin=float(item.get("margin") or 0),
            unrealized_pnl=float(item.get("unrealizedPNL") or 0),
            realized_pnl=float(item.get("realizedPNL") or 0),
            margin_mode=item.get("marginMode", ""),
            position_mode=item.get("positionMode", ""),
            raw=item,
        )
        if ev.event == "CLOSE":
            self.positions.pop(ev.position_id, None)
        else:
            self.positions[ev.position_id] = ev
        for cb in self._position_cbs:
            asyncio.create_task(self._call(cb, ev))

    async def _handle_order(self, data: Any) -> None:
        item = data if isinstance(data, dict) else (data[0] if data else {})
        ev = OrderEvent(
            event=item.get("event", ""),
            order_id=str(item.get("orderId", "")),
            symbol=item.get("symbol", ""),
            side=item.get("side", ""),
            order_type=item.get("type", ""),
            status=item.get("status", ""),
            price=float(item.get("price") or 0),
            qty=float(item.get("qty") or 0),
            filled_qty=float(item.get("filledQty") or item.get("dealQty") or 0),
            avg_price=float(item.get("avgPrice") or item.get("dealAvgPrice") or 0),
            trade_side=item.get("tradeSide", ""),
            raw=item,
        )
        for cb in self._order_cbs:
            asyncio.create_task(self._call(cb, ev))

    async def _handle_tpsl(self, data: Any) -> None:
        item = data if isinstance(data, dict) else (data[0] if data else {})
        ev = TpSlEvent(
            event=item.get("event", ""),
            order_id=str(item.get("orderId", "")),
            position_id=str(item.get("positionId", "")),
            symbol=item.get("symbol", ""),
            side=item.get("side", ""),
            status=item.get("status", ""),
            tp_price=float(item.get("tpPrice") or 0),
            sl_price=float(item.get("slPrice") or 0),
            tp_qty=float(item.get("tpQty") or 0),
            sl_qty=float(item.get("slQty") or 0),
            raw=item,
        )
        for cb in self._tpsl_cbs:
            asyncio.create_task(self._call(cb, ev))

    @staticmethod
    async def _call(cb, ev) -> None:
        try:
            result = cb(ev)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            log.error("ws_private callback error: %s", e)
