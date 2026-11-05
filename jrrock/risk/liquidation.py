"""Liquidation-price and margin math for USDT-margined futures.

The exchange liquidates when the position's maintenance margin requirement can
no longer be met. This module computes the liquidation price so the engine can
place a stop loss *before* it, which is the single most important safety
property of the trader.

Formulas (isolated margin, USDT-margined perpetual):

    nominal      = qty * entry_price
    margin       = nominal / leverage
    maint_rate   = tier maintenance margin rate for this notional
    maint_margin = nominal * maint_rate
    liq_fee      = nominal * taker_fee_rate        (conservative: assume close is taker)

    Long : liq = (entry* qty - margin + maint_margin + liq_fee) / qty
          which simplifies to
          liq = entry + (maint_margin + liq_fee - margin) / qty
    Short: liq = entry - (margin - maint_margin - liq_fee) / qty

Using mark-price-style conservative assumptions keeps the computed liquidation
price slightly *worse* than the exchange's, so our stop sits safely in front of
it. We then require a configurable buffer (default 50% of the distance from
entry to liquidation) between the stop and the liquidation price.

Tiered risk limits are fetched live from /position/get_position_tiers, but a
conservative default table is included so sizing works before the first fetch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Conservative default maintenance margin rates by notional tier (USDT).
# Real tiers come from the exchange; these are only a bootstrap fallback.
DEFAULT_TIERS: list[tuple[float, float]] = [
    # (max notional USDT, maintenance margin rate)
    (50_000, 0.004),
    (250_000, 0.005),
    (1_000_000, 0.01),
    (10_000_000, 0.025),
    (20_000_000, 0.05),
    (100_000_000, 0.10),
    (float("inf"), 0.50),
]

# Conservative taker fee assumption (0.05% is typical for USDT-margined takers).
DEFAULT_TAKER_FEE = 0.0005


@dataclass(frozen=True)
class LiquidationEstimate:
    """Result of a liquidation-price computation."""

    entry: float
    qty: float
    leverage: float
    side: str  # "LONG" | "SHORT"
    liq_price: float
    margin: float
    maint_margin: float
    maint_rate: float
    liq_fee: float
    notional: float
    distance_pct: float  # (entry - liq) / entry * 100, positive for both sides

    @property
    def safe(self) -> bool:
        """True if the computed liquidation price is sane (positive, finite)."""
        return self.liq_price > 0 and self.liq_price == self.liq_price  # NaN check


def maintenance_rate(notional: float, tiers: Optional[list[tuple[float, float]]] = None) -> float:
    """Look up the maintenance margin rate for a notional value."""
    tiers = tiers or DEFAULT_TIERS
    for cap, rate in tiers:
        if notional <= cap:
            return rate
    return tiers[-1][1]


def parse_tiers(raw: list | dict) -> list[tuple[float, float]]:
    """Normalize the exchange's tier response into (cap_notional, mmr) pairs."""
    out: list[tuple[float, float]] = []
    items = raw if isinstance(raw, list) else raw.get("list", raw.get("tiers", []))
    if isinstance(items, dict):
        items = [items]
    for item in items:
        if not isinstance(item, dict):
            continue
        cap = item.get("maximumMarginLimit") or item.get("maxNotional") or item.get("maxQty") or 0
        rate = item.get("maintenanceMarginRate") or item.get("mmr") or item.get("rate") or 0
        try:
            cap = float(cap)
            rate = float(rate)
        except (TypeError, ValueError):
            continue
        if cap > 0 and rate > 0:
            out.append((cap, rate))
    out.sort(key=lambda t: t[0])
    return out or list(DEFAULT_TIERS)


def liquidation_price(
    entry: float,
    qty: float,
    leverage: float,
    side: str,
    *,
    tiers: Optional[list[tuple[float, float]]] = None,
    taker_fee: float = DEFAULT_TAKER_FEE,
) -> LiquidationEstimate:
    """Compute the estimated liquidation price.

    Raises ValueError for nonsensical inputs rather than returning garbage.
    """
    if entry <= 0 or qty <= 0:
        raise ValueError(f"entry and qty must be positive (entry={entry}, qty={qty})")
    if leverage <= 0:
        raise ValueError(f"leverage must be positive (leverage={leverage})")

    side = side.upper()
    if side not in ("LONG", "SHORT", "BUY", "SELL"):
        raise ValueError(f"side must be LONG/SHORT/BUY/SELL, got {side!r}")
    side = "LONG" if side in ("LONG", "BUY") else "SHORT"

    notional = entry * qty
    margin = notional / leverage
    rate = maintenance_rate(notional, tiers)
    maint_margin = notional * rate
    liq_fee = notional * taker_fee

    if side == "LONG":
        # LONG: price falling liquidates us.
        # liq = entry - (margin - maint_margin - liq_fee) / qty
        liq = entry - (margin - maint_margin - liq_fee) / qty
        liq = max(liq, 0.0)
    else:
        # SHORT: price rising liquidates us.
        # liq = entry + (margin - maint_margin - liq_fee) / qty
        liq = entry + (margin - maint_margin - liq_fee) / qty
        liq = max(liq, 0.0)

    distance_pct = abs(entry - liq) / entry * 100 if entry > 0 else 0.0

    return LiquidationEstimate(
        entry=entry,
        qty=qty,
        leverage=leverage,
        side=side,
        liq_price=liq,
        margin=margin,
        maint_margin=maint_margin,
        maint_rate=rate,
        liq_fee=liq_fee,
        notional=notional,
        distance_pct=distance_pct,
    )


def stop_price_for_liquidation_buffer(
    est: LiquidationEstimate,
    buffer_pct: float,
    *,
    min_distance_pct: float = 0.1,
) -> float:
    """Place a stop `buffer_pct` of the way from entry toward liquidation.

    buffer_pct=50 means the stop sits halfway between entry and liquidation,
    which is what the user requested ("Stop Loss must have a distance about 50%
    ROI from the liq price").

    The returned price is guaranteed to be strictly on the safe side of
    liquidation, with at least `min_distance_pct` separation.
    """
    if not 0 < buffer_pct <= 100:
        raise ValueError(f"buffer_pct must be in (0, 100], got {buffer_pct}")

    if est.side == "LONG":
        span = est.entry - est.liq_price
        stop = est.entry - span * (buffer_pct / 100)
        floor = est.liq_price * (1 + min_distance_pct / 100)
        return max(stop, floor)
    else:
        span = est.liq_price - est.entry
        stop = est.entry + span * (buffer_pct / 100)
        cap = est.liq_price * (1 - min_distance_pct / 100)
        return min(stop, cap)


def required_margin(entry: float, qty: float, leverage: float) -> float:
    """Initial margin for a position."""
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    return entry * qty / leverage


def max_qty_for_margin(entry: float, leverage: float, available_margin: float, *, safety: float = 0.95) -> float:
    """Largest qty that fits in `available_margin`, leaving headroom."""
    if entry <= 0 or leverage <= 0 or available_margin <= 0:
        return 0.0
    return (available_margin * safety * leverage) / entry
