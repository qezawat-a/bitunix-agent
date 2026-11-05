"""Trading strategy signals.

Each strategy is a function that takes a dict of indicator arrays (from
indicators.from_klines) and returns a Signal with direction, confidence 0-100,
and a human-readable reason. Returns None if conditions aren't met.

All 16 user-requested strategies are implemented here. The scanner calls
every strategy on each timeframe's data and aggregates results.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Signal:
    """A trading signal from a single strategy."""

    strategy: str
    direction: str  # "LONG" | "SHORT"
    confidence: float  # 0-100
    reason: str
    timeframe: str = ""
    symbol: str = ""
    price: float = 0.0
    # Suggested TP/SL distances as % from entry (engine computes exact prices).
    tp_pct: float = 0.0
    sl_pct: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def side(self) -> str:
        return "BUY" if self.is_long else "SELL"

    def __repr__(self) -> str:
        return f"Signal({self.strategy} {self.direction} {self.confidence:.0f}% {self.reason[:60]})"


def _nan(*vals: float) -> bool:
    return any(math.isnan(v) for v in vals)


def _v(arr, idx: int = -1) -> float:
    """Safe array value — returns nan for out-of-range or nan entries."""
    try:
        v = float(arr[idx])
        return v
    except (IndexError, TypeError, ValueError):
        return float("nan")


# ------------------------------------------------------------------ #
# 1. Classic strategies                                               #
# ------------------------------------------------------------------ #

def vegas_tunnel_bearish(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Vegas Tunnel Trend-Confirmed Bearish.

    Death cross: EMA8 crosses below EMA21 while both are below the Vegas
    tunnel (EMA144-EMA169). Tunnel-down confirms downtrend.
    """
    e8 = ind.get("ema8")
    e21 = ind.get("ema21")
    e144 = ind.get("ema144")
    e169 = ind.get("ema169")
    if e8 is None or e21 is None:
        return None

    c0 = _v(e8); p0 = _v(e21)
    c1 = _v(e8, -2); p1 = _v(e21, -2)
    t_low = _v(e144); t_high = _v(e169)

    if _nan(c0, p0, c1, p1, t_low, t_high):
        return None

    cross_down = c1 >= p1 and c0 < p0
    tunnel_down = t_low < t_high  # inverted (144 < 169) means bearish tunnel
    price_below_tunnel = (price or _v(ind.get("close", []), -1)) < min(t_low, t_high)

    if not (cross_down and tunnel_down):
        return None

    confidence = 65.0
    if price_below_tunnel:
        confidence += 15.0

    return Signal(
        strategy="vegas_tunnel_bearish",
        direction="SHORT",
        confidence=min(confidence, 95),
        reason=f"EMA8/21 death cross below Vegas tunnel (e8={c0:.2f} e21={p0:.2f})",
        price=price,
    )


