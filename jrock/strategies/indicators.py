"""Technical indicators computed from OHLCV numpy arrays.

All functions take a numpy float64 array (or list that will be converted) and
return a same-length array with np.nan prepended for the warmup period. This
makes indexing simple: indicators[-1] is always the current bar.

Performance: pure numpy, no C extensions required. Adequate for the scan
intervals used here (1m-15m bars, up to 200 candles per call).
"""
from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


Array = NDArray[np.float64]


def _arr(x: list | Array) -> Array:
    return np.asarray(x, dtype=np.float64)


def ema(src: list | Array, period: int) -> Array:
    """Exponential moving average. First value = SMA seed."""
    a = _arr(src)
    n = len(a)
    out = np.full(n, np.nan)
    if n < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = float(np.nanmean(a[:period]))
    for i in range(period, n):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


def sma(src: list | Array, period: int) -> Array:
    a = _arr(src)
    n = len(a)
    out = np.full(n, np.nan)
    for i in range(period - 1, n):
        out[i] = float(np.nanmean(a[i - period + 1 : i + 1]))
    return out


def dema(src: list | Array, period: int) -> Array:
    """Double EMA: 2*EMA(src,n) - EMA(EMA(src,n),n)."""
    e1 = ema(src, period)
    e2 = ema(e1, period)
    return 2 * e1 - e2


def tema(src: list | Array, period: int) -> Array:
    """Triple EMA: 3*EMA - 3*EMA(EMA) + EMA(EMA(EMA))."""
    e1 = ema(src, period)
    e2 = ema(e1, period)
    e3 = ema(e2, period)
    return 3 * e1 - 3 * e2 + e3


