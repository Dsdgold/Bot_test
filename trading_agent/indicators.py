"""Technical indicators and deterministic regime classifier."""

from __future__ import annotations

import math
from typing import Sequence

from trading_agent.models import CandleData, Regime, RegimeState
from trading_agent import config


# ---------------------------------------------------------------------------
# Core indicator calculations
# ---------------------------------------------------------------------------

def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average."""
    if len(values) < period:
        return [values[-1]] * len(values) if values else []
    result = [0.0] * len(values)
    k = 2.0 / (period + 1)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def sma(values: Sequence[float], period: int) -> list[float]:
    """Simple moving average."""
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(sum(values[: i + 1]) / (i + 1))
        else:
            result.append(sum(values[i - period + 1 : i + 1]) / period)
    return result


def true_range(candles: Sequence[CandleData]) -> list[float]:
    """True Range for each candle."""
    tr = [candles[0].range_size]
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
    return tr


def atr(candles: Sequence[CandleData], period: int = 14) -> list[float]:
    """Average True Range."""
    tr = true_range(candles)
    return ema(tr, period)


def atr_percent(candles: Sequence[CandleData], period: int = 14) -> float:
    """ATR as percentage of current price."""
    if not candles:
        return 0.0
    atr_vals = atr(candles, period)
    price = candles[-1].close
    if price == 0:
        return 0.0
    return (atr_vals[-1] / price) * 100


def adx(candles: Sequence[CandleData], period: int = 14) -> float:
    """Average Directional Index."""
    if len(candles) < period + 1:
        return 0.0

    plus_dm = []
    minus_dm = []
    for i in range(1, len(candles)):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm.append(max(up_move, 0) if up_move > down_move else 0.0)
        minus_dm.append(max(down_move, 0) if down_move > up_move else 0.0)

    tr_vals = true_range(candles)
    # Use only tr_vals[1:] to match plus_dm/minus_dm length
    tr_vals = tr_vals[1:] if len(tr_vals) > len(plus_dm) else tr_vals

    smooth_tr = ema(tr_vals, period)
    smooth_plus = ema(plus_dm, period)
    smooth_minus = ema(minus_dm, period)

    dx_vals = []
    for i in range(len(smooth_tr)):
        if smooth_tr[i] == 0:
            dx_vals.append(0.0)
            continue
        plus_di = (smooth_plus[i] / smooth_tr[i]) * 100
        minus_di = (smooth_minus[i] / smooth_tr[i]) * 100
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_vals.append(0.0)
        else:
            dx_vals.append(abs(plus_di - minus_di) / di_sum * 100)

    if not dx_vals:
        return 0.0
    adx_vals = ema(dx_vals, period)
    return adx_vals[-1]


def choppiness_index(candles: Sequence[CandleData], period: int = 14) -> float:
    """Choppiness Index — higher = more choppy/ranging."""
    if len(candles) < period:
        return 50.0  # neutral default

    recent = candles[-period:]
    tr_sum = sum(true_range(recent))
    highest = max(c.high for c in recent)
    lowest = min(c.low for c in recent)
    hl_range = highest - lowest

    if hl_range == 0 or tr_sum == 0:
        return 100.0  # max chop

    chop = 100 * math.log10(tr_sum / hl_range) / math.log10(period)
    return min(max(chop, 0), 100)


def bollinger_band_width(candles: Sequence[CandleData], period: int = 20, std_mult: float = 2.0) -> float:
    """Bollinger Band width as percentage of middle band."""
    if len(candles) < period:
        return 0.0

    closes = [c.close for c in candles[-period:]]
    mid = sum(closes) / len(closes)
    if mid == 0:
        return 0.0

    variance = sum((c - mid) ** 2 for c in closes) / len(closes)
    std = math.sqrt(variance)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return ((upper - lower) / mid) * 100


def ema_slope(candles: Sequence[CandleData], period: int = 21, lookback: int = 3) -> float:
    """EMA slope — positive = uptrend, negative = downtrend."""
    if len(candles) < period + lookback:
        return 0.0

    closes = [c.close for c in candles]
    ema_vals = ema(closes, period)

    if len(ema_vals) < lookback + 1:
        return 0.0

    current = ema_vals[-1]
    previous = ema_vals[-lookback - 1]
    if previous == 0:
        return 0.0
    return (current - previous) / previous


def volume_ratio(candles: Sequence[CandleData], period: int = 20) -> float:
    """Current bar volume vs rolling average volume."""
    if len(candles) < 2:
        return 0.0

    avg_period = min(period, len(candles) - 1)
    historical = candles[-(avg_period + 1):-1]
    if not historical:
        return 0.0

    avg_vol = sum(c.volume for c in historical) / len(historical)
    if avg_vol == 0:
        return 0.0
    return candles[-1].volume / avg_vol


def extension_from_ema(candles: Sequence[CandleData], period: int = 21) -> float:
    """Distance of current price from EMA as multiple of ATR."""
    if len(candles) < period:
        return 0.0

    closes = [c.close for c in candles]
    ema_vals = ema(closes, period)
    atr_vals = atr(candles, 14)

    current_price = candles[-1].close
    current_ema = ema_vals[-1]
    current_atr = atr_vals[-1]

    if current_atr == 0:
        return 0.0
    return abs(current_price - current_ema) / current_atr


# ---------------------------------------------------------------------------
# Microstructure: CVD and OI
# ---------------------------------------------------------------------------

def compute_cvd(candles: Sequence[CandleData]) -> list[float]:
    """
    Approximate Cumulative Volume Delta from candle data.

    Uses the close-position-in-range heuristic:
    delta = volume * (2 * (close - low) / (high - low) - 1)
    Positive delta = aggressive buying, negative = aggressive selling.
    """
    cvd = []
    cumulative = 0.0
    for c in candles:
        rng = c.high - c.low
        if rng > 0:
            buy_ratio = (c.close - c.low) / rng
            delta = c.volume * (2 * buy_ratio - 1)
        else:
            delta = 0.0
        cumulative += delta
        cvd.append(cumulative)
    return cvd


def check_cvd_alignment(candles: Sequence[CandleData], direction: str, lookback: int = 5) -> tuple[bool, str]:
    """
    Check if CVD trend aligns with trade direction.
    For LONG: CVD should be rising. For SHORT: CVD should be falling.
    """
    if len(candles) < lookback + 1:
        return False, "Insufficient data for CVD"

    cvd = compute_cvd(candles)
    recent_cvd = cvd[-lookback:]
    cvd_change = recent_cvd[-1] - recent_cvd[0]

    if direction == "LONG" and cvd_change > 0:
        return True, f"CVD rising ({cvd_change:.0f}) — confirms LONG"
    elif direction == "SHORT" and cvd_change < 0:
        return True, f"CVD falling ({cvd_change:.0f}) — confirms SHORT"
    elif direction == "LONG":
        return False, f"CVD divergence: falling ({cvd_change:.0f}) vs LONG"
    else:
        return False, f"CVD divergence: rising ({cvd_change:.0f}) vs SHORT"


def check_oi_confirmation(
    oi_current: float | None,
    oi_previous: float | None,
    price_new_extreme: bool = False,
) -> tuple[bool, str]:
    """
    Check Open Interest confirmation.
    Valid breakout: rising OI (new money entering).
    Price extreme + dropping OI: short covering / liquidation → WAIT.
    """
    if oi_current is None or oi_previous is None:
        return False, "OI data unavailable"

    oi_change = oi_current - oi_previous
    oi_pct = (oi_change / oi_previous * 100) if oi_previous > 0 else 0

    if oi_change > 0:
        return True, f"OI rising ({oi_pct:+.2f}%) — new money confirming move"

    if price_new_extreme and oi_change < 0:
        return False, f"OI dropping ({oi_pct:+.2f}%) at price extreme — likely short covering/liquidation"

    return False, f"OI declining ({oi_pct:+.2f}%) — move unconfirmed"


def trend_direction(candles: Sequence[CandleData]) -> str:
    """Determine trend direction from EMA alignment."""
    if len(candles) < 21:
        return "NEUTRAL"

    closes = [c.close for c in candles]
    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    price = candles[-1].close

    if price > ema9 > ema21:
        return "UP"
    elif price < ema9 < ema21:
        return "DOWN"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Regime classifier — deterministic, cannot be overridden by LLM
# ---------------------------------------------------------------------------

def classify_regime(candles: Sequence[CandleData]) -> RegimeState:
    """
    Classify current market regime.

    TRENDING     — ADX > threshold, clear EMA slope, low CHOP → trading allowed
    RANGING      — High CHOP, flat EMAs, ADX low → blocked
    DEAD_LOW_VOL — ATR% very low, narrow BB → blocked
    SPIKE_HIGH_VOL — ATR% extreme, wide candles → block unless perfect breakout
    """
    if len(candles) < 21:
        return RegimeState(
            regime=Regime.DEAD_LOW_VOL,
            adx=0, chop=100, atr_pct=0, bb_width=0, ema_slope=0,
            details="Insufficient data for regime classification",
        )

    _adx = adx(candles)
    _chop = choppiness_index(candles)
    _atr_pct = atr_percent(candles)
    _bb_width = bollinger_band_width(candles)
    _ema_slope = ema_slope(candles)

    details_parts = []

    # Dead low volatility
    if _atr_pct < config.DEAD_VOL_ATR_PCT_MIN:
        details_parts.append(f"ATR%={_atr_pct:.4f} < {config.DEAD_VOL_ATR_PCT_MIN}")
        return RegimeState(
            regime=Regime.DEAD_LOW_VOL,
            adx=_adx, chop=_chop, atr_pct=_atr_pct,
            bb_width=_bb_width, ema_slope=_ema_slope,
            details=f"Dead zone: {'; '.join(details_parts)}",
        )

    # Spike high volatility
    atr_vals = atr(candles)
    last_candle_range = candles[-1].range_size
    if atr_vals[-1] > 0 and (last_candle_range / atr_vals[-1]) > config.SPIKE_CANDLE_ATR_MAX:
        return RegimeState(
            regime=Regime.SPIKE_HIGH_VOL,
            adx=_adx, chop=_chop, atr_pct=_atr_pct,
            bb_width=_bb_width, ema_slope=_ema_slope,
            details=f"Spike: candle range={last_candle_range:.2f}, ATR={atr_vals[-1]:.2f}",
        )

    # Ranging
    is_choppy = config.USE_CHOP_FILTER and _chop > config.CHOP_MAX
    is_low_adx = config.USE_ADX_FILTER and _adx < config.ADX_MIN
    is_flat_ema = abs(_ema_slope) < config.EMA_SLOPE_MIN

    if is_choppy and (is_low_adx or is_flat_ema):
        return RegimeState(
            regime=Regime.RANGING,
            adx=_adx, chop=_chop, atr_pct=_atr_pct,
            bb_width=_bb_width, ema_slope=_ema_slope,
            details=f"Ranging: CHOP={_chop:.1f}, ADX={_adx:.1f}, slope={_ema_slope:.6f}",
        )

    # Trending (default if none of the above)
    return RegimeState(
        regime=Regime.TRENDING,
        adx=_adx, chop=_chop, atr_pct=_atr_pct,
        bb_width=_bb_width, ema_slope=_ema_slope,
        details=f"Trending: ADX={_adx:.1f}, CHOP={_chop:.1f}, slope={_ema_slope:.6f}",
    )
