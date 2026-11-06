"""Engine settings — loaded from .env / environment, with typed defaults.

All runtime state that changes via commands (leverage, symbols, scan intervals
etc.) lives here and is persisted to state/settings.json automatically.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


STATE_DIR = Path(os.environ.get("JRROCK_STATE_DIR", Path(__file__).resolve().parents[1] / "state"))
SETTINGS_FILE = STATE_DIR / "settings.json"


@dataclass
class EngineSettings:
    # Trading
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT"])
    leverage: int = 10
    margin_mode: str = "ISOLATED"       # ISOLATED | CROSS
    position_mode: str = "HEDGE"        # HEDGE | ONE_WAY
    max_open_positions: int = 3
    risk_per_trade_pct: float = 1.0     # % of equity per trade

    # Scanner
    scan_enabled: bool = False
    scan_interval_sec: int = 30
    guard_interval_sec: int = 30            # trailing/breakeven monitor cadence (was implicitly 30)
    timeframes: list[str] = field(default_factory=lambda: ["1m", "3m", "5m", "15m"])
    min_confidence: float = 80.0
    tf_min_confidence: float = 60.0
    min_agreeing_strategies: int = 3

    # Reversal handling
    reversal_enabled: bool = True
    reversal_confidence: float = 75.0

    # TPSL (computed by engine from signal strength; only buffer params here)
    breakeven_threshold_pct: float = 4.0    # trailing trigger: move stop to BE at this ROI%
    trailing_stop_roi_pct: float = 6.0      # trailing trigger fires at this ROI%
    trailing_distance_pct: float = 2.0      # SL distance behind price once trailing (ROI%)
    liq_sl_distance_pct: float = 50.0       # SL is this % of (entry -> liq) distance from entry
    sl_stop_type: str = "MARK"              # MARK | LAST_PRICE

    # Safety
    liq_unsafe_refusal: bool = True         # refuse the trade if SL would be too close to liquidation

    # Autotrade
    autotrade_enabled: bool = False
    report_enabled: bool = False
    report_interval_sec: int = 30

    # Model / agent
    model_id: str = ""
    base_url: str = ""

    # Safety
    max_daily_loss_pct: float = 5.0
    max_notional_total: float = 0.0         # 0 = unlimited
    cross_margin_risk_ceiling_pct: float = 25.0  # hard cap: CROSS margin risk % of equity

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)

    @classmethod
    def load(cls) -> "EngineSettings":
        if not SETTINGS_FILE.exists():
            return cls()
        try:
            d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            s = cls()
            for k, v in d.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            return s
        except Exception:
            return cls()

    def update(self, **kwargs) -> list[str]:
        """Apply key=value changes, return list of changed keys."""
        changed = []
        for k, v in kwargs.items():
            if not hasattr(self, k):
                continue
            current = getattr(self, k)
            # coerce type to match current
            try:
                if isinstance(current, bool):
                    v = str(v).lower() in ("true", "1", "yes", "on")
                elif isinstance(current, int):
                    v = int(v)
                elif isinstance(current, float):
                    v = float(v)
                elif isinstance(current, list):
                    if isinstance(v, str):
                        v = [s.strip() for s in v.split(",")]
                elif isinstance(current, str):
                    v = str(v)
            except (TypeError, ValueError) as e:
                raise ValueError(f"Invalid value for {k!r}: {e}") from e
            if current != v:
                setattr(self, k, v)
                changed.append(k)
        if changed:
            self.save()
        return changed

    def summary(self) -> str:
        d = asdict(self)
        lines = []
        for k, v in d.items():
            lines.append(f"  {k}: {v}")
        return "Settings:\n" + "\n".join(lines)
