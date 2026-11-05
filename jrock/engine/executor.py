"""Order executor: places orders, sets TPSL, manages positions.

The executor is the only module that sends real orders to the exchange.
Every action is:
1. Pre-validated against risk limits.
2. Logged before and after (audit trail in state/journal.jsonl).
3. Wrapped with a confirmation gate when auto_accept is off.

Order flow for a new trade:
    1. change_leverage(symbol, leverage)         -- always first
    2. change_margin_mode(symbol, "ISOLATED")    -- always isolated
    3. place_order(...)                           -- market open
    4. get_pending_positions(symbol)              -- get position_id + qty
    5. place_tpsl_order(...)                      -- attach SL/TP at mark price

Order flow for closing:
    1. flash_close_position or place_order(CLOSE) with position_id

Mid-management (add margin, adjust TPSL) is triggered via command.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Callable, Awaitable

from jrock.adapters.bitunix import BitunixClient, BitunixError
from .settings import EngineSettings, STATE_DIR
from .tpsl import TPSLPlan, compute_tpsl
from jrock.risk.liquidation import parse_tiers
from jrock.risk.sizing import size_position, check_pretrade, RiskLimits, SymbolLimits
from .types import AggregatedSignal

log = logging.getLogger(__name__)

JOURNAL = STATE_DIR / "journal.jsonl"


def _journal(event: str, **data) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "event": event, **data}
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


ConfirmFn = Callable[[str], Awaitable[bool]]


@dataclass
class TradeResult:
    success: bool
    symbol: str
    direction: str
    qty: str
    entry_price: float
    position_id: str = ""
    tpsl_ok: bool = False
    tp_price: float = 0.0
    sl_price: float = 0.0
    order_id: str = ""
    error: str = ""
    reason: str = ""

    def __repr__(self) -> str:
        if not self.success:
            return f"TradeResult(FAILED {self.symbol}: {self.error})"
        return (
            f"TradeResult(OK {self.symbol} {self.direction} qty={self.qty} "
            f"entry={self.entry_price:.4f} tp={self.tp_price:.4f} sl={self.sl_price:.4f})"
        )


class Executor:
    """Async order executor with pre-trade risk gate."""

    def __init__(
        self,
        client: BitunixClient,
        settings: EngineSettings,
        confirm_fn: Optional[ConfirmFn] = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._confirm = confirm_fn  # None = auto-accept, set = always confirm
        self._risk = RiskLimits(
            max_open_positions=settings.max_open_positions,
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_notional_total=settings.max_notional_total,
        )
        # Symbol limits cache (populated from exchange on first use).
        self._limits: dict[str, SymbolLimits] = {}
        # Tier cache per symbol.
        self._tiers: dict[str, list] = {}

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    async def execute_signal(
        self,
        signal: AggregatedSignal,
        *,
        force: bool = False,
    ) -> TradeResult:
        """Execute an aggregated signal. Full flow: leverage → margin → order → TPSL."""
        sym = signal.symbol
        s = self._settings

        if not s.autotrade_enabled and not force:
            return TradeResult(False, sym, signal.direction, "0", 0,
                               error="autotrade disabled — call with force=True to override")

        # 1. Get current price and account state.
        try:
            tickers = await self._client.get_tickers(sym)
            ticker = tickers[0] if isinstance(tickers, list) and tickers else tickers
            entry = float(ticker.get("lastPrice") or ticker.get("last") or signal.price or 0)
            if entry <= 0:
                raise ValueError("cannot determine entry price")
        except Exception as e:
            return TradeResult(False, sym, signal.direction, "0", 0,
                               error=f"price fetch failed: {e}")

        try:
            account = await self._client.get_account()
            equity = float(
                account.get("equity") or account.get("totalEquity") or
                account.get("available") or account.get("balance") or 0
            )
            available = float(account.get("available") or account.get("availableMargin") or equity)
        except Exception as e:
            return TradeResult(False, sym, signal.direction, "0", 0,
                               error=f"account fetch failed: {e}")

        # 2. Get tier info and size the position.
        tiers = await self._get_tiers(sym)
        limits = await self._get_limits(sym)
        open_pos = await self._client.get_pending_positions(sym)

        # Compute preliminary SL distance for sizing.
        from jrock.risk.liquidation import liquidation_price, stop_price_for_liquidation_buffer
        try:
            est = liquidation_price(entry=entry, qty=1.0, leverage=s.leverage, side=signal.direction, tiers=tiers)
            rough_sl = stop_price_for_liquidation_buffer(est, s.liq_sl_distance_pct)
            stop_dist = abs(entry - rough_sl)
        except Exception:
            stop_dist = entry * 0.02  # 2% fallback

        size = size_position(
            entry=entry,
            stop=entry - stop_dist if signal.direction == "LONG" else entry + stop_dist,
            equity=equity,
            leverage=s.leverage,
            limits=limits,
            risk_pct=s.risk_per_trade_pct,
            available_margin=available,
            max_margin_pct=0.5,
        )
        if not size.valid:
            _journal("size_rejected", symbol=sym, reason=size.reason)
            return TradeResult(False, sym, signal.direction, "0", entry,
                               error=f"sizing: {size.reason}")

        # 3. Pre-trade risk check.
        allowed, reason = check_pretrade(
            open_positions=len(open_pos),
            equity=equity,
            available_margin=available,
            proposed_notional=size.notional,
            proposed_margin=size.margin,
            limits=self._risk,
        )
        if not allowed:
            _journal("pretrade_blocked", symbol=sym, reason=reason)
            return TradeResult(False, sym, signal.direction, str(size.qty), entry,
                               error=f"risk check: {reason}")

        # 4. Confirmation gate.
        qty_str = str(size.qty)
        tpsl_plan = compute_tpsl(signal, entry, size.qty, s.leverage, s, tiers=tiers)

        summary = (
            f"OPEN {signal.direction} {sym}\n"
            f"  qty={qty_str}  entry≈{entry:.4f}\n"
            f"  leverage={s.leverage}x  notional={size.notional:.2f}\n"
            f"  margin≈{size.margin:.2f}  equity={equity:.2f}\n"
            f"  TP={tpsl_plan.tp_price if tpsl_plan.valid else 'n/a'}  "
            f"SL={tpsl_plan.sl_price if tpsl_plan.valid else 'n/a'}\n"
            f"  liq≈{tpsl_plan.liq_price if tpsl_plan.valid else 'unknown'}\n"
            f"  strategies: {', '.join(signal.strategy_names[:4])}\n"
            f"  confidence: {signal.confidence:.1f}%"
        )

        if self._confirm is not None:
            approved = await self._confirm(summary)
            if not approved:
                _journal("user_rejected", symbol=sym, summary=summary)
                return TradeResult(False, sym, signal.direction, qty_str, entry,
                                   error="rejected by user")

        _journal("order_intent", symbol=sym, direction=signal.direction,
                 qty=qty_str, entry=entry, tpsl=asdict(tpsl_plan) if tpsl_plan.valid else {})

        # 5. Set leverage + margin mode.
        try:
            await self._client.change_leverage(sym, s.leverage)
        except BitunixError as e:
            log.warning("leverage set failed: %s", e)

        try:
            await self._client.change_margin_mode(sym, "ISOLATED")
        except BitunixError as e:
            log.warning("margin mode: %s (may already be set)", e)

        # 6. Place the order.
        side = "BUY" if signal.direction == "LONG" else "SELL"
        try:
            result = await self._client.place_order(
                sym, side=side, trade_side="OPEN",
                order_type="MARKET", qty=qty_str,
            )
        except BitunixError as e:
            _journal("order_failed", symbol=sym, error=str(e))
            return TradeResult(False, sym, signal.direction, qty_str, entry,
                               error=f"place_order failed: {e}")

        order_id = str(result.get("orderId", "") if isinstance(result, dict) else "")
        _journal("order_placed", symbol=sym, order_id=order_id, direction=signal.direction,
                 qty=qty_str, entry=entry)

        # 7. Fetch position_id for TPSL.
        await asyncio.sleep(0.5)
        positions = await self._client.get_pending_positions(sym)
        pos = next(
            (p for p in positions
             if p.get("side", "").upper() == signal.direction.upper()[:1] or
                (signal.direction == "LONG" and p.get("side") in ("BUY", "LONG")) or
                (signal.direction == "SHORT" and p.get("side") in ("SELL", "SHORT"))),
            positions[0] if positions else None,
        )
        position_id = str(pos.get("positionId", "")) if pos else ""
        actual_entry = float(pos.get("avgOpenPrice") or pos.get("entryPrice") or entry) if pos else entry
        actual_qty = str(pos.get("qty") or qty_str) if pos else qty_str

        # 8. Attach TPSL.
        tpsl_ok = False
        tp_price = 0.0
        sl_price = 0.0
        if tpsl_plan.valid and position_id:
            try:
                await self._client.place_tpsl_order(
                    symbol=sym,
                    position_id=position_id,
                    tp_price=str(tpsl_plan.tp_price),
                    tp_stop_type=tpsl_plan.tp_stop_type(),
                    tp_order_type="MARKET",
                    tp_qty=actual_qty,
                    sl_price=str(tpsl_plan.sl_price),
                    sl_stop_type=tpsl_plan.stop_type(),
                    sl_order_type="MARKET",
                    sl_qty=actual_qty,
                )
                tpsl_ok = True
                tp_price = tpsl_plan.tp_price
                sl_price = tpsl_plan.sl_price
                _journal("tpsl_set", symbol=sym, position_id=position_id,
                         tp=tp_price, sl=sl_price)
            except BitunixError as e:
                log.error("TPSL placement failed: %s — position is UNPROTECTED", e)
                _journal("tpsl_failed", symbol=sym, error=str(e), tp=tpsl_plan.tp_price, sl=tpsl_plan.sl_price)

        return TradeResult(
            success=True,
            symbol=sym,
            direction=signal.direction,
            qty=actual_qty,
            entry_price=actual_entry,
            position_id=position_id,
            tpsl_ok=tpsl_ok,
            tp_price=tp_price,
            sl_price=sl_price,
            order_id=order_id,
        )

    async def close_position(self, symbol: str, position_id: str = "") -> dict:
        """Flash-close a position. Returns exchange response."""
        if not position_id:
            positions = await self._client.get_pending_positions(symbol)
            if not positions:
                return {"error": f"no open positions for {symbol}"}
            position_id = str(positions[0].get("positionId", ""))
        result = await self._client.flash_close_position(symbol, position_id)
        _journal("position_closed", symbol=symbol, position_id=position_id)
        return result

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _get_tiers(self, symbol: str) -> list:
        if symbol not in self._tiers:
            try:
                raw = await self._client.get_position_tiers(symbol)
                self._tiers[symbol] = parse_tiers(raw)
            except Exception:
                self._tiers[symbol] = []
        return self._tiers[symbol]

    async def _get_limits(self, symbol: str) -> SymbolLimits:
        if symbol not in self._limits:
            try:
                pairs = await self._client.get_trading_pairs(symbol)
                pair = pairs[0] if isinstance(pairs, list) and pairs else (pairs or {})
                min_qty = float(pair.get("minTradeNum") or pair.get("minQty") or 0.001)
                step = float(pair.get("tradeNumPrecision") or pair.get("stepSize") or min_qty)
                price_prec = int(pair.get("pricePrecision") or 2)
                qty_prec = int(pair.get("quantityPrecision") or 3)
                self._limits[symbol] = SymbolLimits(
                    symbol=symbol,
                    min_qty=min_qty,
                    step=step,
                    price_precision=price_prec,
                    qty_precision=qty_prec,
                )
            except Exception:
                self._limits[symbol] = SymbolLimits(symbol=symbol)
        return self._limits[symbol]
