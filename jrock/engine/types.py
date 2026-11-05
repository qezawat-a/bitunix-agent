"""Shared engine types — kept dependency-free to avoid circular imports."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from jrock.strategies.signals import Signal


@dataclass
class AggregatedSignal:
    """Signals from multiple strategies and timeframes that agree on direction."""

    symbol: str
    direction: str          # "LONG" | "SHORT"
    confidence: float       # weighted average of constituent signal confidences
    agreeing: int           # number of agreeing strategies (across all timeframes)
    signals: list[Signal] = field(default_factory=list)
    price: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def side(self) -> str:
        return "BUY" if self.direction == "LONG" else "SELL"

    @property
    def strategy_names(self) -> list[str]:
        return [s.strategy for s in self.signals]

    def __repr__(self) -> str:
        return (
            f"AggSignal({self.symbol} {self.direction} "
            f"conf={self.confidence:.1f}% agreeing={self.agreeing} "
            f"strats={','.join(self.strategy_names[:3])})"
        )