def rsi(src: list | Array, period: int = 14) -> Array:
    """Wilder RSI (smoothed moving average of gains/losses)."""
    a = _arr(src)
    n = len(a)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    delta = np.diff(a)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100 - 100 / (1 + rs)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def macd(
    src: list | Array,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[Array, Array, Array]:
    """MACD line, signal line, histogram. Returns (macd, signal, hist)."""
    e_fast = ema(src, fast)
    e_slow = ema(src, slow)
    m = e_fast - e_slow
    sig = ema(m, signal_period)
    hist = m - sig
    return m, sig, hist


def bollinger(
    src: list | Array,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[Array, Array, Array]:
    """Bollinger Bands: (upper, mid, lower)."""
    a = _arr(src)
    mid = sma(a, period)
    n = len(a)
    std = np.full(n, np.nan)
    for i in range(period - 1, n):
        std[i] = float(np.std(a[i - period + 1 : i + 1], ddof=0))
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def vwap(
    high: list | Array,
    low: list | Array,
    close: list | Array,
    volume: list | Array,
) -> Array:
    """Session VWAP (running cumulative from first bar)."""
    h, l, c, v = _arr(high), _arr(low), _arr(close), _arr(volume)
    tp = (h + l + c) / 3.0
    cum_tpv = np.cumsum(tp * v)
    cum_vol = np.cumsum(v)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(cum_vol > 0, cum_tpv / cum_vol, np.nan)


def atr(
    high: list | Array,
    low: list | Array,
    close: list | Array,
    period: int = 14,
) -> Array:
    """Average True Range."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    n = len(h)
    tr = np.full(n, np.nan)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return ema(tr, period)


def stoch_rsi(src: list | Array, rsi_period: int = 14, stoch_period: int = 14) -> tuple[Array, Array]:
    """Stochastic RSI — returns (%K, %D)."""
    r = rsi(src, rsi_period)
    n = len(r)
    k = np.full(n, np.nan)
    for i in range(stoch_period - 1, n):
        window = r[i - stoch_period + 1 : i + 1]
        valid = window[~np.isnan(window)]
        if len(valid) < stoch_period:
            continue
        lo, hi = valid.min(), valid.max()
        if hi == lo:
            k[i] = 50.0
        else:
            k[i] = (r[i] - lo) / (hi - lo) * 100
    d = sma(k, 3)
    return k, d


# ------------------------------------------------------------------ #
# Candlestick pattern detectors                                       #
# ------------------------------------------------------------------ #

def _body(o: float, c: float) -> float:
    return abs(c - o)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, l: float, c: float) -> float:
    return min(o, c) - l


def _full_range(o: float, h: float, l: float, c: float) -> float:
    return h - l if h != l else 1e-10


def is_hammer(o: float, h: float, l: float, c: float) -> bool:
    """Hammer candle: small body at top, long lower wick."""
    rng = _full_range(o, h, l, c)
    body = _body(o, c)
    lower = _lower_wick(o, l, c)
    upper = _upper_wick(o, h, c)
    return (
        body < rng * 0.35
        and lower >= rng * 0.55
        and upper < rng * 0.1
    )


def is_shooting_star(o: float, h: float, l: float, c: float) -> bool:
    """Shooting star: small body at bottom, long upper wick."""
    rng = _full_range(o, h, l, c)
    body = _body(o, c)
    lower = _lower_wick(o, l, c)
    upper = _upper_wick(o, h, c)
    return (
        body < rng * 0.35
        and upper >= rng * 0.55
        and lower < rng * 0.1
    )


def is_morning_star(
    bars: list[tuple[float, float, float, float]],
) -> bool:
    """Morning star (3-bar): big bearish, small body, big bullish."""
    if len(bars) < 3:
        return False
    o1, h1, l1, c1 = bars[-3]
    o2, h2, l2, c2 = bars[-2]
    o3, h3, l3, c3 = bars[-1]
    big1 = o1 > c1 and _body(o1, c1) > _full_range(o1, h1, l1, c1) * 0.6
    small2 = _body(o2, c2) < _full_range(o1, h1, l1, c1) * 0.3
    big3 = c3 > o3 and _body(o3, c3) > _full_range(o3, h3, l3, c3) * 0.6
    gap_down = max(o2, c2) < c1
    return big1 and small2 and big3 and gap_down


def is_dark_cloud_cover(
    bars: list[tuple[float, float, float, float]],
) -> bool:
    """Dark cloud cover (2-bar): bullish then bearish closing below midpoint."""
    if len(bars) < 2:
        return False
    o1, h1, l1, c1 = bars[-2]
    o2, h2, l2, c2 = bars[-1]
    bullish1 = c1 > o1 and _body(o1, c1) > _full_range(o1, h1, l1, c1) * 0.5
    bearish2 = c2 < o2 and o2 > c1 and c2 < (o1 + c1) / 2
    return bullish1 and bearish2


def three_white_soldiers(
    bars: list[tuple[float, float, float, float]],
) -> bool:
    """Three White Soldiers: 3 consecutive bullish candles with higher closes."""
    if len(bars) < 3:
        return False
    results = []
    for i in (-3, -2, -1):
        o, h, l, c = bars[i]
        results.append(c > o and _body(o, c) > _full_range(o, h, l, c) * 0.5)
    closes = [b[3] for b in bars[-3:]]
    return all(results) and closes[1] > closes[0] and closes[2] > closes[1]


# ------------------------------------------------------------------ #
# Convenience: compute all indicators from a list of kline dicts     #
# ------------------------------------------------------------------ #

def from_klines(klines: list[dict]) -> dict[str, Array]:
    """Parse kline dicts and compute all indicators.

    kline dict fields: open, high, low, close, quoteVol (or volume), baseVol, time
    Returns a dict of named arrays, all same length as klines.
    """
    if not klines:
        return {}

    klines = sorted(klines, key=lambda k: int(k.get("time", 0)))
    opens = [float(k["open"]) for k in klines]
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    closes = [float(k["close"]) for k in klines]
    vols = [float(k.get("baseVol", k.get("volume", 0))) for k in klines]

    c = _arr(closes)
    h = _arr(highs)
    l = _arr(lows)

    ema8 = ema(closes, 8)
    ema12 = ema(closes, 12)
    ema21 = ema(closes, 21)
    ema144 = ema(closes, 144)
    ema169 = ema(closes, 169)
    ma50 = sma(closes, 50)
    ma200 = sma(closes, 200)
    dema55 = dema(closes, 55)
    tema_val = tema(closes, 21)
    rsi14 = rsi(closes, 14)
    macd_l, macd_sig, macd_hist = macd(closes)
    bb_up, bb_mid, bb_low = bollinger(closes, 20, 2.0)
    vwap_val = vwap(highs, lows, closes, vols)
    atr14 = atr(highs, lows, closes, 14)

    return {
        "open": _arr(opens),
        "high": h,
        "low": l,
        "close": c,
        "volume": _arr(vols),
        "ema8": ema8,
        "ema12": ema12,
        "ema21": ema21,
        "ema144": ema144,
        "ema169": ema169,
        "ma50": ma50,
        "ma200": ma200,
        "dema55": dema55,
        "tema21": tema_val,
        "rsi14": rsi14,
        "macd": macd_l,
        "macd_sig": macd_sig,
        "macd_hist": macd_hist,
        "bb_upper": bb_up,
        "bb_mid": bb_mid,
        "bb_lower": bb_low,
        "vwap": vwap_val,
        "atr14": atr14,
    }
