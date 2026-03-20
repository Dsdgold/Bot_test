"""
Technical indicators for scalping strategy.
All calculations are done on lists of Candle data.
"""
import math
from typing import List

from .models import Candle, Indicators


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

    # Align lengths
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
        atr=atr(candles),
        volume_sma=sma(volumes, 20),
        current_volume=volumes[-1] if volumes else 0,
        price=closes[-1],
    )
