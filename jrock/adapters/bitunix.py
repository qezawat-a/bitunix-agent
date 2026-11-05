"""Bitunix Futures REST adapter.

Derived from the battle-tested client in ../Bitunix-MCP/client.py, which was
corrected against the live API and the official docs. Endpoint paths, signing
algorithm, and field names here follow that client exactly — changing any of
them risks silent order-placement failures.

Signing (double SHA-256):
    digest = sha256(nonce + timestamp + api_key + query_str + body_str)
    sign   = sha256(digest + secret_key)

Query string for signing is the concatenation of key+value with NO separators,
sorted by key. Body is compact JSON with no whitespace.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

FUTURES_BASE = "https://fapi.bitunix.com"
FUTURES_PATH = "/api/v1/futures"
SPOT_BASE = "https://openapi.bitunix.com"
SPOT_PATH = "/api/spot/v1"

# Error codes we handle specially rather than surfacing as raw failures.
ERR_MARGIN_MODE = 10002  # margin mode / leverage change rejected
ERR_BAD_REQUEST = 2
ERR_TPSL_BROKEN = 30006  # /tpsl/modify_order is broken upstream


class BitunixError(RuntimeError):
    """An exchange-side rejection, carrying the exchange error code."""

    def __init__(self, code: int, msg: str, *, path: str = ""):
        super().__init__(f"Bitunix error {code}: {msg}" + (f" ({path})" if path else ""))
        self.code = code
        self.msg = msg
        self.path = path


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

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Signing                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _query_string(params: dict) -> str:
        """Sorted key+value concatenation with no separators — signing format."""
        return "".join(f"{k}{v}" for k, v in sorted(params.items()))

    def _headers(self, query_str: str = "", body_str: str = "") -> dict:
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))
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
            headers = {} if public else self._headers(self._query_string(params), body_str)
            try:
                if method == "GET":
                    resp = await self._http.get(path, params=params, headers=headers)
                else:
                    resp = await self._http.post(
                        path, content=body_str or "{}", headers=headers
                    )
                return self._parse(resp, path)
            except BitunixError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise BitunixError(-1, f"transport failure: {e}", path=path) from e
        raise BitunixError(-1, f"transport failure: {last_exc}", path=path)

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> Any:
        try:
            data = resp.json()
        except ValueError:
            raise BitunixError(-1, f"non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}", path=path)

        if resp.status_code >= 400:
            raise BitunixError(
                int(data.get("code", resp.status_code)),
                data.get("msg", resp.text[:200]),
                path=path,
            )

        code = data.get("code", -1)
        if code != 0:
            raise BitunixError(int(code), data.get("msg", "unknown error"), path=path)
        return data.get("data", data)

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", f"{FUTURES_PATH}{path}", params=params)

    async def _get_public(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", f"{FUTURES_PATH}{path}", params=params, public=True)

    async def _post(self, path: str, body: Optional[dict] = None, *, raw_body: bool = False) -> Any:
        """POST with a body. `raw_body` disables empty-value filtering."""
        if raw_body:
            return await self._request("POST", f"{FUTURES_PATH}{path}", body=body or {})
        filtered = {
            k: v
            for k, v in (body or {}).items()
            if v is not None and v != "" and v is not False or k == "reduceOnly"
        }
        return await self._request("POST", f"{FUTURES_PATH}{path}", body=filtered)

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
        params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if kline_type:
            params["type"] = kline_type
        data = await self._get_public("/market/kline", params)
        return data if isinstance(data, list) else data.get("klines", data.get("list", []))

    async def get_tickers(self, symbols: Optional[str] = None) -> Any:
        params = {"symbols": symbols} if symbols else {}
        return await self._get_public("/market/tickers", params)

    async def get_depth(self, symbol: str, limit: int = 20) -> Any:
        return await self._get_public("/market/depth", {"symbol": symbol, "limit": limit})

    async def get_trading_pairs(self, symbols: Optional[str] = None) -> Any:
        params = {"symbols": symbols} if symbols else {}
        return await self._get_public("/market/trading_pairs", params)

    async def get_funding_rate(self, symbol: str) -> Any:
        return await self._get_public("/market/funding_rate", {"symbol": symbol})

    # ------------------------------------------------------------------ #
    # Account                                                             #
    # ------------------------------------------------------------------ #

    async def get_account(self, margin_coin: str = "USDT") -> Any:
        return await self._get("/account", {"marginCoin": margin_coin})

    async def get_leverage_margin_mode(self, symbol: str, margin_coin: str = "USDT") -> Any:
        return await self._get(
            "/account/get_leverage_margin_mode", {"symbol": symbol, "marginCoin": margin_coin}
        )

    async def change_leverage(self, symbol: str, leverage: int, margin_coin: str = "USDT") -> Any:
        return await self._post(
            "/account/change_leverage", {"symbol": symbol, "leverage": leverage, "marginCoin": margin_coin}
        )

    async def change_margin_mode(
        self, symbol: str, margin_mode: str, margin_coin: str = "USDT"
    ) -> Any:
        """margin_mode: "ISOLATED" or "CROSS". Prefer ISOLATED."""
        return await self._post(
            "/account/change_margin_mode",
            {"symbol": symbol, "marginCoin": margin_coin, "marginMode": margin_mode},
        )

    async def change_position_mode(self, position_mode: str, margin_coin: str = "USDT") -> Any:
        """position_mode: "HEDGE" or "ONE_WAY"."""
        return await self._post(
            "/account/change_position_mode",
            {"positionMode": position_mode, "marginCoin": margin_coin},
        )

    async def adjust_position_margin(
        self,
        symbol: str,
        position_id: str,
        amount: str,
        type_: int,
        margin_coin: str = "USDT",
    ) -> Any:
        """type_: 1 = add margin, 2 = reduce margin."""
        return await self._post(
            "/account/adjust_position_margin",
            {
                "symbol": symbol,
                "positionId": position_id,
                "amount": amount,
                "type": type_,
                "marginCoin": margin_coin,
            },
        )

    # ------------------------------------------------------------------ #
    # Positions                                                           #
    # ------------------------------------------------------------------ #

    async def get_pending_positions(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        data = await self._get("/position/get_pending_positions", params)
        return data if isinstance(data, list) else data.get("list", data.get("positions", []))

    async def get_history_positions(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        params: dict = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/position/get_history_positions", params)
        return data if isinstance(data, list) else data.get("list", [])

    async def get_position_tiers(self, symbol: str) -> Any:
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
        effect: str = "GTC",
        client_id: str = "",
    ) -> Any:
        """Open or close a position.

        Hedge-mode direction table:
            open long   -> side=BUY,  tradeSide=OPEN
            open short  -> side=SELL, tradeSide=OPEN
            close long  -> side=SELL, tradeSide=CLOSE
            close short -> side=BUY,  tradeSide=CLOSE
        """
        body: dict = {
            "symbol": symbol,
            "side": side,
            "tradeSide": trade_side,
            "orderType": order_type,
            "qty": qty,
        }
        if price:
            body["price"] = price
        if position_id:
            body["positionId"] = position_id
        if reduce_only:
            body["reduceOnly"] = reduce_only
        if effect and effect != "GTC":
            body["effect"] = effect
        if client_id:
            body["clientId"] = client_id
        return await self._post("/trade/place_order", body, raw_body=True)

    async def batch_order(self, symbol: str, order_list: list) -> Any:
        """symbol is TOP-LEVEL; individual orders must NOT include symbol."""
        return await self._post("/trade/batch_order", {"symbol": symbol, "orderList": order_list}, raw_body=True)

    async def cancel_orders(self, symbol: str, order_ids: list) -> Any:
        return await self._post(
            "/trade/cancel_orders",
            {"orderList": [{"symbol": symbol, "orderId": oid} for oid in order_ids]},
            raw_body=True,
        )

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> Any:
        body = {"symbol": symbol} if symbol else {}
        return await self._post("/trade/cancel_all_orders", body, raw_body=True)

    async def flash_close_position(self, symbol: str, position_id: str) -> Any:
        return await self._post(
            "/trade/flash_close_position", {"symbol": symbol, "positionId": position_id}, raw_body=True
        )

    async def close_all_positions(self, symbol: Optional[str] = None) -> Any:
        body = {"symbol": symbol} if symbol else {}
        return await self._post("/trade/close_all_position", body, raw_body=True)

    async def get_pending_orders(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        data = await self._get("/trade/get_pending_orders", params)
        return data if isinstance(data, list) else data.get("list", [])

    async def get_history_orders(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        params: dict = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/trade/get_history_orders", params)
        return data if isinstance(data, list) else data.get("list", [])

    async def get_history_trades(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        params: dict = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/trade/get_history_trades", params)
        return data if isinstance(data, list) else data.get("list", [])

    # ------------------------------------------------------------------ #
    # TP/SL                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tpsl_body(
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
            body["tpPrice"] = tp_price
            body["tpStopType"] = tp_stop_type
            body["tpOrderType"] = tp_order_type
            if tp_order_type == "LIMIT" and tp_order_price and tp_order_price != "0":
                body["tpOrderPrice"] = tp_order_price
            if tp_qty and tp_qty != "0":
                body["tpQty"] = tp_qty
        if sl_price:
            body["slPrice"] = sl_price
            body["slStopType"] = sl_stop_type
            body["slOrderType"] = sl_order_type
            if sl_order_type == "LIMIT" and sl_order_price and sl_order_price != "0":
                body["slOrderPrice"] = sl_order_price
            if sl_qty and sl_qty != "0":
                body["slQty"] = sl_qty
        return body

    async def place_tpsl_order(self, **kwargs) -> Any:
        """Standalone TP/SL. Requires tpQty/slQty for position-attached orders."""
        return await self._post("/tpsl/place_order", self._tpsl_body(**kwargs))

    async def place_position_tpsl_order(self, **kwargs) -> Any:
        """Position-level TP/SL — closes the full position on trigger."""
        return await self._post("/tpsl/position/place_order", self._tpsl_body(**kwargs))

    async def modify_position_tpsl_order(
        self,
        position_id: str,
        symbol: str,
        tp_price: str = "",
        tp_stop_type: str = "",
        sl_price: str = "",
        sl_stop_type: str = "",
    ) -> Any:
        body: dict = {"positionId": position_id, "symbol": symbol}
        if tp_price:
            body["tpPrice"] = tp_price
            if tp_stop_type:
                body["tpStopType"] = tp_stop_type
        if sl_price:
            body["slPrice"] = sl_price
            if sl_stop_type:
                body["slStopType"] = sl_stop_type
        return await self._post("/tpsl/position/modify_order", body)

    async def cancel_tpsl_order(self, symbol: str, tpsl_ids: list) -> list:
        """One cancellation per request; the field is orderId, not tpslIds."""
        out = []
        for tpsl_id in tpsl_ids:
            out.append(
                await self._post("/tpsl/cancel_order", {"symbol": symbol, "orderId": tpsl_id}, raw_body=True)
            )
        return out

    async def get_pending_tpsl_orders(self, symbol: Optional[str] = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        data = await self._get("/tpsl/get_pending_orders", params)
        return data if isinstance(data, list) else data.get("list", [])

    async def get_history_tpsl_orders(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100,
    ) -> list:
        params: dict = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        data = await self._get("/tpsl/get_history_orders", params)
        return data if isinstance(data, list) else data.get("list", [])

    async def modify_tpsl_order(self, tpsl_id: str, symbol: str, **kwargs) -> Any:
        """The upstream modify endpoint is broken (30006) — cancel then re-place."""
        pending = await self.get_pending_tpsl_orders(symbol)
        order = next((o for o in pending if str(o.get("id")) == str(tpsl_id)), None)
        if not order:
            raise BitunixError(-1, f"TP/SL {tpsl_id} not found for {symbol}", path="/tpsl/modify_order")
        params = {
            "symbol": symbol,
            "position_id": order.get("positionId", ""),
            "tp_price": kwargs.get("tp_price") or str(order.get("tpPrice", "")),
            "tp_stop_type": kwargs.get("tp_stop_type") or order.get("tpStopType", ""),
            "tp_order_type": kwargs.get("tp_order_type") or order.get("tpOrderType", ""),
            "sl_price": kwargs.get("sl_price") or str(order.get("slPrice", "")),
            "sl_stop_type": kwargs.get("sl_stop_type") or order.get("slStopType", ""),
            "sl_order_type": kwargs.get("sl_order_type") or order.get("slOrderType", ""),
            "tp_qty": kwargs.get("tp_qty") or str(order.get("tpQty", "")),
            "sl_qty": kwargs.get("sl_qty") or str(order.get("slQty", "")),
        }
        await self.cancel_tpsl_order(symbol, [tpsl_id])
        return await self.place_tpsl_order(**params)


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
    """Normalize the exchange's varied field naming into a Ticker."""
    def num(*keys: str, default: float = 0.0) -> float:
        for k in keys:
            v = raw.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return default

    return Ticker(
        symbol=symbol,
        last=num("lastPrice", "last", "close", "markPrice"),
        bid=num("bidPrice", "bid1Price", "bid"),
        ask=num("askPrice", "ask1Price", "ask"),
        mark=num("markPrice", "mark"),
        index=num("indexPrice", "index"),
        funding=num("fundingRate", "funding"),
        volume=num("volume", "quoteVol", "turnover24h"),
        high=num("high24h", "high"),
        low=num("low24h", "low"),
        raw=raw,
    )
