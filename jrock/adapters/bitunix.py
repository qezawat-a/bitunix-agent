"""Bitunix Futures REST adapter.

Endpoint paths, signing, and field names follow the official docs
(jrock/docs_full/) and the official demo repo (BitunixOfficial/open-api).
Anything that deviates from those two sources is a bug: the exchange returns
HTTP 200 with a non-zero ``code`` for most failures, and a wrong field name is
silently ignored rather than rejected — which for order placement means an
order without its protective stop.

Signing (double SHA-256), per /futures/common/sign:
    digest = sha256(nonce + timestamp_ms + api_key + query_str + body_str)
    sign   = sha256(digest + secret_key)

Query string for signing is the concatenation of key+value with NO separators,
sorted by key. Body is compact JSON with no whitespace. ``timestamp`` is in
milliseconds and must be within 60s of server time; ``nonce`` must be < 64 chars.

Rate limits (from the official docs) are tracked per-endpoint-class in
``RateLimiter`` so the client stays under the 5 req/sec ceiling the spec
mandates even when several engine tasks fire at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

FUTURES_BASE = "https://fapi.bitunix.com"
FUTURES_PATH = "/api/v1/futures"
SPOT_BASE = "https://openapi.bitunix.com"
SPOT_PATH = "/api/spot/v1"
# Copy-trading endpoints live OUTSIDE the /api/v1/futures prefix.
COPY_PATH = "/api/v1/cp"

# ------------------------------------------------------------------ #
# Error codes (official ErrorCode table)                              #
# ------------------------------------------------------------------ #
# The exchange answers HTTP 200 with `code != 0` for essentially every
# failure, so these are checked on every response rather than inferred from
# the HTTP status.
ERR_SUCCESS = 0
ERR_NETWORK = 10001
ERR_PARAM = 10002
ERR_IP_NOT_WHITELISTED = 10004
ERR_RATE_LIMIT = 10005
ERR_REQUEST_TOO_FREQUENT = 10006
ERR_SIGNATURE = 10007
ERR_ORDER_IN_PROGRESS = 10009
ERR_NONCE_TOO_LONG = 10010
ERR_CLOCK_SKEW = 10011
ERR_MARKET_NOT_EXISTS = 20001
ERR_MAX_POSITIONS = 20002
ERR_INSUFFICIENT_BALANCE = 20003
ERR_INVALID_LEVERAGE = 20005
# 20006: leverage/margin cannot change while orders/positions are open.
ERR_OPEN_ORDERS = 20006
ERR_INSUFFICIENT_AMOUNT = 20008
ERR_POSITION_MODE_LOCKED = 20009
ERR_ACCOUNT_DELETED = 20014
ERR_ORDER_IMMEDIATE_LIQ = 30001
ERR_PRICE_BELOW_LIQ = 30002
ERR_PRICE_ABOVE_LIQ = 30003
ERR_POSITION_NOT_EXIST = 30004
ERR_TRIGGER_TOO_CLOSE = 30005
ERR_TPSL_NONE_SELECTED = 30006
ERR_TP_ABOVE_ENTRY = 30007
ERR_TP_BELOW_ENTRY = 30008
ERR_SL_BELOW_ENTRY = 30009
ERR_SL_ABOVE_ENTRY = 30010
ERR_QTY_TOO_SMALL = 30016
ERR_QTY_BELOW_MIN = 30017
ERR_MAX_ORDER_QTY = 30013
ERR_SL_LT_LIQ = 30024
ERR_SL_GT_LIQ = 30025
ERR_TPSL_QTY_EXCEEDS_POS = 30038

# Human-readable remediation for the codes an operator is most likely to see.
ERROR_HINTS: dict[int, str] = {
    ERR_IP_NOT_WHITELISTED: "API key is IP-restricted — add this host's IP in Bitunix API management.",
    ERR_RATE_LIMIT: "Rate limited by the exchange — slow the loop down.",
    ERR_REQUEST_TOO_FREQUENT: "Too many requests — back off.",
    ERR_SIGNATURE: "Signature rejected — check API key/secret and that the body matches what was signed.",
    ERR_NONCE_TOO_LONG: "Nonce must be shorter than 64 characters.",
    ERR_CLOCK_SKEW: "Local clock is more than 60s off server time — fix NTP.",
    ERR_MAX_POSITIONS: "Position/count limit on the exchange — reduce size or open positions.",
    ERR_INSUFFICIENT_BALANCE: "Insufficient balance for this order.",
    ERR_INVALID_LEVERAGE: "Leverage rejected for this symbol — check maxLeverage from trading_pairs.",
    ERR_OPEN_ORDERS: "Cannot change leverage/margin mode while positions or orders are open.",
    ERR_POSITION_MODE_LOCKED: "Position mode cannot change while positions exist.",
    ERR_ORDER_IMMEDIATE_LIQ: "Order price would liquidate immediately — adjust price or leverage.",
    ERR_PRICE_BELOW_LIQ: "Price is below the liquidation price.",
    ERR_PRICE_ABOVE_LIQ: "Price is above the liquidation price.",
    ERR_TRIGGER_TOO_CLOSE: "TP/SL trigger is too close to current price and may fire immediately.",
    ERR_SL_LT_LIQ: "Stop loss must sit at/above the liquidation price (LONG).",
    ERR_SL_GT_LIQ: "Stop loss must sit at/below the liquidation price (SHORT).",
    ERR_TPSL_QTY_EXCEEDS_POS: "TP/SL quantity exceeds the position size.",
}

# Per-endpoint-class rate limits from the docs (requests per second).
RATE_LIMITS: dict[str, int] = {
    "market": 10,     # /market/* — 10 req/sec/ip
    "trade": 20,      # place/modify/cancel — 20 req/sec/uid
    "tpsl": 20,       # TP/SL place/modify — 20 req/sec/uid
    "account": 10,    # account reads — 10 req/sec/uid
    "position": 10,   # position reads — 10 req/sec/uid
    "default": 5,     # spec's hard ceiling for unclassified calls
}


class BitunixError(RuntimeError):
    """An exchange-side rejection, carrying the exchange error code."""

    def __init__(self, code: int, msg: str, *, path: str = ""):
        hint = ERROR_HINTS.get(code, "")
        text = f"Bitunix error {code}: {msg}"
        if path:
            text += f" ({path})"
        if hint:
            text += f" — {hint}"
        super().__init__(text)
        self.code = code
        self.msg = msg
        self.path = path

    @property
    def retryable(self) -> bool:
        """True when retrying the identical request could reasonably succeed."""
        return self.code in (
            ERR_NETWORK, ERR_RATE_LIMIT, ERR_REQUEST_TOO_FREQUENT,
            ERR_ORDER_IN_PROGRESS,
        )


class RateLimiter:
    """Sliding-window limiter keeping a request class under N requests/second."""

    def __init__(self, per_second: int) -> None:
        self._per_second = max(1, per_second)
        self._window: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._window and now - self._window[0] >= 1.0:
                self._window.popleft()
            if len(self._window) >= self._per_second:
                # Sleep just past the oldest request's 1-second boundary.
                wait = 1.0 - (now - self._window[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                now = time.monotonic()
                while self._window and now - self._window[0] >= 1.0:
                    self._window.popleft()
            self._window.append(time.monotonic())


class BitunixClient:
    """Async Bitunix Futures client. One instance per process is fine."""

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        *,
        base_url: str = FUTURES_BASE,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("BITUNIX_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("BITUNIX_SECRET_KEY", "")
        self.base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Content-Type": "application/json", "language": "en-US"},
        )
        self._limiters: dict[str, RateLimiter] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def _limiter(self, path: str) -> RateLimiter:
        if path.startswith("/market") or "/market/" in path:
            cls = "market"
        elif "/cp/" in path:
            cls = "account"
        elif "/trade/" in path:
            cls = "trade"
        elif "/tpsl/" in path:
            cls = "tpsl"
        elif "/account" in path:
            cls = "account"
        elif "/position" in path:
            cls = "position"
        else:
            cls = "default"
        if cls not in self._limiters:
            self._limiters[cls] = RateLimiter(RATE_LIMITS[cls])
        return self._limiters[cls]

    # ------------------------------------------------------------------ #
    # Signing                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _query_string(params: dict) -> str:
        """Sorted key+value concatenation with no separators — signing format."""
        return "".join(f"{k}{v}" for k, v in sorted(params.items()))

    def _headers(self, query_str: str = "", body_str: str = "") -> dict:
        # 32 hex chars = 32 bytes of entropy, well under the 64-char cap.
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))  # milliseconds
        digest = hashlib.sha256(
            (nonce + timestamp + self.api_key + query_str + body_str).encode("utf-8")
        ).hexdigest()
        sign = hashlib.sha256((digest + self.secret_key).encode("utf-8")).hexdigest()
        return {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
        }

    # ------------------------------------------------------------------ #
    # Transport                                                           #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        public: bool = False,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        body_str = ""
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"))

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            await self._limiter(path).acquire()
            headers = {} if public else self._headers(self._query_string(params), body_str)
            try:
                if method == "GET":
                    resp = await self._http.get(path, params=params, headers=headers)
                else:
                    resp = await self._http.post(
                        path, content=body_str or "{}", headers=headers
                    )
                return self._parse(resp, path)
            except BitunixError as e:
                last_exc = e
                # Only transient, exchange-side conditions are worth retrying;
                # a signature or parameter error will fail identically.
                if e.retryable and attempt < self._max_retries - 1:
                    delay = 0.5 * (2**attempt)
                    log.warning("bitunix %s retry %d/%d in %.1fs: %s",
                                path, attempt + 1, self._max_retries, delay, e)
                    await asyncio.sleep(delay)
                    continue
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise BitunixError(-1, f"transport failure: {e}", path=path) from e
        if isinstance(last_exc, BitunixError):
            raise last_exc
        raise BitunixError(-1, f"transport failure: {last_exc}", path=path)

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> Any:
        try:
            data = resp.json()
        except ValueError:
            raise BitunixError(
                -1, f"non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}", path=path
            )

        if resp.status_code >= 400 and not isinstance(data, dict):
            raise BitunixError(int(resp.status_code), resp.text[:200], path=path)

        if isinstance(data, dict):
            code = int(data.get("code", ERR_SUCCESS))
            if code != ERR_SUCCESS:
                raise BitunixError(code, data.get("msg", "unknown error"), path=path)
            return data.get("data", data)

        raise BitunixError(-1, f"unexpected response shape (HTTP {resp.status_code})", path=path)

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", f"{FUTURES_PATH}{path}", params=params)

    async def _get_public(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", f"{FUTURES_PATH}{path}", params=params, public=True)

    async def _post(self, path: str, body: Optional[dict] = None) -> Any:
        return await self._request("POST", f"{FUTURES_PATH}{path}", body=body or {})

    # ------------------------------------------------------------------ #
    # Market data (public)                                                #
    # ------------------------------------------------------------------ #

    async def get_kline(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        kline_type: str = "LAST_PRICE",
    ) -> list:
        """Fetch closed klines, counting backward from ``end_time``.

        ``endTime`` is an EXCLUSIVE bound: the candle whose open time equals
        ``endTime`` is not part of the response. Omitting it makes the exchange
        anchor on the current forming candle's open time, so the newest bar
        returned is the last closed candle. Only closed candles are returned.
        """
        params: dict = {"symbol": symbol, "interval": interval, "limit": min(int(limit), 200)}
        # The official kline endpoint documents only symbol/endTime/interval/limit/type —
        # no startTime. Accepting it silently ignored the caller's paging intent;
        # keep the signature for compatibility but never send it on the wire.
        _ = start_time
        if end_time:
            params["endTime"] = end_time
        if kline_type:
            params["type"] = kline_type
        data = await self._get_public("/market/kline", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("klines", data.get("list", []))
        return []

    async def get_tickers(self, symbols: Optional[str] = None) -> Any:
        """`symbols` is a comma-separated string, e.g. "BTCUSDT,ETHUSDT"."""
        params = {"symbols": symbols} if symbols else {}
        return await self._get_public("/market/tickers", params)

    async def get_depth(self, symbol: str, limit: str = "max") -> Any:
        """`limit` is a gear value: 1/5/15/50/'max'."""
        return await self._get_public("/market/depth", {"symbol": symbol, "limit": limit})

    async def get_trading_pairs(self, symbols: Optional[str] = None) -> Any:
        params = {"symbols": symbols} if symbols else {}
        return await self._get_public("/market/trading_pairs", params)

    async def get_funding_rate(self, symbol: str) -> Any:
        """Single-symbol funding rate. See get_funding_rate_batch for all pairs."""
        return await self._get_public("/market/funding_rate", {"symbol": symbol})

    async def get_funding_rate_batch(self) -> list:
        """Current funding rate for every contract."""
        data = await self._get_public("/market/funding_rate/batch")
        return data if isinstance(data, list) else data.get("list", [])

    async def get_funding_rate_history(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        params: dict = {"symbol": symbol, "limit": min(int(limit), 200)}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get_public("/market/get_funding_rate_history", params)
        return data if isinstance(data, list) else data.get("list", [])

    # ------------------------------------------------------------------ #
    # Account                                                             #
    # ------------------------------------------------------------------ #

    async def get_account(self, margin_coin: str = "USDT") -> dict:
        """Single account. The exchange returns ``data`` as a LIST — unwrapped here.

        Fields: available, frozen, margin, transfer, positionMode,
        crossUnrealizedPNL, isolationUnrealizedPNL, bonus. There is no
        ``equity`` field; use :meth:`account_snapshot` for derived totals.
        """
        data = await self._get("/account", {"marginCoin": margin_coin})
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}

    async def account_snapshot(self, margin_coin: str = "USDT") -> "AccountSnapshot":
        """Normalized account view with margin genuinely accounted for.

        ``equity`` = available + margin + both unrealized PnL legs. That is the
        capital actually backing the account, including money locked as
        position margin — using bare ``available`` as equity understates it and
        sizes every trade too small.
        """
        raw = await self.get_account(margin_coin)
        return AccountSnapshot.from_raw(raw, margin_coin=margin_coin)

    async def get_leverage_margin_mode(self, symbol: str, margin_coin: str = "USDT") -> Any:
        return await self._get(
            "/account/get_leverage_margin_mode", {"symbol": symbol, "marginCoin": margin_coin}
        )

    async def get_position_mode(self) -> str:
        """Current account-wide position mode: ONE_WAY or HEDGE."""
        data = await self._get("/account/position_mode")
        if isinstance(data, dict):
            return str(data.get("positionMode", ""))
        return ""

    async def get_trading_settings(self, symbols: Optional[str] = None) -> list:
        """Per-symbol margin mode and leverage actually set on the account."""
        params = {"symbols": symbols} if symbols else {}
        data = await self._get("/account/trading_settings", params)
        return data if isinstance(data, list) else data.get("list", [])

    async def change_leverage(self, symbol: str, leverage: int, margin_coin: str = "USDT") -> Any:
        return await self._post(
            "/account/change_leverage",
            {"symbol": symbol, "leverage": int(leverage), "marginCoin": margin_coin},
        )

    async def change_margin_mode(
        self, symbol: str, margin_mode: str, margin_coin: str = "USDT"
    ) -> Any:
        """margin_mode: "ISOLATION" or "CROSS". Cannot change with open orders/positions."""
        mode = margin_mode.upper()
        if mode not in ("ISOLATION", "CROSS"):
            raise ValueError(f"margin_mode must be ISOLATION or CROSS, got {margin_mode!r}")
        return await self._post(
            "/account/change_margin_mode",
            {"symbol": symbol, "marginCoin": margin_coin, "marginMode": mode},
        )

    async def change_position_mode(self, position_mode: str) -> Any:
        """position_mode: "HEDGE" or "ONE_WAY". Account-wide; no marginCoin param."""
        mode = position_mode.upper()
        if mode not in ("HEDGE", "ONE_WAY"):
            raise ValueError(f"position_mode must be HEDGE or ONE_WAY, got {position_mode!r}")
        return await self._post("/account/change_position_mode", {"positionMode": mode})

    async def adjust_position_margin(
        self,
        symbol: str,
        amount: str,
        *,
        margin_coin: str = "USDT",
        position_id: str = "",
        side: str = "",
    ) -> Any:
        """Add (positive) or reduce (negative) isolated margin.

        ``amount`` carries the sign — there is no separate type field. Either
        ``position_id`` or ``side`` must be provided.
        """
        body: dict = {"symbol": symbol, "marginCoin": margin_coin, "amount": str(amount)}
        if position_id:
            body["positionId"] = position_id
        elif side:
            body["side"] = side.upper()
        else:
            raise ValueError("adjust_position_margin needs position_id or side")
        return await self._post("/account/adjust_position_margin", body)

    async def get_asset_query(self) -> dict:
        """Copy-trading asset query. Lives outside the /futures prefix."""
        return await self._request("GET", f"{COPY_PATH}/asset/query") or {}

    # ------------------------------------------------------------------ #
    # Positions                                                           #
    # ------------------------------------------------------------------ #

    async def get_pending_positions(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        data = await self._get("/position/get_pending_positions", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("positionList", data.get("list", []))
        return []

    async def get_history_positions(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
        skip: int = 0,
    ) -> list:
        params: dict = {"limit": min(int(limit), 100)}
        if skip:
            params["skip"] = skip
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/position/get_history_positions", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("positionList", data.get("list", []))
        return []

    async def get_position_tiers(self, symbol: str) -> Any:
        """Tiered risk limits: level, startValue, endValue, leverage, maintenanceMarginRate."""
        return await self._get("/position/get_position_tiers", {"symbol": symbol})

    # ------------------------------------------------------------------ #
    # Orders                                                              #
    # ------------------------------------------------------------------ #

    async def place_order(
        self,
        symbol: str,
        side: str,
        trade_side: str,
        order_type: str,
        qty: str,
        *,
        price: str = "",
        position_id: str = "",
        reduce_only: bool = False,
        effect: str = "",
        client_id: str = "",
        tp_price: str = "",
        tp_stop_type: str = "",
        tp_order_type: str = "",
        sl_price: str = "",
        sl_stop_type: str = "",
        sl_order_type: str = "",
    ) -> Any:
        """Open or close a position.

        Hedge-mode direction table (from the official docs):
            open long   -> side=BUY,  tradeSide=OPEN
            open short  -> side=SELL, tradeSide=OPEN
            close long  -> side=BUY,  tradeSide=CLOSE
            close short -> side=SELL, tradeSide=CLOSE

        Embedded TP/SL is accepted on the open order itself, which removes the
        unprotected window between fill and a follow-up TP/SL call.
        """
        body: dict = {
            "symbol": symbol,
            "side": side.upper(),
            "tradeSide": trade_side.upper(),
            "orderType": order_type.upper(),
            "qty": str(qty),
        }
        if price:
            body["price"] = str(price)
        if position_id:
            body["positionId"] = position_id
        # reduceOnly is a real boolean and must survive the bool-filter, so it
        # is always sent explicitly rather than dropped when False.
        body["reduceOnly"] = bool(reduce_only)
        if effect:
            body["effect"] = effect.upper()
        if client_id:
            body["clientId"] = client_id
        if tp_price:
            body["tpPrice"] = str(tp_price)
            body["tpStopType"] = tp_stop_type or "MARK_PRICE"
            body["tpOrderType"] = tp_order_type or "MARKET"
        if sl_price:
            body["slPrice"] = str(sl_price)
            body["slStopType"] = sl_stop_type or "MARK_PRICE"
            body["slOrderType"] = sl_order_type or "MARKET"
        return await self._post("/trade/place_order", body)

    async def batch_order(self, symbol: str, order_list: list) -> Any:
        """symbol is TOP-LEVEL; individual orders must NOT include symbol (max 5)."""
        if len(order_list) > 5:
            raise ValueError(f"batch_order accepts at most 5 orders, got {len(order_list)}")
        return await self._post("/trade/batch_order", {"symbol": symbol, "orderList": order_list})

    async def modify_order(
        self,
        *,
        order_id: str = "",
        client_id: str = "",
        symbol: str,
        qty: str,
        price: str,
        **tpsl: str,
    ) -> Any:
        """Modify a pending order. qty and price are mandatory here."""
        if not order_id and not client_id:
            raise ValueError("modify_order needs order_id or client_id")
        body: dict = {"symbol": symbol, "qty": str(qty), "price": str(price)}
        if order_id:
            body["orderId"] = order_id
        else:
            body["clientId"] = client_id
        for key in ("tpPrice", "tpStopType", "tpOrderType", "tpOrderPrice",
                    "slPrice", "slStopType", "slOrderType", "slOrderPrice"):
            if tpsl.get(key):
                body[key] = tpsl[key]
        return await self._post("/trade/modify_order", body)

    async def cancel_orders(self, symbol: str, order_ids: list) -> Any:
        return await self._post(
            "/trade/cancel_orders",
            {"symbol": symbol, "orderList": [{"orderId": oid} for oid in order_ids]},
        )

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> Any:
        body = {"symbol": symbol} if symbol else {}
        return await self._post("/trade/cancel_all_orders", body)

    async def get_order_detail(self, *, order_id: str = "", client_id: str = "") -> Any:
        if not order_id and not client_id:
            raise ValueError("get_order_detail needs order_id or client_id")
        params = {"orderId": order_id} if order_id else {"clientId": client_id}
        return await self._get("/trade/get_order_detail", params)

    async def flash_close_position(self, position_id: str) -> Any:
        """Close by position id. The body takes positionId ONLY — no symbol."""
        if not position_id:
            raise ValueError("flash_close_position requires a position_id")
        return await self._post("/trade/flash_close_position", {"positionId": position_id})

    async def close_all_positions(self, symbol: Optional[str] = None) -> Any:
        body = {"symbol": symbol} if symbol else {}
        return await self._post("/trade/close_all_position", body)

    async def get_pending_orders(
        self, symbol: Optional[str] = None, limit: int = 100, skip: int = 0
    ) -> list:
        params: dict = {"limit": min(int(limit), 100)}
        if symbol:
            params["symbol"] = symbol
        if skip:
            params["skip"] = skip
        data = await self._get("/trade/get_pending_orders", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("orderList", data.get("list", []))
        return []

    async def get_history_orders(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
        skip: int = 0,
    ) -> list:
        params: dict = {"limit": min(int(limit), 100)}
        if skip:
            params["skip"] = skip
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/trade/get_history_orders", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("orderList", data.get("list", []))
        return []

    async def get_history_trades(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
        skip: int = 0,
    ) -> list:
        params: dict = {"limit": min(int(limit), 100)}
        if skip:
            params["skip"] = skip
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/trade/get_history_trades", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tradeList", data.get("list", []))
        return []

    # ------------------------------------------------------------------ #
    # TP/SL                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_trigger(stop_type: str) -> str:
        """The API enum is MARK_PRICE / LAST_PRICE; accept the short forms too."""
        st = (stop_type or "MARK_PRICE").upper()
        if st in ("MARK", "MARK_PRICE"):
            return "MARK_PRICE"
        if st in ("LAST", "LAST_PRICE", "LASTPRICE"):
            return "LAST_PRICE"
        raise ValueError(f"stop_type must be MARK_PRICE or LAST_PRICE, got {stop_type!r}")

    @classmethod
    def _tpsl_body(
        cls,
        symbol: str,
        position_id: str = "",
        tp_price: str = "",
        tp_stop_type: str = "",
        tp_order_type: str = "",
        tp_order_price: str = "",
        tp_qty: str = "",
        sl_price: str = "",
        sl_stop_type: str = "",
        sl_order_type: str = "",
        sl_order_price: str = "",
        sl_qty: str = "",
    ) -> dict:
        body: dict = {"symbol": symbol}
        if position_id:
            body["positionId"] = position_id
        if tp_price:
            body["tpPrice"] = str(tp_price)
            body["tpStopType"] = cls._normalize_trigger(tp_stop_type)
            body["tpOrderType"] = (tp_order_type or "MARKET").upper()
            if tp_order_price and tp_order_price != "0":
                body["tpOrderPrice"] = str(tp_order_price)
            if tp_qty and tp_qty != "0":
                body["tpQty"] = str(tp_qty)
        if sl_price:
            body["slPrice"] = str(sl_price)
            body["slStopType"] = cls._normalize_trigger(sl_stop_type)
            body["slOrderType"] = (sl_order_type or "MARKET").upper()
            if sl_order_price and sl_order_price != "0":
                body["slOrderPrice"] = str(sl_order_price)
            if sl_qty and sl_qty != "0":
                body["slQty"] = str(sl_qty)
        if "tpPrice" not in body and "slPrice" not in body:
            raise ValueError("TP/SL request needs at least one of tp_price or sl_price")
        return body

    async def place_tpsl_order(self, **kwargs) -> Any:
        """Standalone TP/SL attached to a position. positionId is REQUIRED."""
        if not kwargs.get("position_id"):
            raise ValueError("place_tpsl_order requires position_id")
        return await self._post("/tpsl/place_order", self._tpsl_body(**kwargs))

    async def place_position_tpsl_order(self, **kwargs) -> Any:
        """Position-level TP/SL — closes the whole position (one per position)."""
        if not kwargs.get("position_id"):
            raise ValueError("place_position_tpsl_order requires position_id")
        return await self._post("/tpsl/position/place_order", self._tpsl_body(**kwargs))

    async def modify_tpsl_order(self, order_id: str, **kwargs) -> Any:
        """Modify an existing TP/SL order. orderId is the only required field."""
        body: dict = {"orderId": order_id}
        if kwargs.get("tp_price"):
            body["tpPrice"] = str(kwargs["tp_price"])
            if kwargs.get("tp_stop_type"):
                body["tpStopType"] = self._normalize_trigger(kwargs["tp_stop_type"])
            if kwargs.get("tp_order_type"):
                body["tpOrderType"] = str(kwargs["tp_order_type"]).upper()
            if kwargs.get("tp_order_price"):
                body["tpOrderPrice"] = str(kwargs["tp_order_price"])
            if kwargs.get("tp_qty"):
                body["tpQty"] = str(kwargs["tp_qty"])
        if kwargs.get("sl_price"):
            body["slPrice"] = str(kwargs["sl_price"])
            if kwargs.get("sl_stop_type"):
                body["slStopType"] = self._normalize_trigger(kwargs["sl_stop_type"])
            if kwargs.get("sl_order_type"):
                body["slOrderType"] = str(kwargs["sl_order_type"]).upper()
            if kwargs.get("sl_order_price"):
                body["slOrderPrice"] = str(kwargs["sl_order_price"])
            if kwargs.get("sl_qty"):
                body["slQty"] = str(kwargs["sl_qty"])
        if len(body) == 1:
            raise ValueError("modify_tpsl_order needs at least one of tp_price or sl_price")
        return await self._post("/tpsl/modify_order", body)

    async def modify_position_tpsl_order(
        self,
        position_id: str,
        symbol: str,
        tp_price: str = "",
        tp_stop_type: str = "",
        sl_price: str = "",
        sl_stop_type: str = "",
    ) -> Any:
        """Modify position-level TP/SL. Takes symbol + positionId, not orderId."""
        body: dict = {"positionId": position_id, "symbol": symbol}
        if tp_price:
            body["tpPrice"] = str(tp_price)
            body["tpStopType"] = self._normalize_trigger(tp_stop_type or "MARK_PRICE")
        if sl_price:
            body["slPrice"] = str(sl_price)
            body["slStopType"] = self._normalize_trigger(sl_stop_type or "MARK_PRICE")
        if len(body) == 2:
            raise ValueError("modify_position_tpsl_order needs tp_price or sl_price")
        return await self._post("/tpsl/position/modify_order", body)

    async def cancel_tpsl_order(self, symbol: str, tpsl_id: str) -> Any:
        """One cancellation per request; the field is orderId, not tpslIds."""
        return await self._post("/tpsl/cancel_order", {"symbol": symbol, "orderId": tpsl_id})

    async def get_pending_tpsl_orders(self, symbol: Optional[str] = None, limit: int = 100) -> list:
        params: dict = {"limit": min(int(limit), 100)}
        if symbol:
            params["symbol"] = symbol
        data = await self._get("/tpsl/get_pending_orders", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("orderList", data.get("list", []))
        return []

    async def get_history_tpsl_orders(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        params: dict = {"limit": min(int(limit), 100)}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/tpsl/get_history_orders", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("orderList", data.get("list", []))
        return []

    # ------------------------------------------------------------------ #
    # Composite helpers used by the engine                                #
    # ------------------------------------------------------------------ #

    async def replace_position_tpsl(
        self,
        symbol: str,
        position_id: str,
        *,
        tp_price: str = "",
        sl_price: str = "",
        stop_type: str = "MARK_PRICE",
        qty: str = "",
    ) -> Any:
        """Set TP/SL on a position, replacing any existing ones.

        There is no reliable atomic replace: the position-level order is
        modified when one exists, otherwise freshly placed. Cancelling first
        would open a window with no stop, so it is only done as a last resort.
        """
        stop_type = self._normalize_trigger(stop_type)
        try:
            return await self.modify_position_tpsl_order(
                position_id=position_id,
                symbol=symbol,
                tp_price=tp_price,
                tp_stop_type=stop_type,
                sl_price=sl_price,
                sl_stop_type=stop_type,
            )
        except BitunixError as e:
            if e.code not in (ERR_POSITION_NOT_EXIST, ERR_PARAM, 30006):
                raise
            return await self.place_position_tpsl_order(
                symbol=symbol,
                position_id=position_id,
                tp_price=tp_price,
                tp_stop_type=stop_type,
                sl_price=sl_price,
                sl_stop_type=stop_type,
            )

    async def close_position(self, symbol: str, position_id: str = "") -> Any:
        """Flash-close a position, resolving the position id when not supplied."""
        if not position_id:
            positions = await self.get_pending_positions(symbol)
            if not positions:
                raise BitunixError(ERR_POSITION_NOT_EXIST, f"no open position for {symbol}", path="/position/get_pending_positions")
            position_id = str(positions[0].get("positionId", ""))
        return await self.flash_close_position(position_id)


# ------------------------------------------------------------------ #
# Normalized response views                                           #
# ------------------------------------------------------------------ #

@dataclass
class AccountSnapshot:
    """Account state with the exchange's string fields parsed and equity derived."""

    margin_coin: str = "USDT"
    available: float = 0.0
    frozen: float = 0.0
    margin: float = 0.0
    transfer: float = 0.0
    cross_unrealized_pnl: float = 0.0
    isolation_unrealized_pnl: float = 0.0
    bonus: float = 0.0
    position_mode: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def equity(self) -> float:
        """Wallet balance backing the account, including posted margin."""
        return (
            self.available + self.margin + self.bonus
            + self.cross_unrealized_pnl + self.isolation_unrealized_pnl
        )

    @property
    def unrealized_pnl(self) -> float:
        return self.cross_unrealized_pnl + self.isolation_unrealized_pnl

    @classmethod
    def from_raw(cls, raw: dict, *, margin_coin: str = "USDT") -> "AccountSnapshot":
        def num(*keys: str) -> float:
            for k in keys:
                v = raw.get(k)
                if v in (None, ""):
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return 0.0

        return cls(
            margin_coin=str(raw.get("marginCoin") or margin_coin),
            available=num("available"),
            frozen=num("frozen"),
            margin=num("margin"),
            transfer=num("transfer"),
            cross_unrealized_pnl=num("crossUnrealizedPNL"),
            isolation_unrealized_pnl=num("isolationUnrealizedPNL"),
            bonus=num("bonus"),
            position_mode=str(raw.get("positionMode") or ""),
            raw=raw,
        )


@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float = 0.0
    ask: float = 0.0
    mark: float = 0.0
    index: float = 0.0
    funding: float = 0.0
    volume: float = 0.0
    high: float = 0.0
    low: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def spread_pct(self) -> float:
        if self.bid and self.ask and self.bid > 0:
            return (self.ask - self.bid) / self.bid * 100
        return 0.0


def parse_ticker(symbol: str, raw: dict) -> Ticker:
    """Normalize the exchange's ticker fields into a Ticker."""
    def num(*keys: str, default: float = 0.0) -> float:
        for k in keys:
            v = raw.get(k)
            if v in (None, ""):
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return default

    return Ticker(
        symbol=str(raw.get("symbol") or symbol),
        last=num("lastPrice", "last"),
        bid=num("bd", "bidPrice", "bid"),
        ask=num("ak", "askPrice", "ask"),
        mark=num("markPrice", "mp"),
        index=num("indexPrice", "ip"),
        funding=num("fundingRate", "fr"),
        volume=num("baseVol", "volume"),
        high=num("high"),
        low=num("low"),
        raw=raw,
    )
