"""TPSL (Take Profit / Stop Loss) calculator.

The agent auto-calculates TP and SL from signal strength and the liquidation
model. The user sets only two parameters:
    - breakeven_threshold_pct: trailing trigger activates when ROI reaches this
    - trailing_stop_roi_pct:  trailing stop fires at this ROI% (after breakeven)

The SL is placed at liq_sl_distance_pct% of the way from entry toward liquidation
(default 50%), ensuring it hits before the exchange liquidates us.

TP is computed from the signal's strategy confluence and confidence:
    - High confidence (≥85) → TP at 3× the SL distance (3:1 R/R)
    - Medium confidence (70-85) → TP at 2× SL distance
    - Lower confidence → TP at 1.5× SL distance

All prices are rounded to the symbol's price precision before being sent.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass
from typing import Optional

from jrock.risk.liquidation import liquidation_price, stop_price_for_liquidation_buffer
from .types import AggregatedSignal
from .settings import EngineSettings

log = logging.getLogger(__name__)


@dataclass
class TPSLPlan:
    """Computed TP and SL prices for a trade."""

    entry: float
    tp_price: float
    sl_price: float
    direction: str
    tp_pct: float      # TP distance from entry as %
    sl_pct: float      # SL distance from entry as %
    rr_ratio: float    # risk:reward
    liq_price: float   # estimated liquidation price
    liq_buffer_pct: float  # actual % gap between SL and liquidation
    valid: bool = True
    reason: str = ""

    def stop_type(self) -> str:
        return "MARK"  # Always use mark price for SL

    def tp_stop_type(self) -> str:
        return "MARK"

    def __repr__(self) -> str:
        if not self.valid:
            return f"TPSLPlan(invalid: {self.reason})"
        return (
            f"TPSLPlan({self.direction} entry={self.entry:.4f} "
            f"tp={self.tp_price:.4f}(+{self.tp_pct:.2f}%) "
            f"sl={self.sl_price:.4f}(-{self.sl_pct:.2f}%) "
            f"RR={self.rr_ratio:.1f} liq={self.liq_price:.4f})"
        )


def compute_tpsl(
    signal: AggregatedSignal,
    entry: float,
    qty: float,
    leverage: int,
    settings: EngineSettings,
    *,
    tiers: Optional[list] = None,
    price_precision: int = 2,
) -> TPSLPlan:
    """Compute TP and SL prices for a trade entry.

    Never fabricates a valid plan when inputs are bad — returns valid=False
    with a clear reason instead.
    """
    if entry <= 0:
        return TPSLPlan(entry, 0, 0, signal.direction, 0, 0, 0, 0, 0,
                       valid=False, reason="entry price must be positive")
    if qty <= 0:
        return TPSLPlan(entry, 0, 0, signal.direction, 0, 0, 0, 0, 0,
                       valid=False, reason="qty must be positive")

    def round_p(p: float) -> float:
        return round(p, price_precision)

    # Compute liquidation price.
    try:
        est = liquidation_price(
            entry=entry,
            qty=qty,
            leverage=leverage,
            side=signal.direction,
            tiers=tiers,
        )
    except Exception as e:
        return TPSLPlan(entry, 0, 0, signal.direction, 0, 0, 0, 0, 0,
                       valid=False, reason=f"liquidation calc failed: {e}")

    # SL: liq_sl_distance_pct% of the way from entry toward liquidation.
    try:
        sl = stop_price_for_liquidation_buffer(
            est,
            buffer_pct=settings.liq_sl_distance_pct,
            min_distance_pct=0.1,
        )
    except Exception as e:
        return TPSLPlan(entry, 0, 0, signal.direction, 0, 0, 0, est.liq_price, 0,
                       valid=False, reason=f"SL calc failed: {e}")

    sl_distance = abs(entry - sl)
    sl_pct = sl_distance / entry * 100

    # TP: risk:reward determined by signal confidence.
    confidence = signal.confidence
    if confidence >= 85:
        rr = 3.0
    elif confidence >= 75:
        rr = 2.5
    elif confidence >= 70:
        rr = 2.0
    else:
        rr = 1.5

    tp_distance = sl_distance * rr
    if signal.direction == "LONG":
        tp = entry + tp_distance
        sl_final = sl
    else:
        tp = entry - tp_distance
        sl_final = sl

    tp_pct = tp_distance / entry * 100

    # Sanity checks.
    if signal.direction == "LONG" and sl_final >= entry:
        return TPSLPlan(entry, 0, 0, signal.direction, 0, 0, 0, est.liq_price, 0,
                       valid=False, reason="SL above entry for LONG trade")
    if signal.direction == "SHORT" and sl_final <= entry:
        return TPSLPlan(entry, 0, 0, signal.direction, 0, 0, 0, est.liq_price, 0,
                       valid=False, reason="SL below entry for SHORT trade")

    # Verify SL is safely in front of liquidation.
    if signal.direction == "LONG":
        liq_buffer = (sl_final - est.liq_price) / entry * 100
    else:
        liq_buffer = (est.liq_price - sl_final) / entry * 100

    if liq_buffer < 0.05:
        log.warning("SL too close to liquidation: buffer=%.3f%%", liq_buffer)

    return TPSLPlan(
        entry=entry,
        tp_price=round_p(tp),
        sl_price=round_p(sl_final),
        direction=signal.direction,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        rr_ratio=rr,
        liq_price=round_p(est.liq_price),
        liq_buffer_pct=liq_buffer,
    )