def vegas_tunnel_bullish(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Vegas Tunnel Trend-Confirmed Bullish.

    Golden cross: EMA8 crosses above EMA21 while both are above the Vegas
    tunnel and the tunnel is sloping up (EMA144 > EMA169).
    """
    e8 = ind.get("ema8")
    e21 = ind.get("ema21")
    e144 = ind.get("ema144")
    e169 = ind.get("ema169")
    if e8 is None or e21 is None:
        return None

    c0 = _v(e8); p0 = _v(e21)
    c1 = _v(e8, -2); p1 = _v(e21, -2)
    t_high = _v(e144); t_low = _v(e169)

    if _nan(c0, p0, c1, p1, t_high, t_low):
        return None

    cross_up = c1 <= p1 and c0 > p0
    tunnel_up = t_high > t_low
    price_above = (price or _v(ind.get("close", []), -1)) > max(t_high, t_low)

    if not (cross_up and tunnel_up):
        return None

    confidence = 65.0
    if price_above:
        confidence += 15.0

    return Signal(
        strategy="vegas_tunnel_bullish",
        direction="LONG",
        confidence=min(confidence, 95),
        reason=f"EMA8/21 golden cross above Vegas tunnel (e8={c0:.2f} e21={p0:.2f})",
        price=price,
    )


def golden_cross_enhanced(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Golden Cross 50/200 Enhanced Long — MA cross with volume confirmation."""
    ma50 = ind.get("ma50")
    ma200 = ind.get("ma200")
    vol = ind.get("volume")
    if ma50 is None or ma200 is None:
        return None

    m50_0 = _v(ma50); m200_0 = _v(ma200)
    m50_1 = _v(ma50, -2); m200_1 = _v(ma200, -2)

    if _nan(m50_0, m200_0, m50_1, m200_1):
        return None

    cross_up = m50_1 <= m200_1 and m50_0 > m200_0
    if not cross_up:
        return None

    confidence = 70.0
    if vol is not None:
        avg_vol = float(np.nanmean(vol[-20:]))
        cur_vol = _v(vol)
        if not math.isnan(cur_vol) and avg_vol > 0 and cur_vol > avg_vol * 1.3:
            confidence += 15.0

    return Signal(
        strategy="golden_cross_enhanced",
        direction="LONG",
        confidence=min(confidence, 95),
        reason=f"MA50/200 golden cross (ma50={m50_0:.2f} ma200={m200_0:.2f})",
        price=price,
    )


def death_cross_enhanced(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Death Cross 50/200 Enhanced Short — MA cross with weak/bearish volume."""
    ma50 = ind.get("ma50")
    ma200 = ind.get("ma200")
    vol = ind.get("volume")
    if ma50 is None or ma200 is None:
        return None

    m50_0 = _v(ma50); m200_0 = _v(ma200)
    m50_1 = _v(ma50, -2); m200_1 = _v(ma200, -2)

    if _nan(m50_0, m200_0, m50_1, m200_1):
        return None

    cross_down = m50_1 >= m200_1 and m50_0 < m200_0
    if not cross_down:
        return None

    confidence = 68.0
    if vol is not None:
        avg_vol = float(np.nanmean(vol[-20:]))
        cur_vol = _v(vol)
        if not math.isnan(cur_vol) and avg_vol > 0 and cur_vol < avg_vol * 0.8:
            confidence += 12.0

    return Signal(
        strategy="death_cross_enhanced",
        direction="SHORT",
        confidence=min(confidence, 95),
        reason=f"MA50/200 death cross (ma50={m50_0:.2f} ma200={m200_0:.2f})",
        price=price,
    )


# ------------------------------------------------------------------ #
# 2. Risk strategy                                                    #
# ------------------------------------------------------------------ #

def vwap_breakdown_alert(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """VWAP Breakdown Alert — intraday risk (short signal below VWAP with MACD confirm)."""
    vwap_arr = ind.get("vwap")
    macd_h = ind.get("macd_hist")
    macd_l = ind.get("macd")
    macd_s = ind.get("macd_sig")
    close = ind.get("close")
    if vwap_arr is None or close is None:
        return None

    v = _v(vwap_arr); c = _v(close); hist = _v(macd_h) if macd_h is not None else float("nan")
    hist_prev = _v(macd_h, -2) if macd_h is not None else float("nan")

    if _nan(v, c):
        return None

    below_vwap = c < v
    macd_turning_down = (not math.isnan(hist) and not math.isnan(hist_prev)
                         and hist < 0 and hist < hist_prev)

    if not below_vwap:
        return None

    confidence = 60.0
    if macd_turning_down:
        confidence += 20.0

    return Signal(
        strategy="vwap_breakdown_alert",
        direction="SHORT",
        confidence=min(confidence, 90),
        reason=f"Price {c:.2f} below VWAP {v:.2f}" + (" + MACD turning down" if macd_turning_down else ""),
        price=price or c,
    )


# ------------------------------------------------------------------ #
# 3. Reversal strategies                                              #
# ------------------------------------------------------------------ #

def hammer_oversold_bounce(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Hammer Oversold Bounce — hammer candle at oversold RSI with EMA support."""
    o = ind.get("open"); h = ind.get("high"); l = ind.get("low"); c = ind.get("close")
    rsi_arr = ind.get("rsi14"); ema_arr = ind.get("ema21")
    if o is None or h is None or l is None or c is None:
        return None

    from .indicators import is_hammer
    o0, h0, l0, c0 = _v(o), _v(h), _v(l), _v(c)
    if _nan(o0, h0, l0, c0):
        return None

    rsi_val = _v(rsi_arr) if rsi_arr is not None else float("nan")
    ema_val = _v(ema_arr) if ema_arr is not None else float("nan")

    hammer = is_hammer(o0, h0, l0, c0)
    oversold = not math.isnan(rsi_val) and rsi_val < 35
    near_ema = not math.isnan(ema_val) and abs(c0 - ema_val) / ema_val < 0.005

    if not hammer:
        return None

    confidence = 60.0
    if oversold:
        confidence += 20.0
    if near_ema:
        confidence += 10.0

    return Signal(
        strategy="hammer_oversold_bounce",
        direction="LONG",
        confidence=min(confidence, 95),
        reason=f"Hammer at oversold (RSI={rsi_val:.1f})" if not math.isnan(rsi_val) else "Hammer candle",
        price=price or c0,
    )


def morning_star_divergence(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Morning Star Bullish Divergence — 3-bar reversal + MACD divergence."""
    o = ind.get("open"); h = ind.get("high"); l = ind.get("low"); c = ind.get("close")
    macd_h = ind.get("macd_hist")
    if o is None or h is None or l is None or c is None:
        return None

    from .indicators import is_morning_star
    bars = [(_v(o, i), _v(h, i), _v(l, i), _v(c, i)) for i in (-3, -2, -1)]
    if any(_nan(b[0], b[1], b[2], b[3]) for b in bars):
        return None

    if not is_morning_star(bars):
        return None

    confidence = 68.0
    if macd_h is not None:
        h0, h3 = _v(macd_h), _v(macd_h, -3)
        if not _nan(h0, h3) and h0 > h3 and c[-1] < c[-3]:  # price lower, hist higher = divergence
            confidence += 15.0

    return Signal(
        strategy="morning_star_divergence",
        direction="LONG",
        confidence=min(confidence, 95),
        reason="Morning Star 3-bar reversal pattern" + (" + MACD divergence" if confidence > 68 else ""),
        price=price or _v(c),
    )


def shooting_star_overbought(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Shooting Star Overbought Alert — shooting star at RSI overbought."""
    o = ind.get("open"); h = ind.get("high"); l = ind.get("low"); c = ind.get("close")
    rsi_arr = ind.get("rsi14"); ema_arr = ind.get("ema21")
    if o is None or h is None or l is None or c is None:
        return None

    from .indicators import is_shooting_star
    o0, h0, l0, c0 = _v(o), _v(h), _v(l), _v(c)
    if _nan(o0, h0, l0, c0):
        return None

    ss = is_shooting_star(o0, h0, l0, c0)
    rsi_val = _v(rsi_arr) if rsi_arr is not None else float("nan")
    overbought = not math.isnan(rsi_val) and rsi_val > 65

    if not ss:
        return None

    confidence = 60.0
    if overbought:
        confidence += 20.0

    return Signal(
        strategy="shooting_star_overbought",
        direction="SHORT",
        confidence=min(confidence, 92),
        reason=f"Shooting star at overbought (RSI={rsi_val:.1f})" if not math.isnan(rsi_val) else "Shooting star",
        price=price or c0,
    )


def dark_cloud_cover_death(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Dark Cloud Cover Death Cross — bearish top with MACD crossing down."""
    o = ind.get("open"); h = ind.get("high"); l = ind.get("low"); c = ind.get("close")
    macd_l = ind.get("macd"); macd_s = ind.get("macd_sig")
    if o is None or h is None or l is None or c is None:
        return None

    from .indicators import is_dark_cloud_cover
    bars = [(_v(o, i), _v(h, i), _v(l, i), _v(c, i)) for i in (-2, -1)]
    if any(_nan(b[0], b[1], b[2], b[3]) for b in bars):
        return None

    if not is_dark_cloud_cover(bars):
        return None

    confidence = 65.0
    if macd_l is not None and macd_s is not None:
        m0, s0 = _v(macd_l), _v(macd_s)
        m1, s1 = _v(macd_l, -2), _v(macd_s, -2)
        if not _nan(m0, s0, m1, s1) and m1 > s1 and m0 < s0:
            confidence += 18.0

    return Signal(
        strategy="dark_cloud_cover_death",
        direction="SHORT",
        confidence=min(confidence, 93),
        reason="Dark cloud cover" + (" + MACD death cross" if confidence > 65 else ""),
        price=price or _v(c),
    )


# ------------------------------------------------------------------ #
# 4. Breakout strategies                                              #
# ------------------------------------------------------------------ #

def bollinger_upper_breakout(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Bollinger Upper Band Breakout — breakout with RSI momentum."""
    bb_up = ind.get("bb_upper"); close = ind.get("close"); rsi_arr = ind.get("rsi14")
    if bb_up is None or close is None:
        return None

    c0, bb0 = _v(close), _v(bb_up)
    c1, bb1 = _v(close, -2), _v(bb_up, -2)
    rsi_val = _v(rsi_arr) if rsi_arr is not None else float("nan")

    if _nan(c0, bb0, c1, bb1):
        return None

    breakout = c1 <= bb1 and c0 > bb0
    if not breakout:
        return None

    confidence = 65.0
    if not math.isnan(rsi_val) and 50 < rsi_val < 80:
        confidence += 15.0
    elif not math.isnan(rsi_val) and rsi_val >= 80:
        confidence -= 10.0  # overbought reduces confidence

    return Signal(
        strategy="bollinger_upper_breakout",
        direction="LONG",
        confidence=max(40, min(confidence, 90)),
        reason=f"Price broke above BB upper (BB={bb0:.2f} RSI={rsi_val:.1f})",
        price=price or c0,
    )


def midband_macd_cross(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Mid-Band MACD Cross — bullish MACD cross while price above BB midband."""
    bb_mid = ind.get("bb_mid"); close = ind.get("close")
    macd_l = ind.get("macd"); macd_s = ind.get("macd_sig"); rsi_arr = ind.get("rsi14")
    if bb_mid is None or close is None or macd_l is None or macd_s is None:
        return None

    c0, m0 = _v(close), _v(macd_l)
    mid0, s0 = _v(bb_mid), _v(macd_s)
    m1, s1 = _v(macd_l, -2), _v(macd_s, -2)

    if _nan(c0, m0, mid0, s0, m1, s1):
        return None

    macd_cross_up = m1 <= s1 and m0 > s0
    above_mid = c0 > mid0

    if not (macd_cross_up and above_mid):
        return None

    confidence = 68.0
    rsi_val = _v(rsi_arr) if rsi_arr is not None else float("nan")
    if not math.isnan(rsi_val) and 45 < rsi_val < 70:
        confidence += 12.0

    return Signal(
        strategy="midband_macd_cross",
        direction="LONG",
        confidence=min(confidence, 92),
        reason=f"MACD bullish cross above BB midband (price={c0:.2f} mid={mid0:.2f})",
        price=price or c0,
    )


# ------------------------------------------------------------------ #
# 5. Trend strategies                                                 #
# ------------------------------------------------------------------ #

def ma200_macd_confirm(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """MA200 Cross MACD Confirm — major trend turn bullish."""
    ma200 = ind.get("ma200"); close = ind.get("close")
    macd_l = ind.get("macd"); macd_s = ind.get("macd_sig")
    if ma200 is None or close is None:
        return None

    c0, c1 = _v(close), _v(close, -2)
    m200_0, m200_1 = _v(ma200), _v(ma200, -2)

    if _nan(c0, c1, m200_0, m200_1):
        return None

    cross_above_200 = c1 <= m200_1 and c0 > m200_0
    if not cross_above_200:
        return None

    confidence = 72.0
    if macd_l is not None and macd_s is not None:
        m0, s0 = _v(macd_l), _v(macd_s)
        if not _nan(m0, s0) and m0 > s0:
            confidence += 15.0

    return Signal(
        strategy="ma200_macd_confirm",
        direction="LONG",
        confidence=min(confidence, 95),
        reason=f"Price crossed above MA200 ({m200_0:.2f})" + (" + MACD bullish" if confidence > 72 else ""),
        price=price or c0,
    )


def tema_cross_vwap(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """TEMA Cross Above VWAP Confirm — intraday bullish alignment."""
    tema_arr = ind.get("tema21"); vwap_arr = ind.get("vwap"); close = ind.get("close")
    if tema_arr is None or vwap_arr is None or close is None:
        return None

    t0, t1 = _v(tema_arr), _v(tema_arr, -2)
    v0 = _v(vwap_arr)
    c0 = _v(close)

    if _nan(t0, t1, v0, c0):
        return None

    tema_above_vwap = t0 > v0 and t1 <= v0
    price_above_vwap = c0 > v0

    if not (tema_above_vwap and price_above_vwap):
        return None

    return Signal(
        strategy="tema_cross_vwap",
        direction="LONG",
        confidence=72.0,
        reason=f"TEMA21 crossed above VWAP ({v0:.2f}) with price confirmation",
        price=price or c0,
    )


def dema55_bullish_trend(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """DEMA55 Bullish Trend Follow — mid-term momentum with MACD."""
    dema_arr = ind.get("dema55"); close = ind.get("close")
    macd_l = ind.get("macd"); macd_h = ind.get("macd_hist")
    if dema_arr is None or close is None:
        return None

    d0, d1 = _v(dema_arr), _v(dema_arr, -2)
    c0 = _v(close)

    if _nan(d0, d1, c0):
        return None

    dema_rising = d0 > d1
    price_above = c0 > d0

    if not (dema_rising and price_above):
        return None

    confidence = 65.0
    if macd_h is not None:
        h0 = _v(macd_h)
        if not math.isnan(h0) and h0 > 0:
            confidence += 15.0

    return Signal(
        strategy="dema55_bullish_trend",
        direction="LONG",
        confidence=min(confidence, 90),
        reason=f"Price above rising DEMA55 ({d0:.2f})" + (" + MACD positive" if confidence > 65 else ""),
        price=price or c0,
    )


def vwap_bullish_confirm(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """VWAP Bullish Confirmation — intraday price/MACD above VWAP."""
    vwap_arr = ind.get("vwap"); close = ind.get("close")
    macd_l = ind.get("macd"); macd_s = ind.get("macd_sig")
    if vwap_arr is None or close is None:
        return None

    v0, c0 = _v(vwap_arr), _v(close)
    if _nan(v0, c0):
        return None

    above_vwap = c0 > v0
    if not above_vwap:
        return None

    confidence = 60.0
    if macd_l is not None and macd_s is not None:
        m0, s0 = _v(macd_l), _v(macd_s)
        if not _nan(m0, s0) and m0 > s0:
            confidence += 15.0

    return Signal(
        strategy="vwap_bullish_confirm",
        direction="LONG",
        confidence=min(confidence, 88),
        reason=f"Price {c0:.2f} above VWAP {v0:.2f}" + (" + MACD bullish" if confidence > 60 else ""),
        price=price or c0,
    )


def three_white_soldiers_trend(ind: dict, price: float = 0.0) -> Optional[Signal]:
    """Three White Soldiers Trend Confirm — continuation with RSI and EMA."""
    o = ind.get("open"); h = ind.get("high"); l = ind.get("low"); c = ind.get("close")
    rsi_arr = ind.get("rsi14"); ema_arr = ind.get("ema21")
    if o is None or h is None or l is None or c is None:
        return None

    from .indicators import three_white_soldiers
    bars = [(_v(o, i), _v(h, i), _v(l, i), _v(c, i)) for i in (-3, -2, -1)]
    if any(_nan(b[0], b[1], b[2], b[3]) for b in bars):
        return None

    if not three_white_soldiers(bars):
        return None

    confidence = 70.0
    rsi_val = _v(rsi_arr) if rsi_arr is not None else float("nan")
    ema_val = _v(ema_arr) if ema_arr is not None else float("nan")
    c0 = _v(c)

    if not math.isnan(rsi_val) and 50 < rsi_val < 75:
        confidence += 12.0
    if not math.isnan(ema_val) and not math.isnan(c0) and c0 > ema_val:
        confidence += 8.0

    return Signal(
        strategy="three_white_soldiers_trend",
        direction="LONG",
        confidence=min(confidence, 93),
        reason="Three White Soldiers continuation pattern",
        price=price or c0,
    )


# ------------------------------------------------------------------ #
# Registry — all strategies                                           #
# ------------------------------------------------------------------ #

ALL_STRATEGIES = {
    "vegas_tunnel_bearish": vegas_tunnel_bearish,
    "vegas_tunnel_bullish": vegas_tunnel_bullish,
    "golden_cross_enhanced": golden_cross_enhanced,
    "death_cross_enhanced": death_cross_enhanced,
    "vwap_breakdown_alert": vwap_breakdown_alert,
    "hammer_oversold_bounce": hammer_oversold_bounce,
    "morning_star_divergence": morning_star_divergence,
    "shooting_star_overbought": shooting_star_overbought,
    "dark_cloud_cover_death": dark_cloud_cover_death,
    "bollinger_upper_breakout": bollinger_upper_breakout,
    "midband_macd_cross": midband_macd_cross,
    "ma200_macd_confirm": ma200_macd_confirm,
    "tema_cross_vwap": tema_cross_vwap,
    "dema55_bullish_trend": dema55_bullish_trend,
    "vwap_bullish_confirm": vwap_bullish_confirm,
    "three_white_soldiers_trend": three_white_soldiers_trend,
}

LONG_STRATEGIES = {k: v for k, v in ALL_STRATEGIES.items()
                   if k not in {"vegas_tunnel_bearish", "death_cross_enhanced",
                                "vwap_breakdown_alert", "shooting_star_overbought",
                                "dark_cloud_cover_death"}}

SHORT_STRATEGIES = {k: v for k, v in ALL_STRATEGIES.items()
                    if k not in LONG_STRATEGIES}


def run_all(
    ind: dict,
    price: float = 0.0,
    enabled: Optional[set] = None,
    min_confidence: float = 60.0,
) -> list[Signal]:
    """Run all enabled strategies and return qualifying signals."""
    signals = []
    for name, fn in ALL_STRATEGIES.items():
        if enabled is not None and name not in enabled:
            continue
        try:
            sig = fn(ind, price)
        except Exception:
            continue
        if sig is not None and sig.confidence >= min_confidence:
            signals.append(sig)
    return signals
