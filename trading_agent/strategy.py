"""
Scalping strategy engine.
Combines multiple technical indicators to generate high-confidence trading signals.
"""
import logging
from typing import List

from .config import TradingConfig
from .indicators import calculate_all
from .models import Candle, Indicators, Side, Signal, SignalStrength

logger = logging.getLogger("strategy")


class ScalpingStrategy:
    """
    Aggressive scalping strategy for futures with high leverage.

    Combines:
    - RSI for overbought/oversold
    - EMA crossovers for trend direction
    - MACD for momentum
    - Bollinger Bands for volatility and mean reversion
    - VWAP for institutional bias
    - Volume analysis for confirmation
    - ATR for dynamic stop-loss
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.prev_indicators: Indicators | None = None

    def analyze(self, candles: List[Candle]) -> Signal:
        """Analyze candles and generate a trading signal."""
        if len(candles) < 60:
            return Signal(strength=SignalStrength.NEUTRAL, reasons=["Insufficient data"])

        ind = calculate_all(candles, self.config)
        signal = Signal(indicators=ind)

        long_score = 0.0
        short_score = 0.0
        reasons = []

        # ── 1. RSI Analysis (weight: 20) ────────────────────────
        if ind.rsi < self.config.rsi_oversold:
            long_score += 20
            reasons.append(f"RSI oversold ({ind.rsi:.1f})")
        elif ind.rsi < 40:
            long_score += 10
            reasons.append(f"RSI low ({ind.rsi:.1f})")
        elif ind.rsi > self.config.rsi_overbought:
            short_score += 20
            reasons.append(f"RSI overbought ({ind.rsi:.1f})")
        elif ind.rsi > 60:
            short_score += 10
            reasons.append(f"RSI high ({ind.rsi:.1f})")

        # RSI divergence check (momentum)
        if self.prev_indicators:
            if ind.price > self.prev_indicators.price and ind.rsi < self.prev_indicators.rsi:
                short_score += 8
                reasons.append("Bearish RSI divergence")
            elif ind.price < self.prev_indicators.price and ind.rsi > self.prev_indicators.rsi:
                long_score += 8
                reasons.append("Bullish RSI divergence")

        # ── 2. EMA Analysis (weight: 25) ────────────────────────
        if ind.ema_fast > ind.ema_slow:
            long_score += 15
            reasons.append("EMA fast > slow (bullish)")
            if ind.price > ind.ema_trend:
                long_score += 10
                reasons.append("Price above trend EMA")
        else:
            short_score += 15
            reasons.append("EMA fast < slow (bearish)")
            if ind.price < ind.ema_trend:
                short_score += 10
                reasons.append("Price below trend EMA")

        # EMA crossover detection
        if self.prev_indicators:
            prev_diff = self.prev_indicators.ema_fast - self.prev_indicators.ema_slow
            curr_diff = ind.ema_fast - ind.ema_slow
            if prev_diff < 0 < curr_diff:
                long_score += 15
                reasons.append("Bullish EMA crossover!")
            elif prev_diff > 0 > curr_diff:
                short_score += 15
                reasons.append("Bearish EMA crossover!")

        # ── 3. MACD Analysis (weight: 20) ───────────────────────
        if ind.macd > ind.macd_signal:
            long_score += 10
            reasons.append("MACD above signal")
            if ind.macd_histogram > 0:
                long_score += 10
                reasons.append(f"MACD histogram positive ({ind.macd_histogram:.4f})")
        else:
            short_score += 10
            reasons.append("MACD below signal")
            if ind.macd_histogram < 0:
                short_score += 10
                reasons.append(f"MACD histogram negative ({ind.macd_histogram:.4f})")

        # MACD crossover
        if self.prev_indicators:
            prev_diff = self.prev_indicators.macd - self.prev_indicators.macd_signal
            curr_diff = ind.macd - ind.macd_signal
            if prev_diff < 0 < curr_diff:
                long_score += 12
                reasons.append("MACD bullish crossover!")
            elif prev_diff > 0 > curr_diff:
                short_score += 12
                reasons.append("MACD bearish crossover!")

        # ── 4. Bollinger Bands (weight: 20) ─────────────────────
        price = ind.price

        if price <= ind.bb_lower:
            long_score += 20
            reasons.append("Price at lower Bollinger Band (bounce)")
        elif price < ind.bb_middle and price > ind.bb_lower:
            bb_pos = (price - ind.bb_lower) / (ind.bb_middle - ind.bb_lower) if ind.bb_middle != ind.bb_lower else 0.5
            if bb_pos < 0.3:
                long_score += 12
                reasons.append("Price near lower BB")

        if price >= ind.bb_upper:
            short_score += 20
            reasons.append("Price at upper Bollinger Band (reversal)")
        elif price > ind.bb_middle and price < ind.bb_upper:
            bb_pos = (price - ind.bb_middle) / (ind.bb_upper - ind.bb_middle) if ind.bb_upper != ind.bb_middle else 0.5
            if bb_pos > 0.7:
                short_score += 12
                reasons.append("Price near upper BB")

        # Bollinger Band squeeze (low volatility → big move coming)
        if ind.bb_width < 0.02:
            reasons.append(f"BB squeeze detected (width: {ind.bb_width:.4f})")

        # ── 5. VWAP Analysis (weight: 10) ───────────────────────
        if price > ind.vwap:
            long_score += 5
            reasons.append("Price above VWAP (bullish bias)")
        elif price < ind.vwap:
            short_score += 5
            reasons.append("Price below VWAP (bearish bias)")

        # ── 6. Volume Confirmation (weight: 5) ──────────────────
        if ind.volume_sma > 0 and ind.current_volume > ind.volume_sma * self.config.volume_spike_multiplier:
            # Volume spike confirms the move
            if long_score > short_score:
                long_score += 5
            else:
                short_score += 5
            reasons.append(f"Volume spike ({ind.current_volume / ind.volume_sma:.1f}x avg)")

        # ── Determine Signal ────────────────────────────────────
        max_possible = 100.0

        if long_score > short_score:
            confidence = min((long_score / max_possible) * 100, 100)
            signal.side = Side.LONG
            signal.confidence = confidence
            if confidence >= 75:
                signal.strength = SignalStrength.STRONG_BUY
            elif confidence >= 50:
                signal.strength = SignalStrength.BUY
            else:
                signal.strength = SignalStrength.NEUTRAL
                signal.side = None
        elif short_score > long_score:
            confidence = min((short_score / max_possible) * 100, 100)
            signal.side = Side.SHORT
            signal.confidence = confidence
            if confidence >= 75:
                signal.strength = SignalStrength.STRONG_SELL
            elif confidence >= 50:
                signal.strength = SignalStrength.SELL
            else:
                signal.strength = SignalStrength.NEUTRAL
                signal.side = None
        else:
            signal.strength = SignalStrength.NEUTRAL
            signal.confidence = 0

        signal.reasons = reasons

        # Store for next comparison
        self.prev_indicators = ind

        logger.info(
            f"Signal: {signal.strength.value} | "
            f"Confidence: {signal.confidence:.1f}% | "
            f"LONG: {long_score:.0f} SHORT: {short_score:.0f} | "
            f"Reasons: {len(reasons)}"
        )

        return signal
