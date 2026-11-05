"""Position sizing and pre-trade risk checks.

The engine never sizes a position from a hardcoded number of coins. It derives
quantity from the account's available margin, the signal's stop distance, and a
per-trade risk budget, then verifies the result against the exchange's minimum
and step constraints.

Sizing model:
    risk_amount   = equity * risk_per_trade_pct
    stop_distance = |entry - stop|
    qty           = risk_amount / stop_distance
    notional      = qty * entry
    margin        = notional / leverage

qty is then clamped to [min_qty, max_qty] and rounded down to the symbol's step.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class SymbolLimits:
    """Exchange-imposed and account-imposed constraints for one symbol."""

    symbol: str
    min_qty: float = 0.001
    max_qty: float = 1e9
    step: float = 0.001
    min_notional: float = 0.0
    price_precision: int = 2
    qty_precision: int = 3

    def round_qty(self, qty: float) -> float:
        """Round DOWN to the step so we never exceed the intended risk."""
        if self.step <= 0:
            return qty
        q = math.floor(qty / self.step) * self.step
        return round(q, self.qty_precision)

    def round_price(self, price: float) -> float:
        if price <= 0:
            return 0.0
        return round(price, self.price_precision)


@dataclass
class SizeResult:
    qty: float
    notional: float
    margin: float
    risk_amount: float
    stop_distance: float
    stop_distance_pct: float
    leverage: float
    clamped_by: str = ""
    valid: bool = True
    reason: str = ""

    def __str__(self) -> str:
        if not self.valid:
            return f"size rejected: {self.reason}"
        return (
            f"qty={self.qty:g} notional={self.notional:.2f} margin={self.margin:.2f} "
            f"risk={self.risk_amount:.2f} stop={self.stop_distance_pct:.2f}% "
            f"leverage={self.leverage:g}x"
            + (f" [clamped: {self.clamped_by}]" if self.clamped_by else "")
        )


def size_position(
    *,
    entry: float,
    stop: float,
    equity: float,
    leverage: float,
    limits: SymbolLimits,
    risk_pct: float = 1.0,
    available_margin: Optional[float] = None,
    max_notional: Optional[float] = None,
    max_margin_pct: float = 0.5,
) -> SizeResult:
    """Compute a position size honoring risk budget and exchange limits.

    Returns a SizeResult with valid=False (and a human-readable reason) when no
    sane size exists — the engine must treat that as a hard block, never a
    reason to fall back to a hardcoded quantity.
    """
    if entry <= 0:
        return SizeResult(0, 0, 0, 0, 0, 0, leverage, valid=False, reason="entry price must be positive")
    if stop <= 0:
        return SizeResult(0, 0, 0, 0, 0, 0, leverage, valid=False, reason="stop price must be positive")
    if equity <= 0:
        return SizeResult(0, 0, 0, 0, 0, 0, leverage, valid=False, reason="equity must be positive")
    if leverage <= 0:
        return SizeResult(0, 0, 0, 0, 0, 0, leverage, valid=False, reason="leverage must be positive")

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return SizeResult(0, 0, 0, 0, 0, 0, leverage, valid=False, reason="stop equals entry")

    stop_distance_pct = stop_distance / entry * 100
    if stop_distance_pct > 50:
        return SizeResult(
            0, 0, 0, 0, stop_distance, stop_distance_pct, leverage,
            valid=False, reason=f"stop distance {stop_distance_pct:.1f}% exceeds 50% sanity limit",
        )

    risk_amount = equity * (risk_pct / 100)
    qty = risk_amount / stop_distance

    # Available-margin constraint: margin = notional / leverage = qty*entry/leverage
    if available_margin is not None and available_margin > 0:
        margin_cap_qty = available_margin * leverage / entry
        if qty > margin_cap_qty:
            qty = margin_cap_qty
            clamped_by = "available_margin"
        else:
            clamped_by = ""
    else:
        clamped_by = ""

    # Account-level: never commit more than max_margin_pct of equity.
    max_margin = equity * max_margin_pct
    if entry > 0 and leverage > 0:
        margin_cap = max_margin * leverage / entry
        if qty > margin_cap:
            qty = margin_cap
            clamped_by = "max_margin_pct"

    if max_notional is not None and max_notional > 0:
        notional_cap = max_notional / entry
        if qty > notional_cap:
            qty = notional_cap
            clamped_by = "max_notional"

    # Exchange limits, rounded down to step.
    qty = min(qty, limits.max_qty)
    qty = limits.round_qty(qty)

    if qty < limits.min_qty:
        return SizeResult(
            qty, qty * entry, qty * entry / leverage, risk_amount,
            stop_distance, stop_distance_pct, leverage,
            valid=False,
            reason=f"qty {qty:g} < exchange minimum {limits.min_qty:g}",
        )

    notional = qty * entry
    if notional < limits.min_notional:
        return SizeResult(
            qty, notional, notional / leverage, risk_amount,
            stop_distance, stop_distance_pct, leverage,
            valid=False,
            reason=f"notional {notional:.2f} < exchange minimum {limits.min_notional:.2f}",
        )

    margin = notional / leverage
    return SizeResult(
        qty=qty,
        notional=notional,
        margin=margin,
        risk_amount=risk_amount,
        stop_distance=stop_distance,
        stop_distance_pct=stop_distance_pct,
        leverage=leverage,
        clamped_by=clamped_by,
    )


@dataclass
class RiskLimits:
    """Account-level guardrails enforced before any order is sent."""

    max_open_positions: int = 3
    max_notional_total: float = 0.0  # 0 = unlimited
    max_daily_loss_pct: float = 5.0
    max_single_loss_pct: float = 2.0
    min_margin_buffer_pct: float = 10.0
    allow_new_positions: bool = True
    block_reason: str = ""


def check_pretrade(
    *,
    open_positions: int,
    equity: float,
    available_margin: float,
    proposed_notional: float,
    proposed_margin: float,
    limits: RiskLimits,
    daily_pnl_pct: float = 0.0,
    proposed_risk_pct: float = 0.0,
) -> tuple[bool, str]:
    """Return (allowed, reason). Never raises — a block must be a clean answer."""
    if not limits.allow_new_positions:
        return False, "new positions disabled by risk limits"
    if open_positions >= limits.max_open_positions:
        return False, f"max open positions reached ({open_positions}/{limits.max_open_positions})"

    if equity > 0 and daily_pnl_pct <= -limits.max_daily_loss_pct:
        return False, f"daily loss limit hit ({daily_pnl_pct:.2f}%)"

    if equity > 0 and proposed_risk_pct > limits.max_single_loss_pct:
        return False, f"single-trade risk {proposed_risk_pct:.2f}% > {limits.max_single_loss_pct:.2f}%"

    if available_margin > 0:
        required = proposed_margin * (1 + limits.min_margin_buffer_pct / 100)
        if required > available_margin:
            return False, (
                f"margin {proposed_margin:.2f} + {limits.min_margin_buffer_pct:.0f}% buffer "
                f"> available {available_margin:.2f}"
            )

    if limits.max_notional_total > 0:
        if proposed_notional > limits.max_notional_total:
            return False, f"notional {proposed_notional:.2f} > limit {limits.max_notional_total:.2f}"

    return True, ""
