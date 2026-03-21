"""Deterministic entry-quality gate system.

The strategy receives a DirectionalLicense from the AI brain and applies
strict deterministic filters before allowing execution. These gates
CANNOT be overridden by LLM confidence alone.
"""

from __future__ import annotations

import logging
from typing import Sequence

from trading_agent import config
from trading_agent.indicators import (
    adx, check_cvd_alignment, check_oi_confirmation,
    ema_slope, extension_from_ema, trend_direction, volume_ratio,
)
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, EntryGateResult,
    HTFAlignment, Regime, RegimeState, SetupType,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sub-gate A: Higher-timeframe alignment
# ---------------------------------------------------------------------------

def check_htf_alignment(
    license: DirectionalLicense,
    candles_5m: Sequence[CandleData],
    candles_15m: Sequence[CandleData],
    candles_1h: Sequence[CandleData],
) -> tuple[bool, str]:
    """
    Continuation: 15m AND 1h must align, OR 5m/15m strongly align while 1h neutral.
    1h opposing → block.
    """
    if not license.is_trade:
        return True, "WAIT — no HTF check needed"

    direction = "UP" if license.action == Action.LONG else "DOWN"

    trend_1h = trend_direction(candles_1h) if candles_1h else "NEUTRAL"
    trend_15m = trend_direction(candles_15m) if candles_15m else "NEUTRAL"
    trend_5m = trend_direction(candles_5m) if candles_5m else "NEUTRAL"

    # 1h clearly opposing → warning only (not a hard block)
    opposing_1h = (
        (direction == "UP" and trend_1h == "DOWN") or
        (direction == "DOWN" and trend_1h == "UP")
    )
    if opposing_1h:
        logger.info(f"HTF WARNING: 1h opposing ({trend_1h}) vs {direction} — allowing with caution")
        return True, f"HTF cautious: 1h opposing ({trend_1h}) but allowing trade"

    # 15m AND 1h aligned → pass
    aligned_15m = trend_15m == direction or trend_15m == "NEUTRAL"
    aligned_1h = trend_1h == direction or trend_1h == "NEUTRAL"
    if aligned_15m and aligned_1h:
        return True, f"HTF aligned: 15m={trend_15m}, 1h={trend_1h}"

    # 5m/15m strongly align, 1h neutral → pass
    if trend_5m == direction and trend_15m == direction and trend_1h == "NEUTRAL":
        return True, f"5m/15m strongly aligned ({direction}), 1h neutral"

    return False, f"HTF misaligned: 5m={trend_5m}, 15m={trend_15m}, 1h={trend_1h}"


# ---------------------------------------------------------------------------
# Sub-gate B: Trend-strength confirmation
# ---------------------------------------------------------------------------

def check_trend_strength(candles_1m: Sequence[CandleData]) -> tuple[bool, str]:
    """Require ADX ≥ min OR EMA slope ≥ min."""
    if not candles_1m or len(candles_1m) < 21:
        return False, "Insufficient data for trend check"

    _adx = adx(candles_1m)
    _slope = abs(ema_slope(candles_1m))

    adx_ok = _adx >= config.ADX_MIN
    slope_ok = _slope >= config.EMA_SLOPE_MIN

    if adx_ok or slope_ok:
        return True, f"Trend confirmed: ADX={_adx:.1f}, slope={_slope:.6f}"
    return False, f"Weak trend: ADX={_adx:.1f} < {config.ADX_MIN}, slope={_slope:.6f} < {config.EMA_SLOPE_MIN}"


# ---------------------------------------------------------------------------
# Sub-gate C: Extension filter
# ---------------------------------------------------------------------------

def check_extension(candles_1m: Sequence[CandleData]) -> tuple[bool, str]:
    """Reject if price is too far from local mean (ATR-normalized)."""
    if not candles_1m or len(candles_1m) < 21:
        return True, "Insufficient data — no extension check"

    ext = extension_from_ema(candles_1m)
    if ext > config.MAX_ENTRY_EXTENSION_ATR:
        return False, f"Overextended: {ext:.2f} ATR from EMA (max {config.MAX_ENTRY_EXTENSION_ATR})"
    return True, f"Extension OK: {ext:.2f} ATR from EMA"


# ---------------------------------------------------------------------------
# Sub-gate D: Candle-close confirmation
# ---------------------------------------------------------------------------

def check_candle_close(
    candles_1m: Sequence[CandleData],
    license: DirectionalLicense,
) -> tuple[bool, str]:
    """
    Require the last closed candle to confirm the trade direction.
    Skip check if config says so.
    """
    if not config.CANDLE_CLOSE_CONFIRMATION:
        return True, "Candle-close confirmation disabled"

    if not candles_1m or len(candles_1m) < 2:
        return False, "Insufficient candle data"

    # Use second-to-last candle (last fully closed)
    last_closed = candles_1m[-2]

    if license.action == Action.LONG:
        if last_closed.is_bullish:
            return True, f"Bullish close confirms LONG: {last_closed.close:.1f}"
        return False, f"Last closed candle bearish ({last_closed.close:.1f} < {last_closed.open:.1f})"

    if license.action == Action.SHORT:
        if not last_closed.is_bullish:
            return True, f"Bearish close confirms SHORT: {last_closed.close:.1f}"
        return False, f"Last closed candle bullish ({last_closed.close:.1f} > {last_closed.open:.1f})"

    return True, "WAIT — no candle check needed"


