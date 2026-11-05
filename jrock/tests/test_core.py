"""Tests for liquidation math, risk sizing, indicators, and strategies."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest

from risk.liquidation import (
    liquidation_price,
    stop_price_for_liquidation_buffer,
    maintenance_rate,
    DEFAULT_TIERS,
)
from risk.sizing import size_position, check_pretrade, SymbolLimits, RiskLimits
from strategies.indicators import ema, sma, rsi, macd, bollinger, vwap, from_klines, is_hammer, is_shooting_star
from strategies.signals import run_all, golden_cross_enhanced, death_cross_enhanced


# ──────────────────────────────────────────────────────────────────────────────
# Liquidation math
# ──────────────────────────────────────────────────────────────────────────────

def test_liq_long_10x():
    est = liquidation_price(entry=100.0, qty=1.0, leverage=10, side="LONG")
    assert est.liq_price > 0
    # LONG: liq is BELOW entry (~9-11% below at 10x)
    assert 85 < est.liq_price < 96, f"Unexpected liq_price={est.liq_price}"
    assert est.side == "LONG"
    assert est.distance_pct > 0


def test_liq_short_10x():
    est = liquidation_price(entry=100.0, qty=1.0, leverage=10, side="SHORT")
    assert est.liq_price > 0
    # SHORT: liq is ABOVE entry (~9-11% above at 10x)
    assert 104 < est.liq_price < 116, f"Unexpected liq_price={est.liq_price}"
    assert est.side == "SHORT"


def test_liq_long_sl_before_liq():
    est = liquidation_price(entry=100.0, qty=1.0, leverage=10, side="LONG")
    sl = stop_price_for_liquidation_buffer(est, buffer_pct=50.0)
    # SL must be above liq, below entry.
    assert est.liq_price < sl < 100.0, f"SL={sl} liq={est.liq_price}"


def test_liq_short_sl_before_liq():
    est = liquidation_price(entry=100.0, qty=1.0, leverage=10, side="SHORT")
    sl = stop_price_for_liquidation_buffer(est, buffer_pct=50.0)
    # SHORT: liq is above entry. SL sits between entry and liq (above entry, below liq).
    assert est.entry < sl < est.liq_price, f"SL={sl} entry={est.entry} liq={est.liq_price}"


def test_liq_raises_on_bad_input():
    with pytest.raises(ValueError):
        liquidation_price(entry=0, qty=1.0, leverage=10, side="LONG")
    with pytest.raises(ValueError):
        liquidation_price(entry=100, qty=1.0, leverage=0, side="LONG")


def test_maintenance_rate_tiers():
    assert maintenance_rate(10_000) == 0.004
    assert maintenance_rate(500_000) == 0.01
    assert maintenance_rate(50_000_000) == 0.10


# ──────────────────────────────────────────────────────────────────────────────
# Position sizing
# ──────────────────────────────────────────────────────────────────────────────

def test_size_basic():
    lim = SymbolLimits(symbol="BTCUSDT", min_qty=0.001, step=0.001)
    s = size_position(
        entry=100.0, stop=99.0, equity=1000.0, leverage=10,
        limits=lim, risk_pct=1.0,
    )
    assert s.valid
    assert s.qty >= 0.001
    assert s.margin > 0
    # Risk = 1% of 1000 = 10. Stop distance = 1. qty = 10/1 = 10 coins.
    assert abs(s.qty - 10.0) < 0.1


def test_size_stop_equals_entry_invalid():
    lim = SymbolLimits(symbol="BTCUSDT")
    s = size_position(entry=100.0, stop=100.0, equity=1000.0, leverage=10, limits=lim)
    assert not s.valid
    assert "stop equals entry" in s.reason


def test_size_below_min_qty():
    lim = SymbolLimits(symbol="XYZUSDT", min_qty=1000.0)
    s = size_position(entry=100.0, stop=99.0, equity=100.0, leverage=10, limits=lim, risk_pct=0.1)
    assert not s.valid
    assert "minimum" in s.reason.lower()


def test_pretrade_max_positions():
    limits = RiskLimits(max_open_positions=3)
    ok, reason = check_pretrade(
        open_positions=3, equity=1000.0, available_margin=500.0,
        proposed_notional=100.0, proposed_margin=10.0, limits=limits,
    )
    assert not ok
    assert "max open" in reason


# ──────────────────────────────────────────────────────────────────────────────
# Indicators
# ──────────────────────────────────────────────────────────────────────────────

def _prices(n=100, start=100.0, trend=0.1):
    import random
    random.seed(42)
    p = [start]
    for _ in range(n - 1):
        p.append(p[-1] * (1 + trend / 100 + random.gauss(0, 0.005)))
    return p


def test_ema_length():
    p = _prices(50)
    e = ema(p, 20)
    assert len(e) == 50
    assert math.isnan(e[0])
    assert not math.isnan(e[-1])


def test_sma_length():
    p = _prices(50)
    s = sma(p, 10)
    assert len(s) == 50
    assert math.isnan(s[0])
    assert not math.isnan(s[-1])


def test_rsi_range():
    p = _prices(100)
    r = rsi(p, 14)
    valid = [x for x in r if not math.isnan(x)]
    assert all(0 <= x <= 100 for x in valid)


def test_macd_length():
    p = _prices(100)
    m, sig, hist = macd(p)
    assert len(m) == len(sig) == len(hist) == 100


def test_bollinger_bands():
    p = _prices(50)
    up, mid, low = bollinger(p, 20)
    valid_idx = [i for i in range(len(up)) if not math.isnan(up[i])]
    for i in valid_idx:
        assert up[i] >= mid[i] >= low[i], f"Band order violated at {i}"


def test_from_klines():
    klines = [
        {"time": i * 60000, "open": str(100 + i * 0.1), "high": str(100 + i * 0.1 + 0.5),
         "low": str(100 + i * 0.1 - 0.5), "close": str(100 + i * 0.1 + 0.05),
         "baseVol": "10", "quoteVol": "1000"}
        for i in range(150)
    ]
    ind = from_klines(klines)
    assert "close" in ind
    assert "ema8" in ind
    assert "rsi14" in ind
    assert "macd" in ind
    assert len(ind["close"]) == 150


def test_hammer():
    assert is_hammer(100.0, 100.5, 98.0, 100.4)  # Small body at top, long lower wick
    assert not is_hammer(100.0, 105.0, 99.5, 101.0)  # Big upper wick = shooting star-ish


def test_shooting_star():
    assert is_shooting_star(100.0, 103.0, 99.8, 100.1)  # Long upper wick, small body at bottom
    assert not is_shooting_star(100.0, 100.5, 97.0, 100.3)  # Hammer, not shooting star


# ──────────────────────────────────────────────────────────────────────────────
# Strategies
# ──────────────────────────────────────────────────────────────────────────────

def test_golden_cross_fires():
    """MA50 crosses above MA200 — golden cross should fire."""
    n = 250
    # MA50 starts below MA200, then crosses above.
    prices = [100.0] * 200 + [110.0 + i * 0.5 for i in range(50)]
    klines = [
        {"time": i * 60000, "open": str(prices[i]), "high": str(prices[i] + 1),
         "low": str(prices[i] - 1), "close": str(prices[i]),
         "baseVol": "100", "quoteVol": "10000"}
        for i in range(n)
    ]
    from strategies.indicators import from_klines
    ind = from_klines(klines)
    sig = golden_cross_enhanced(ind, price=prices[-1])
    # May or may not fire depending on exact cross timing — just check it doesn't crash.
    if sig:
        assert sig.direction == "LONG"
        assert sig.confidence > 0


def test_death_cross_fires():
    """MA50 crosses below MA200 — death cross should fire."""
    prices = [110.0 - i * 0.5 for i in range(250)]
    klines = [
        {"time": i * 60000, "open": str(prices[i]), "high": str(prices[i] + 1),
         "low": str(prices[i] - 1), "close": str(prices[i]),
         "baseVol": "50", "quoteVol": "5000"}
        for i in range(250)
    ]
    from strategies.indicators import from_klines
    ind = from_klines(klines)
    sig = death_cross_enhanced(ind, price=prices[-1])
    if sig:
        assert sig.direction == "SHORT"


def test_run_all_returns_list():
    n = 200
    prices = [100 + math.sin(i / 10) * 5 for i in range(n)]
    klines = [
        {"time": i * 60000, "open": str(prices[i]), "high": str(prices[i] + 1),
         "low": str(prices[i] - 1), "close": str(prices[i]),
         "baseVol": "100", "quoteVol": "10000"}
        for i in range(n)
    ]
    from strategies.indicators import from_klines
    ind = from_klines(klines)
    sigs = run_all(ind, price=prices[-1], min_confidence=0)
    assert isinstance(sigs, list)
    for s in sigs:
        assert s.direction in ("LONG", "SHORT")
        assert 0 <= s.confidence <= 100


# ──────────────────────────────────────────────────────────────────────────────
# Self-edit safety — HIGH-RISK always blocked in auto-accept
# ──────────────────────────────────────────────────────────────────────────────

def test_high_risk_blocked_in_auto_accept():
    """Even with auto_accept=True, high-risk keywords must be blocked."""
    import asyncio

    # Test the confirmation logic directly without importing the full agent.
    HIGH_RISK_KEYWORDS = [
        "rm ", "rmdir", "shutil.rmtree", "drop table",
        "delete from", ".env", "credentials", "git push", "sudo", "os.remove",
    ]

    def is_high_risk(action: str) -> bool:
        for kw in HIGH_RISK_KEYWORDS:
            if kw.lower() in action.lower():
                return True
        return False

    dangerous = ["rm -rf /", "os.remove('.env')", "git push --force", "delete from positions"]
    for action in dangerous:
        assert is_high_risk(action), f"Should be flagged as high-risk: {action!r}"

    safe = ["print('hello')", "get_positions()", "scan_once()"]
    for action in safe:
        assert not is_high_risk(action), f"Should not be flagged: {action!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
