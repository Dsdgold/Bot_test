"""
Technical indicators for scalping strategy — Selective Execution Mode.
Includes regime classifier, ADX, Choppiness Index, CVD approximation, extension metrics.
"""
import math
from typing import List, Optional

from .models import Candle, Indicators, MarketRegime, RegimeState


def ema(values: List[float], period: int) -> List[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return [values[-1]] if values else [0.0]
    multiplier = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for price in values[period:]:
        result.append((price - result[-1]) * multiplier + result[-1])
    return result


def sma(values: List[float], period: int) -> float:
    """Simple Moving Average of last N values."""
    if not values:
        return 0.0
    data = values[-period:]
    return sum(data) / len(data)


def rsi(closes: List[float], period: int = 14) -> float:
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(closes: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9):
    """MACD indicator. Returns (macd_line, signal_line, histogram)."""
    if len(closes) < slow + signal_period:
        return 0.0, 0.0, 0.0
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    min_len = min(len(ema_fast), len(ema_slow))
    offset_fast = len(ema_fast) - min_len
    offset_slow = len(ema_slow) - min_len
    macd_line = [ema_fast[i + offset_fast] - ema_slow[i + offset_slow] for i in range(min_len)]
    if len(macd_line) < signal_period:
        return macd_line[-1] if macd_line else 0.0, 0.0, 0.0
    signal_line = ema(macd_line, signal_period)
    current_macd = macd_line[-1]
    current_signal = signal_line[-1]
    histogram = current_macd - current_signal
    return current_macd, current_signal, histogram


def bollinger_bands(closes: List[float], period: int = 20, std_mult: float = 2.0):
    """Bollinger Bands. Returns (upper, middle, lower, width)."""
    if len(closes) < period:
        p = closes[-1] if closes else 0
        return p, p, p, 0.0
    data = closes[-period:]
    middle = sum(data) / len(data)
    variance = sum((x - middle) ** 2 for x in data) / len(data)
    std = math.sqrt(variance)
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    width = (upper - lower) / middle if middle != 0 else 0
    return upper, middle, lower, width


def vwap(candles: List[Candle]) -> float:
    """Volume Weighted Average Price."""
    if not candles:
        return 0.0
    total_vp = 0.0
    total_volume = 0.0
    for c in candles:
        typical_price = (c.high + c.low + c.close) / 3
        total_vp += typical_price * c.volume
        total_volume += c.volume
    if total_volume == 0:
        return candles[-1].close
    return total_vp / total_volume


def atr(candles: List[Candle], period: int = 14) -> float:
    """Average True Range."""
    if len(candles) < 2:
        return 0.0
    true_ranges = []
    for i in range(1, len(candles)):
        high_low = candles[i].high - candles[i].low
        high_prev_close = abs(candles[i].high - candles[i - 1].close)
        low_prev_close = abs(candles[i].low - candles[i - 1].close)
        true_ranges.append(max(high_low, high_prev_close, low_prev_close))
    if not true_ranges:
        return 0.0
    data = true_ranges[-period:]
    return sum(data) / len(data)


def adx(candles: List[Candle], period: int = 14) -> float:
    """Average Directional Index — trend strength measure.
    Returns ADX value (0-100). Higher = stronger trend."""
    if len(candles) < period + 1:
        return 0.0

    plus_dm_list = []
    minus_dm_list = []
    tr_list = []

    for i in range(1, len(candles)):
        high_diff = candles[i].high - candles[i - 1].high
        low_diff = candles[i - 1].low - candles[i].low

        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0

        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    if len(tr_list) < period:
        return 0.0

    # Smoothed values
    smoothed_tr = sum(tr_list[:period])
    smoothed_plus_dm = sum(plus_dm_list[:period])
    smoothed_minus_dm = sum(minus_dm_list[:period])

    dx_values = []

    for i in range(period, len(tr_list)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_list[i]

        if smoothed_tr == 0:
            continue

        plus_di = (smoothed_plus_dm / smoothed_tr) * 100
        minus_di = (smoothed_minus_dm / smoothed_tr) * 100

        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_values.append(0)
        else:
            dx = abs(plus_di - minus_di) / di_sum * 100
            dx_values.append(dx)

    if not dx_values:
        return 0.0

    # ADX is the smoothed average of DX
    adx_vals = dx_values[-period:]
    return sum(adx_vals) / len(adx_vals)


def choppiness_index(candles: List[Candle], period: int = 14) -> float:
    """Choppiness Index — range detection.
    High values (>61.8) = choppy/ranging. Low values (<38.2) = trending."""
    if len(candles) < period + 1:
        return 50.0

    recent = candles[-period:]
    atr_sum = 0.0
    for i in range(1, len(recent)):
        tr = max(
            recent[i].high - recent[i].low,
            abs(recent[i].high - recent[i - 1].close),
            abs(recent[i].low - recent[i - 1].close),
        )
        atr_sum += tr

    highest_high = max(c.high for c in recent)
    lowest_low = min(c.low for c in recent)
    hl_range = highest_high - lowest_low

    if hl_range <= 0 or atr_sum <= 0:
        return 50.0

    chop = 100 * math.log10(atr_sum / hl_range) / math.log10(period)
    return max(0, min(100, chop))


def ema_slope(values: List[float], period: int, lookback: int = 5) -> float:
    """Calculate EMA slope as % change over lookback periods."""
    ema_vals = ema(values, period)
    if len(ema_vals) < lookback + 1:
        return 0.0
    old_val = ema_vals[-(lookback + 1)]
    new_val = ema_vals[-1]
    if old_val == 0:
        return 0.0
    return ((new_val - old_val) / old_val) * 100


def extension_from_mean(price: float, ema_value: float, atr_value: float) -> float:
    """Calculate how far price is from EMA in ATR units."""
    if atr_value <= 0:
        return 0.0
    return abs(price - ema_value) / atr_value


def approximate_cvd(candles: List[Candle], period: int = 20) -> float:
    """Approximate Cumulative Volume Delta from candle data.
    Positive = buying pressure. Negative = selling pressure."""
    if len(candles) < period:
        return 0.0

    cvd = 0.0
    for c in candles[-period:]:
        candle_range = c.high - c.low
        if candle_range <= 0:
            continue
        # Estimate buy/sell volume from candle position
        close_position = (c.close - c.low) / candle_range
        buy_volume = c.volume * close_position
        sell_volume = c.volume * (1 - close_position)
        cvd += (buy_volume - sell_volume)

    return cvd


def classify_regime(
    adx_value: float,
    chop_value: float,
    atr_pct: float,
    bb_width: float,
    ema_slope_val: float,
    adx_min: float = 18,
    chop_max: float = 61.8,
    dead_vol_min: float = 0.10,
    spike_atr_max: float = 1.5,
) -> RegimeState:
    """Deterministic market regime classifier.

    Returns RegimeState with regime classification and whether trading is allowed.
    """
    state = RegimeState(
        adx=adx_value,
        chop_index=chop_value,
        atr_pct=atr_pct,
        bb_width=bb_width,
        ema_slope=ema_slope_val,
    )

    # DEAD_LOW_VOL: no volatility, no participation
    if atr_pct < dead_vol_min and bb_width < 0.01:
        state.regime = MarketRegime.DEAD_LOW_VOL
        state.trading_allowed = False
        state.reason = f"Dead market: ATR%={atr_pct:.3f}% BB_w={bb_width:.4f}"
        return state

    # SPIKE_HIGH_VOL: extreme volatility spike
    # Use relative ATR — if atr_pct > spike_atr_max% it's extreme
    if atr_pct > spike_atr_max:
        state.regime = MarketRegime.SPIKE_HIGH_VOL
        state.trading_allowed = False
        state.reason = f"Volatility spike: ATR%={atr_pct:.3f}%"
        return state

    # RANGING: high chop, low ADX, flat EMAs
    if chop_value > chop_max and adx_value < adx_min:
        state.regime = MarketRegime.RANGING
        state.trading_allowed = False
        state.reason = f"Ranging: CHOP={chop_value:.1f} ADX={adx_value:.1f}"
        return state

    # TRENDING: clear directional movement
    if adx_value >= adx_min and chop_value <= chop_max:
        state.regime = MarketRegime.TRENDING
        state.trading_allowed = True
        state.reason = f"Trending: ADX={adx_value:.1f} CHOP={chop_value:.1f} slope={ema_slope_val:.3f}%"
        return state

    # Borderline — allow with caution
    if adx_value >= adx_min:
        state.regime = MarketRegime.TRENDING
        state.trading_allowed = True
        state.reason = f"Weak trend: ADX={adx_value:.1f} CHOP={chop_value:.1f}"
    else:
        state.regime = MarketRegime.RANGING
        state.trading_allowed = False
        state.reason = f"Weak/ranging: ADX={adx_value:.1f} CHOP={chop_value:.1f}"

    return state


def calculate_all(candles: List[Candle], config) -> Indicators:
    """Calculate all indicators from candle data."""
    if not candles or len(candles) < 2:
        return Indicators()

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    # EMA
    ema_fast_vals = ema(closes, config.ema_fast)
    ema_slow_vals = ema(closes, config.ema_slow)
    ema_trend_vals = ema(closes, config.ema_trend)

    # MACD
    macd_val, macd_sig, macd_hist = macd(
        closes, config.macd_fast, config.macd_slow, config.macd_signal
    )

    # Bollinger Bands
    bb_up, bb_mid, bb_low, bb_w = bollinger_bands(
        closes, config.bb_period, config.bb_std
    )

    # ATR
    atr_val = atr(candles)
    price = closes[-1]
    atr_pct_val = (atr_val / price * 100) if price > 0 else 0

    # Volume ratio
    vol_sma = sma(volumes, 20)
    vol_ratio = (volumes[-1] / vol_sma) if vol_sma > 0 else 1.0

    # ADX
    adx_val = adx(candles)

    # Choppiness Index
    chop_val = choppiness_index(candles)

    # EMA slope
    slope_1m = ema_slope(closes, config.ema_slow, lookback=5)

    # Extension from mean
    ext = extension_from_mean(price, ema_slow_vals[-1], atr_val) if atr_val > 0 else 0

    # CVD approximation
    cvd_val = approximate_cvd(candles)

    return Indicators(
        rsi=rsi(closes, config.rsi_period),
        ema_fast=ema_fast_vals[-1],
        ema_slow=ema_slow_vals[-1],
        ema_trend=ema_trend_vals[-1],
        macd=macd_val,
        macd_signal=macd_sig,
        macd_histogram=macd_hist,
        bb_upper=bb_up,
        bb_middle=bb_mid,
        bb_lower=bb_low,
        bb_width=bb_w,
        vwap=vwap(candles),
        atr=atr_val,
        volume_sma=vol_sma,
        current_volume=volumes[-1] if volumes else 0,
        price=price,
        adx=adx_val,
        chop_index=chop_val,
        ema_slope_1m=slope_1m,
        atr_pct=atr_pct_val,
        cvd_value=cvd_val,
        extension_atr=ext,
        volume_ratio=vol_ratio,
    )