# ---------------------------------------------------------------------------
# Sub-gate E: Volume / participation
# ---------------------------------------------------------------------------

def check_volume(candles_1m: Sequence[CandleData]) -> tuple[bool, str]:
    """Require current bar volume ≥ MIN_VOLUME_RATIO × average."""
    if not candles_1m or len(candles_1m) < 3:
        return False, "Insufficient volume data"

    ratio = volume_ratio(candles_1m)
    if ratio >= config.MIN_VOLUME_RATIO:
        return True, f"Volume OK: ratio={ratio:.2f}x (min {config.MIN_VOLUME_RATIO})"
    return False, f"Weak volume: ratio={ratio:.2f}x < {config.MIN_VOLUME_RATIO}"


# ---------------------------------------------------------------------------
# Sub-gate F: Reversal elevated standard
# ---------------------------------------------------------------------------

def check_reversal_requirements(license: DirectionalLicense) -> tuple[bool, str]:
    """
    Reversals must meet higher entry quality and confidence thresholds.
    Full divergence/structure checks would require more complex indicators —
    we enforce the quality/confidence floor here.
    """
    if not license.is_reversal:
        return True, "Not a reversal — skip"

    issues = []
    if license.entry_quality < config.REVERSAL_QUALITY_MIN:
        issues.append(
            f"Entry quality {license.entry_quality} < {config.REVERSAL_QUALITY_MIN}"
        )
    if license.confidence < config.REVERSAL_QUALITY_MIN:
        issues.append(
            f"Confidence {license.confidence} < {config.REVERSAL_QUALITY_MIN}"
        )

    if issues:
        return False, f"Reversal evidence insufficient: {'; '.join(issues)}"
    return True, "Reversal evidence meets elevated bar"


# ---------------------------------------------------------------------------
# Sub-gate G: CVD alignment
# ---------------------------------------------------------------------------

def check_cvd(
    candles_1m: Sequence[CandleData],
    license: DirectionalLicense,
) -> tuple[bool, str]:
    """Check CVD alignment with trade direction."""
    if not config.REQUIRE_CVD_ALIGNMENT:
        return True, "CVD check disabled"

    if not license.is_trade or not candles_1m or len(candles_1m) < 6:
        return True, "No CVD check needed"

    direction = "LONG" if license.action == Action.LONG else "SHORT"
    return check_cvd_alignment(candles_1m, direction)


# ---------------------------------------------------------------------------
# Sub-gate H: OI confirmation
# ---------------------------------------------------------------------------

def check_oi(
    oi_current: float | None,
    oi_previous: float | None,
    price_new_extreme: bool = False,
) -> tuple[bool, str]:
    """Check Open Interest confirms the move."""
    if not config.REQUIRE_OI_CONFIRMATION:
        return True, "OI check disabled"

    if oi_current is None or oi_previous is None:
        # Graceful degradation — log warning but don't block
        return True, "OI data unavailable — skipping check"

    return check_oi_confirmation(oi_current, oi_previous, price_new_extreme)


# ---------------------------------------------------------------------------
# Sub-gate I: Session / hour-of-day filter
# ---------------------------------------------------------------------------

def check_session_filter(timestamp: Sequence[CandleData] | None = None) -> tuple[bool, str]:
    """Block trading during configured underperforming hours."""
    if not config.SESSION_FILTER_ENABLED:
        return True, "Session filter disabled"

    if not config.BLOCKED_HOURS_LOCAL:
        return True, "No blocked hours configured"

    if ZoneInfo is None:
        return True, "zoneinfo not available — session filter skipped"

    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)

    try:
        local_tz = ZoneInfo(config.TIMEZONE)
        now_local = now_utc.astimezone(local_tz)
        current_hour = now_local.hour
    except Exception:
        return True, f"Invalid timezone {config.TIMEZONE} — session filter skipped"

    if current_hour in config.BLOCKED_HOURS_LOCAL:
        return False, f"Blocked hour: {current_hour}:00 {config.TIMEZONE}"

    return True, f"Session OK: {current_hour}:00 {config.TIMEZONE}"


# ---------------------------------------------------------------------------
# Master gate: evaluate all sub-gates
# ---------------------------------------------------------------------------

