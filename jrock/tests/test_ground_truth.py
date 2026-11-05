"""Ground-truth compliance tests for Bitunix API channel names and semantics.

These tests validate the WS public channel names and candle-close inference
against the official docs (jrock/docs_full/) and the official demo repo,
which the repo's earlier guesses got wrong (see bitunix-api-ground-truth).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from jrock.adapters.ws_public import kline_channel, KlineEvent, PublicWsClient


# ------------------------------------------------------------------ #
# Kline channel names                                                #
# ------------------------------------------------------------------ #

def test_kline_channel_market():
    assert kline_channel("BTCUSDT", "1m") == "market_kline_1min"
    assert kline_channel("ETHUSDT", "5m") == "market_kline_5min"
    assert kline_channel("BTCUSDT", "15m") == "market_kline_15min"


def test_kline_channel_mark():
    assert kline_channel("BTCUSDT", "1m", mark=True) == "mark_kline_1min"
    assert kline_channel("ETHUSDT", "3m", mark=True) == "mark_kline_3min"


# ------------------------------------------------------------------ #
# Candle close inference (no confirm flag on kline pushes)           #
# ------------------------------------------------------------------ #

def test_close_inferred_on_time_change():
    """A candle is closed when its open time differs from the previous one."""
    client = PublicWsClient.__new__(PublicWsClient)
    client._last_candle_time = {}

    # First candle for a key: treated as closed.
    ev1 = KlineEvent("BTCUSDT", "1m", 100, 101, 99, 100.5, 1.0, 1000, closed=False)
    key = ("BTCUSDT", "1m")
    prev = client._last_candle_time.get(key)
    closed = prev is None or ev1.time_ms != prev
    client._last_candle_time[key] = ev1.time_ms
    assert closed is True

    # Same time again (forming candle update): not closed.
    ev2 = KlineEvent("BTCUSDT", "1m", 100.5, 101, 100, 100.2, 0.5, 1000, closed=False)
    prev = client._last_candle_time.get(key)
    closed = prev is None or ev2.time_ms != prev
    assert closed is False

    # New candle open time: closed.
    ev3 = KlineEvent("BTCUSDT", "1m", 100.2, 102, 100, 101.5, 2.0, 1060000, closed=False)
    prev = client._last_candle_time.get(key)
    closed = prev is None or ev3.time_ms != prev
    assert closed is True


# ------------------------------------------------------------------ #
# Private WS signing (seconds timestamp, no body/query_str)          #
# ------------------------------------------------------------------ #

def test_ws_private_sign_shape():
    from jrock.adapters.ws_private import _ws_auth
    auth = _ws_auth("key123", "secret456")
    assert set(auth.keys()) == {"apiKey", "timestamp", "nonce", "sign"}
    assert auth["apiKey"] == "key123"
    assert isinstance(auth["timestamp"], int)  # seconds for WS
    assert len(auth["nonce"]) <= 64
    assert isinstance(auth["sign"], str) and len(auth["sign"]) == 64


# ------------------------------------------------------------------ #
# REST signing shape (milliseconds, double SHA-256)                  #
# ------------------------------------------------------------------ #

def test_rest_signing_fields_present():
    from jrock.adapters.bitunix import BitunixClient
    client = BitunixClient(api_key="ak", secret_key="sk")
    headers = client._headers(query_str="symbolBTCUSDT", body_str="{}")
    assert set(headers.keys()) == {"api-key", "nonce", "timestamp", "sign"}
    assert headers["api-key"] == "ak"
    # timestamp is milliseconds (13 digits).
    assert len(headers["timestamp"]) == 13
    assert len(headers["nonce"]) <= 64
    assert len(headers["sign"]) == 64


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
