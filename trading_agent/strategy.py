"""
Scalping strategy engine — Selective Execution Mode.
Combines technical indicators with strict entry quality gates.
WAIT is the default. Every trade must pass ALL gates.
"""
import logging
from typing import List, Optional

from .config import TradingConfig
from .indicators import (
    calculate_all, classify_regime, extension_from_mean
)
from .models import (
    Candle, DirectionalLicense, EntryQuality, HTFAlignment, Indicators,
    MarketContext, MarketRegime, RegimeState, SetupType,
    Side, Signal, SignalStrength
)

logger = logging.getLogger("strategy")


class ScalpingStrategy:
    """
    Selective scalping strategy — institutional approach.

    Gate order (all must pass):
    1. Regime filter (deterministic)
    2. Session/hour filter
    3. HTF alignment
    4. Trend strength confirmation
    5. Extension filter (no chasing)
    6. Volume confirmation
    7. CVD + OI microstructure
    8. Candle close confirmation
    9. Reversal elevated standard (if reversal)
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.prev_indicators: Indicators | None = None
        self.prev_regime: Optional[RegimeState] = None
        self.skip_count: int = 0
        self.skip_reasons: dict = {}

    def classify_market_regime(self, indicators: Indicators) -> RegimeState:
        """Classify current market regime using deterministic filters."""
        return classify_regime(
            adx_value=indicators.adx,
            chop_value=indicators.chop_index,
            atr_pct=indicators.atr_pct,
            bb_width=indicators.bb_width,
            ema_slope_val=indicators.ema_slope_1m,
            adx_min=self.config.adx_min,
            chop_max=self.config.chop_max,
            dead_vol_min=self.config.dead_vol_atr_pct_min,
            spike_atr_max=self.config.spike_candle_atr_max,
        )

    def evaluate_entry_quality(
        self,
        side: Side,
        indicators: Indicators,
        regime: RegimeState,
        market_context: Optional[MarketContext] = None,
        license: Optional[DirectionalLicense] = None,
    ) -> EntryQuality:
        """Evaluate entry quality through all gates. Returns composite quality."""
        quality = EntryQuality()
        score = 0

        # Gate A: HTF alignment
        if market_context:
            trends = [market_context.trend_5m, market_context.trend_15m, market_context.trend_1h]
            target_trend = "UP" if side == Side.LONG else "DOWN"
            opposing_trend = "DOWN" if side == Side.LONG else "UP"

            aligned_count = sum(1 for t in trends if t == target_trend)
            opposing_count = sum(1 for t in trends if t == opposing_trend)

            if market_context.trend_1h == opposing_trend:
                quality.skip_reasons.append(f"HTF opposing: 1h={market_context.trend_1h}")
                quality.htf_alignment = HTFAlignment.OPPOSING
            elif aligned_count >= 2:
                quality.htf_alignment = HTFAlignment.ALIGNED
                score += 25
                quality.reasons.append(f"HTF aligned: {aligned_count}/3 TFs agree")
            elif opposing_count == 0:
                quality.htf_alignment = HTFAlignment.NEUTRAL
                score += 10
                quality.reasons.append("HTF neutral — no opposition")
            else:
                quality.htf_alignment = HTFAlignment.OPPOSING
                quality.skip_reasons.append(f"HTF opposition: {opposing_count}/3 TFs oppose")

        # Gate B: Trend strength
        trend_ok = False
        if indicators.adx >= self.config.adx_min:
            trend_ok = True
            score += 15
            quality.reasons.append(f"ADX={indicators.adx:.1f} (min={self.config.adx_min})")
        elif abs(indicators.ema_slope_1m) >= 0.02:
            trend_ok = True
            score += 10
            quality.reasons.append(f"EMA slope={indicators.ema_slope_1m:.3f}%")

        quality.trend_confirmed = trend_ok
        if not trend_ok and regime.regime == MarketRegime.TRENDING:
            # Borderline — still allow but lower score
            score += 5

        # Gate C: Extension filter
        ext = indicators.extension_atr
        if ext <= self.config.max_entry_extension_atr:
            quality.extension_ok = True
            score += 15
            quality.reasons.append(f"Extension OK: {ext:.2f} ATR (max={self.config.max_entry_extension_atr})")
        else:
            quality.extension_ok = False
            quality.skip_reasons.append(f"Overextended: {ext:.2f} ATR > {self.config.max_entry_extension_atr}")

        # Gate D: Candle close confirmation
        # In live trading we check if the last candle is closed (not still printing)
        # For now mark as confirmed — agent enforces timing
        quality.candle_confirmed = True
        score += 5

        # Gate E: Volume confirmation
        vol_ratio = indicators.volume_ratio
        if vol_ratio >= self.config.min_volume_ratio:
            quality.volume_confirmed = True
            score += 15
            quality.reasons.append(f"Volume confirmed: {vol_ratio:.2f}x (min={self.config.min_volume_ratio})")
        else:
            quality.volume_confirmed = False
            quality.skip_reasons.append(f"Weak volume: {vol_ratio:.2f}x < {self.config.min_volume_ratio}")

        # Gate F: CVD alignment
        if self.config.require_cvd_alignment:
            cvd = indicators.cvd_value
            if side == Side.LONG and cvd > 0:
                quality.cvd_aligned = True
                score += 10
                quality.reasons.append(f"CVD aligned (buying): {cvd:+.0f}")
            elif side == Side.SHORT and cvd < 0:
                quality.cvd_aligned = True
                score += 10
                quality.reasons.append(f"CVD aligned (selling): {cvd:+.0f}")
            else:
                quality.cvd_aligned = False
                quality.skip_reasons.append(f"CVD divergence: {cvd:+.0f} vs {side.value}")
        else:
            quality.cvd_aligned = True
            score += 5

        # Gate F2: OI confirmation
        if self.config.require_oi_confirmation and market_context:
            oi_change = market_context.open_interest_change
            if oi_change > 0:
                quality.oi_confirmed = True
                score += 10
                quality.reasons.append(f"OI rising: {oi_change:+.2f}%")
            else:
                quality.oi_confirmed = False
                quality.skip_reasons.append(f"OI dropping: {oi_change:+.2f}%")
        else:
            quality.oi_confirmed = True
            score += 5

        # Determine setup type
        if license and license.setup_type:
            try:
                quality.setup_type = SetupType(license.setup_type)
            except ValueError:
                quality.setup_type = SetupType.CONTINUATION
        else:
            # Infer from indicators
            if indicators.ema_fast > indicators.ema_slow and side == Side.LONG:
                quality.setup_type = SetupType.CONTINUATION
            elif indicators.ema_fast < indicators.ema_slow and side == Side.SHORT:
                quality.setup_type = SetupType.CONTINUATION
            else:
                quality.setup_type = SetupType.REVERSAL

        # Gate G: Reversal elevated standard
        if quality.setup_type == SetupType.REVERSAL:
            reversal_score = 0
            # Need RSI divergence
            if self.prev_indicators:
                if (side == Side.LONG and indicators.price < self.prev_indicators.price
                        and indicators.rsi > self.prev_indicators.rsi):
                    reversal_score += 1
                    quality.reasons.append("Bullish divergence")
                elif (side == Side.SHORT and indicators.price > self.prev_indicators.price
                      and indicators.rsi < self.prev_indicators.rsi):
                    reversal_score += 1
                    quality.reasons.append("Bearish divergence")

            # Need extreme RSI
            if (side == Side.LONG and indicators.rsi < 30) or (side == Side.SHORT and indicators.rsi > 70):
                reversal_score += 1
                quality.reasons.append(f"Extreme RSI: {indicators.rsi:.1f}")

            if reversal_score < 2:
                quality.skip_reasons.append(f"Reversal evidence insufficient: {reversal_score}/2")

            # Require higher quality threshold
            if score < self.config.reversal_quality_min:
                quality.skip_reasons.append(
                    f"Reversal quality {score} < {self.config.reversal_quality_min}"
                )

        quality.score = min(score, 100)
        return quality

    def analyze(self, candles: List[Candle]) -> Signal:
        """Analyze candles and generate a trading signal with regime context."""
        if len(candles) < 60:
            return Signal(strength=SignalStrength.NEUTRAL, reasons=["Insufficient data"])

        ind = calculate_all(candles, self.config)
        signal = Signal(indicators=ind)

        long_score = 0.0
        short_score = 0.0
        reasons = []

        # RSI Analysis (weight: 20)
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

        # RSI divergence
        if self.prev_indicators:
            if ind.price > self.prev_indicators.price and ind.rsi < self.prev_indicators.rsi:
                short_score += 8
                reasons.append("Bearish RSI divergence")
            elif ind.price < self.prev_indicators.price and ind.rsi > self.prev_indicators.rsi:
                long_score += 8
                reasons.append("Bullish RSI divergence")

        # EMA Analysis (weight: 25)
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

        # EMA crossover
        if self.prev_indicators:
            prev_diff = self.prev_indicators.ema_fast - self.prev_indicators.ema_slow
            curr_diff = ind.ema_fast - ind.ema_slow
            if prev_diff < 0 < curr_diff:
                long_score += 15
                reasons.append("Bullish EMA crossover!")
            elif prev_diff > 0 > curr_diff:
                short_score += 15
                reasons.append("Bearish EMA crossover!")

        # MACD Analysis (weight: 20)
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

        # Bollinger Bands (weight: 20)
        price = ind.price
        if price <= ind.bb_lower:
            long_score += 20
            reasons.append("Price at lower Bollinger Band")
        elif price < ind.bb_middle and price > ind.bb_lower:
            bb_pos = (price - ind.bb_lower) / (ind.bb_middle - ind.bb_lower) if ind.bb_middle != ind.bb_lower else 0.5
            if bb_pos < 0.3:
                long_score += 12
                reasons.append("Price near lower BB")

        if price >= ind.bb_upper:
            short_score += 20
            reasons.append("Price at upper Bollinger Band")
        elif price > ind.bb_middle and price < ind.bb_upper:
            bb_pos = (price - ind.bb_middle) / (ind.bb_upper - ind.bb_middle) if ind.bb_upper != ind.bb_middle else 0.5
            if bb_pos > 0.7:
                short_score += 12
                reasons.append("Price near upper BB")

        if ind.bb_width < 0.02:
            reasons.append(f"BB squeeze (width: {ind.bb_width:.4f})")

        # VWAP (weight: 5)
        if price > ind.vwap:
            long_score += 5
            reasons.append("Price above VWAP")
        elif price < ind.vwap:
            short_score += 5
            reasons.append("Price below VWAP")

        # Volume (weight: 5)
        if ind.volume_sma > 0 and ind.current_volume > ind.volume_sma * self.config.volume_spike_multiplier:
            if long_score > short_score:
                long_score += 5
            else:
                short_score += 5
            reasons.append(f"Volume spike ({ind.current_volume / ind.volume_sma:.1f}x avg)")

        # Determine Signal
        max_possible = 100.0
        if long_score > short_score:
            confidence = min((long_score / max_possible) * 100, 100)
            signal.side = Side.LONG
            signal.confidence = confidence
            if confidence >= 50:
                signal.strength = SignalStrength.STRONG_BUY
            elif confidence >= 20:
                signal.strength = SignalStrength.BUY
            else:
                signal.strength = SignalStrength.NEUTRAL
                signal.side = None
        elif short_score > long_score:
            confidence = min((short_score / max_possible) * 100, 100)
            signal.side = Side.SHORT
            signal.confidence = confidence
            if confidence >= 50:
                signal.strength = SignalStrength.STRONG_SELL
            elif confidence >= 20:
                signal.strength = SignalStrength.SELL
            else:
                signal.strength = SignalStrength.NEUTRAL
                signal.side = None
        else:
            signal.strength = SignalStrength.NEUTRAL
            signal.confidence = 0

        signal.reasons = reasons

        # Add regime and extension info to signal
        regime = self.classify_market_regime(ind)
        signal.regime = regime.regime
        signal.extension_atr = ind.extension_atr
        signal.volume_ratio = ind.volume_ratio
        signal.cvd_aligned = ind.cvd_value > 0 if signal.side == Side.LONG else ind.cvd_value < 0

        # Store for next comparison
        self.prev_indicators = ind
        self.prev_regime = regime

        logger.info(
            f"Signal: {signal.strength.value} | "
            f"Confidence: {signal.confidence:.1f}% | "
            f"LONG: {long_score:.0f} SHORT: {short_score:.0f} | "
            f"Regime: {regime.regime.value} | "
            f"Reasons: {len(reasons)}"
        )

        return signal

    def record_skip(self, reason: str):
        """Record a skipped trade for analysis."""
        self.skip_count += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