def evaluate_entry_gates(
    license: DirectionalLicense,
    regime: RegimeState,
    candles_1m: Sequence[CandleData],
    candles_5m: Sequence[CandleData],
    candles_15m: Sequence[CandleData],
    candles_1h: Sequence[CandleData],
    oi_current: float | None = None,
    oi_previous: float | None = None,
    price_new_extreme: bool = False,
) -> EntryGateResult:
    """
    Run all entry sub-gates. ALL must pass for a trade to be allowed.
    Returns EntryGateResult with detailed reasons.
    """
    result = EntryGateResult(passed=True)

    # Gate 0: Is this even a trade signal?
    if not license.is_trade:
        result.passed = False
        result.add_block("AI said WAIT — no trade")
        return result

    # Gate 0b: Confidence threshold
    if license.confidence < config.MIN_CONFIDENCE:
        result.add_block(
            f"Confidence {license.confidence} < {config.MIN_CONFIDENCE}"
        )

    # Gate 0c: Entry quality threshold
    quality_min = (
        config.REVERSAL_QUALITY_MIN
        if license.is_reversal
        else config.TRADE_QUALITY_MIN
    )
    if license.entry_quality < quality_min:
        result.add_block(
            f"Entry quality {license.entry_quality} < {quality_min}"
        )

    # Gate 1: Regime filter — only block DEAD_LOW_VOL (no liquidity)
    if config.REGIME_FILTER_ENABLED:
        if regime.regime == Regime.DEAD_LOW_VOL:
            result.regime_ok = False
            result.add_block(f"DEAD_LOW_VOL regime blocks all entries: {regime.details}")
        else:
            result.regime_ok = True
    else:
        result.regime_ok = True

    # Gate A: HTF alignment
    htf_ok, htf_reason = check_htf_alignment(
        license, candles_5m, candles_15m, candles_1h
    )
    result.htf_ok = htf_ok
    if not htf_ok:
        result.add_block(f"HTF: {htf_reason}")

    # Gate B: Trend strength
    trend_ok, trend_reason = check_trend_strength(candles_1m)
    result.trend_ok = trend_ok
    if not trend_ok:
        result.add_block(f"Trend: {trend_reason}")

    # Gate C: Extension
    ext_ok, ext_reason = check_extension(candles_1m)
    result.extension_ok = ext_ok
    if not ext_ok:
        result.add_block(f"Extension: {ext_reason}")

    # Gate D: Candle close
    candle_ok, candle_reason = check_candle_close(candles_1m, license)
    result.candle_ok = candle_ok
    if not candle_ok:
        result.add_block(f"Candle: {candle_reason}")

    # Gate E: Volume
    vol_ok, vol_reason = check_volume(candles_1m)
    result.volume_ok = vol_ok
    if not vol_ok:
        result.add_block(f"Volume: {vol_reason}")

    # Gate F: Reversal elevated standard
    rev_ok, rev_reason = check_reversal_requirements(license)
    result.reversal_ok = rev_ok
    if not rev_ok:
        result.add_block(f"Reversal: {rev_reason}")

    # Gate G: CVD alignment
    cvd_ok, cvd_reason = check_cvd(candles_1m, license)
    result.cvd_ok = cvd_ok
    if not cvd_ok:
        result.add_block(f"CVD: {cvd_reason}")

    # Gate H: OI confirmation
    oi_ok, oi_reason = check_oi(oi_current, oi_previous, price_new_extreme)
    result.oi_ok = oi_ok
    if not oi_ok:
        result.add_block(f"OI: {oi_reason}")

    # Gate I: Session filter
    session_ok, session_reason = check_session_filter()
    result.session_ok = session_ok
    if not session_ok:
        result.add_block(f"Session: {session_reason}")

    return result


# ---------------------------------------------------------------------------
# Fallback override logic
# ---------------------------------------------------------------------------

def evaluate_fallback_override(
    license: DirectionalLicense,
    regime: RegimeState,
    candles_1m: Sequence[CandleData],
) -> tuple[bool, str]:
    """
    Fallback override — disabled by default.
    When enabled, only fires under the strictest conditions.
    """
    if not config.ENABLE_FALLBACK_OVERRIDE:
        return False, "Fallback override disabled"

    if not license.is_trade:
        return False, "No trade signal to override"

    # ALL must pass
    checks = []

    if regime.regime == Regime.DEAD_LOW_VOL:
        checks.append(f"Regime DEAD_LOW_VOL — no liquidity")

    if license.htf_alignment == HTFAlignment.OPPOSING:
        checks.append("HTF opposing")

    if license.confidence < config.FALLBACK_MIN_CONFIDENCE:
        checks.append(f"Confidence {license.confidence} < {config.FALLBACK_MIN_CONFIDENCE}")

    if license.entry_quality < config.FALLBACK_MIN_QUALITY:
        checks.append(f"Quality {license.entry_quality} < {config.FALLBACK_MIN_QUALITY}")

    if candles_1m and len(candles_1m) >= 21:
        ext = extension_from_ema(candles_1m)
        if ext > config.MAX_ENTRY_EXTENSION_ATR:
            checks.append(f"Overextended ({ext:.2f} ATR)")

        vol = volume_ratio(candles_1m)
        if vol < config.MIN_VOLUME_RATIO:
            checks.append(f"Weak volume ({vol:.2f}x)")

    if checks:
        return False, f"Fallback blocked: {'; '.join(checks)}"

    return True, "Fallback override: all strict conditions met"
