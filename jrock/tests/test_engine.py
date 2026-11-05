"""Engine-layer tests: scanner aggregation, TPSL calculation, sizing, settings.

These tests catch import/config bugs (broken relative paths, missing modules)
and validate the trading logic in isolation without hitting the exchange.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from jrock.engine.types import AggregatedSignal
from jrock.engine.settings import EngineSettings
from jrock.engine.scanner import Scanner
from jrock.engine.tpsl import compute_tpsl, TPSLPlan
from jrock.engine.ws_scanner import WsScanner
from jrock.strategies.signals import Signal, ALL_STRATEGIES
from jrock.strategies.indicators import from_klines
from jrock.adapters.ws_public import PublicWsClient, KlineEvent
from jrock.adapters.ws_private import PrivateWsClient


# ------------------------------------------------------------------ #
# AggregatedSignal                                                   #
# ------------------------------------------------------------------ #

def test_aggregated_signal_side_long():
    agg = AggregatedSignal(
        symbol="BTCUSDT", direction="LONG", confidence=80.0, agreeing=4,
        signals=[], price=50000.0,
    )
    assert agg.side == "BUY"


def test_aggregated_signal_side_short():
    agg = AggregatedSignal(
        symbol="BTCUSDT", direction="SHORT", confidence=80.0, agreeing=4,
        signals=[], price=50000.0,
    )
    assert agg.side == "SELL"


# ------------------------------------------------------------------ #
# TPSL calculation                                                   #
# ------------------------------------------------------------------ #

def make_signal(direction="LONG", confidence=80.0) -> AggregatedSignal:
    return AggregatedSignal(
        symbol="BTCUSDT", direction=direction, confidence=confidence,
        agreeing=3, signals=[], price=100.0,
    )


def test_tpsl_long():
    plan = compute_tpsl(
        make_signal("LONG", 80), entry=100.0, qty=1.0, leverage=10,
        settings=EngineSettings(),
    )
    assert plan.valid
    # SL below entry, TP above entry for LONG.
    assert plan.sl_price < plan.entry < plan.tp_price
    # liq at 10x ≈ 90.45, SL at 50% buffer ≈ 95.22 (halfway entry→liq).
    assert 94 < plan.sl_price < 96, f"SL={plan.sl_price}"
    # RR ratio ~2:1 at confidence 80.
    assert plan.rr_ratio >= 1.5


def test_tpsl_short():
    plan = compute_tpsl(
        make_signal("SHORT", 80), entry=100.0, qty=1.0, leverage=10,
        settings=EngineSettings(),
    )
    assert plan.valid
    # SL above entry, TP below entry for SHORT.
    assert plan.sl_price > plan.entry > plan.tp_price
    # liq at 10x ≈ 109.55, SL at 50% buffer ≈ 104.78 (halfway entry→liq).
    assert 104 < plan.sl_price < 106, f"SL={plan.sl_price}"


def test_tpsl_high_confidence_rr():
    """Higher confidence → wider TP (higher R/R)."""
    p_low = compute_tpsl(make_signal("LONG", 70), entry=100, qty=1, leverage=10,
                         settings=EngineSettings())
    p_high = compute_tpsl(make_signal("LONG", 90), entry=100, qty=1, leverage=10,
                          settings=EngineSettings())
    assert p_high.rr_ratio > p_low.rr_ratio


def test_tpsl_bad_entry():
    plan = compute_tpsl(make_signal("LONG"), entry=0, qty=1, leverage=10,
                        settings=EngineSettings())
    assert not plan.valid


# ------------------------------------------------------------------ #
# Settings                                                           #
# ------------------------------------------------------------------ #

def test_settings_defaults():
    s = EngineSettings()
    assert s.leverage == 10
    assert s.margin_mode == "ISOLATED"
    assert s.scan_enabled is False
    assert s.autotrade_enabled is False


def test_settings_update_types():
    s = EngineSettings()
    changed = s.update(leverage="20", scan_enabled="true", symbols="BTCUSDT,ETHUSDT")
    assert "leverage" in changed
    assert "scan_enabled" in changed
    assert "symbols" in changed
    assert s.leverage == 20
    assert s.scan_enabled is True
    assert s.symbols == ["BTCUSDT", "ETHUSDT"]


def test_settings_update_unknown_key_ignored():
    """Unknown keys are silently ignored (by design — robust against bad input)."""
    s = EngineSettings()
    before = s.summary()
    s.update(not_a_real_setting="x")
    assert s.summary() == before


def test_settings_summary_contains_leverage():
    s = EngineSettings()
    summary = s.summary()
    assert "leverage" in summary.lower()


# ------------------------------------------------------------------ #
# Scanner aggregation                                                #
# ------------------------------------------------------------------ #

def _agg_signal(direction: str, strategy: str, confidence: float = 80) -> Signal:
    return Signal(
        strategy=strategy, direction=direction, confidence=confidence,
        reason="test", timeframe="5m", symbol="BTCUSDT", price=100.0,
    )


class _FakeSettings:
    min_agreeing_strategies = 3
    min_confidence = 70.0
    tf_min_confidence = 60.0


def test_scanner_aggregates_longs():
    scanner = Scanner.__new__(Scanner)
    scanner._settings = _FakeSettings()
    longs = [
        _agg_signal("LONG", "vegas_tunnel_bullish"),
        _agg_signal("LONG", "golden_cross_enhanced"),
        _agg_signal("LONG", "ma200_macd_confirm"),
    ]
    agg = scanner._aggregate("BTCUSDT", longs)
    assert agg is not None
    assert agg.direction == "LONG"
    assert agg.agreeing == 3


def test_scanner_rejects_below_min_strategies():
    scanner = Scanner.__new__(Scanner)
    scanner._settings = _FakeSettings()
    sigs = [_agg_signal("LONG", "vegas_tunnel_bullish")]  # only 1 strategy
    agg = scanner._aggregate("BTCUSDT", sigs)
    assert agg is None  # min_agreeing_strategies=3


def test_scanner_rejects_below_min_confidence():
    scanner = Scanner.__new__(Scanner)
    scanner._settings = _FakeSettings()
    sigs = [
        _agg_signal("LONG", "vegas_tunnel_bullish", 50),
        _agg_signal("LONG", "golden_cross_enhanced", 50),
        _agg_signal("LONG", "death_cross_enhanced", 50),
    ]
    agg = scanner._aggregate("BTCUSDT", sigs)
    assert agg is None  # min_confidence=70


# ------------------------------------------------------------------ #
# All 16 strategies importable                                       #
# ------------------------------------------------------------------ #

def test_all_strategies_present():
    expected = {
        "vegas_tunnel_bearish", "vegas_tunnel_bullish",
        "golden_cross_enhanced", "death_cross_enhanced",
        "vwap_breakdown_alert", "hammer_oversold_bounce",
        "morning_star_divergence", "shooting_star_overbought",
        "dark_cloud_cover_death", "bollinger_upper_breakout",
        "midband_macd_cross", "ma200_macd_confirm",
        "tema_cross_vwap", "dema55_bullish_trend",
        "vwap_bullish_confirm", "three_white_soldiers_trend",
    }
    assert set(ALL_STRATEGIES.keys()) == expected


# ------------------------------------------------------------------ #
# WebSocket client instantiation (no connection)                     #
# ------------------------------------------------------------------ #

def test_ws_clients_instantiate():
    pub = PublicWsClient()
    priv = PrivateWsClient()
    assert pub._connected is False
    assert priv._authenticated is False
    assert len(pub._kline_cbs) == 0
    assert len(priv._position_cbs) == 0
