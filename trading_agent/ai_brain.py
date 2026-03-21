"""AI Brain — LLM-powered directional license issuer.

The AI evaluates higher-timeframe context, regime, and directional bias
to issue a Directional License. It does NOT decide exact entry timing.
WAIT is the default. Trading is the exception.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional, Sequence

from trading_agent import config
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, HTFAlignment,
    Regime, RegimeState, SetupType,
)

logger = logging.getLogger(__name__)

# Strategy prompt is now managed by StrategyEvolution (stored in DB).
# This module-level variable is updated by the bot at startup.
_active_strategy_prompt: str | None = None


def get_system_prompt() -> str:
    """Get the current active strategy prompt (from StrategyEvolution or default)."""
    if _active_strategy_prompt:
        return _active_strategy_prompt
    # Fallback: load from StrategyEvolution directly
    try:
        from trading_agent.strategy_evolution import StrategyEvolution
        evo = StrategyEvolution()
        return evo.current_prompt
    except Exception:
        from trading_agent.strategy_evolution import DEFAULT_STRATEGY
        return DEFAULT_STRATEGY


def set_system_prompt(prompt: str) -> None:
    """Update the active strategy prompt (called when strategy evolves)."""
    global _active_strategy_prompt
    _active_strategy_prompt = prompt
    logger.info(f"AI brain system prompt updated ({len(prompt)} chars)")


def _format_candles_summary(candles: Sequence[CandleData], label: str) -> str:
    """Format candle data into a concise summary for the LLM."""
    if not candles:
        return f"{label}: no data"

    recent = candles[-5:] if len(candles) >= 5 else candles
    lines = [f"{label} (last {len(recent)} candles):"]
    for c in recent:
        direction = "▲" if c.is_bullish else "▼"
        lines.append(
            f"  {direction} O={c.open:.1f} H={c.high:.1f} "
            f"L={c.low:.1f} C={c.close:.1f} V={c.volume:.0f}"
        )

    if len(candles) >= 5:
        highs = [c.high for c in candles[-20:]] if len(candles) >= 20 else [c.high for c in candles]
        lows = [c.low for c in candles[-20:]] if len(candles) >= 20 else [c.low for c in candles]
        lines.append(f"  20-bar range: {min(lows):.1f} - {max(highs):.1f}")

    return "\n".join(lines)


def build_analysis_prompt(
    candles_1m: Sequence[CandleData],
    candles_5m: Sequence[CandleData],
    candles_15m: Sequence[CandleData],
    candles_1h: Sequence[CandleData],
    regime: RegimeState,
    indicators: dict,
) -> str:
    """Build the user prompt with market data for LLM analysis."""
    parts = [
        "Analyze BTCUSDT perpetual futures for a scalping opportunity.",
        "",
        f"CURRENT REGIME: {regime.regime.value} — {regime.details}",
        f"  ADX={regime.adx:.1f}, CHOP={regime.chop:.1f}, "
        f"ATR%={regime.atr_pct:.4f}, BB_width={regime.bb_width:.2f}%, "
        f"EMA_slope={regime.ema_slope:.6f}",
        "",
        _format_candles_summary(candles_1m, "1m"),
        "",
        _format_candles_summary(candles_5m, "5m"),
        "",
        _format_candles_summary(candles_15m, "15m"),
        "",
        _format_candles_summary(candles_1h, "1h"),
        "",
    ]

    if indicators:
        parts.append("INDICATORS:")
        for key, val in indicators.items():
            parts.append(f"  {key}: {val}")
        parts.append("")

    parts.append(
        "Issue a Directional License or WAIT. "
        "Remember: WAIT is correct for unclear conditions."
    )
    return "\n".join(parts)


def parse_ai_response(response_text: str) -> Optional[DirectionalLicense]:
    """Parse LLM JSON response into a DirectionalLicense."""
    try:
        # Strip markdown code fences if present
        text = response_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        data = json.loads(text)

        action = Action(data.get("action", "WAIT").upper())
        confidence = int(data.get("confidence", 0))
        setup_type_val = data.get("setup_type", "NONE").upper()
        htf_val = data.get("htf_alignment", "NEUTRAL").upper()

        try:
            regime = Regime(data.get("regime", "RANGING").upper())
        except ValueError:
            regime = Regime.RANGING

        try:
            setup_type = SetupType(setup_type_val)
        except ValueError:
            setup_type = SetupType.NONE

        try:
            htf_alignment = HTFAlignment(htf_val)
        except ValueError:
            htf_alignment = HTFAlignment.NEUTRAL

        return DirectionalLicense(
            action=action,
            confidence=confidence,
            regime=regime,
            setup_type=setup_type,
            entry_quality=int(data.get("entry_quality", 0)),
            htf_alignment=htf_alignment,
            reason=data.get("reason", ""),
            valid_minutes=config.LICENSE_VALIDITY_MINUTES,
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Failed to parse AI response: {e}")
        logger.debug(f"Raw response: {response_text}")
        return None


def _build_journal_context(journal_insights: list[dict] | None = None) -> str:
    """Build context from recent learning journal entries for AI memory."""
    if not journal_insights:
        return ""

    lines = [
        "",
        "## YOUR ACCUMULATED KNOWLEDGE (from past trades and analysis)",
        "Use these insights to make BETTER decisions. Learn from mistakes.",
        "",
    ]
    for entry in journal_insights[-8:]:  # Last 8 entries max
        etype = entry.get("entry_type", "")
        obs = entry.get("observation", "")[:150]
        conc = entry.get("conclusion", "")[:150]
        icon = {"POST_WIN": "WIN", "POST_LOSS": "LOSS", "POST_SKIP_REVIEW": "SKIP",
                "REGIME_SHIFT": "REGIME", "EDGE_DECAY": "EDGE", "TUNING_CYCLE": "TUNE",
                "META_LEARNING": "META", "ROLLBACK": "ROLLBACK"}.get(etype, etype)
        lines.append(f"- [{icon}] {obs}")
        if conc:
            lines.append(f"  → {conc}")

    lines.append("")
    lines.append("Apply these lessons. Avoid repeating past mistakes.")
    return "\n".join(lines)


async def get_directional_license(
    candles_1m: Sequence[CandleData],
    candles_5m: Sequence[CandleData],
    candles_15m: Sequence[CandleData],
    candles_1h: Sequence[CandleData],
    regime: RegimeState,
    indicators: dict,
    journal_insights: list[dict] | None = None,
) -> DirectionalLicense:
    """
    Query the LLM for a directional license.

    journal_insights: Recent learning journal entries to inject as AI memory.
    Falls back to WAIT if API fails or response is unparseable.
    """
    prompt = build_analysis_prompt(
        candles_1m, candles_5m, candles_15m, candles_1h, regime, indicators
    )

    # Append journal insights as accumulated knowledge
    journal_context = _build_journal_context(journal_insights)
    if journal_context:
        prompt += "\n" + journal_context

    if not config.ANTHROPIC_API_KEY:
        logger.warning("No ANTHROPIC_API_KEY set — returning WAIT")
        return _wait_license("No API key configured")

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=512,
            system=get_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text
        license = parse_ai_response(response_text)

        if license is None:
            return _wait_license("Failed to parse AI response")

        return license

    except Exception as e:
        logger.error(f"AI brain error: {e}")
        return _wait_license(f"AI error: {e}")


def _wait_license(reason: str) -> DirectionalLicense:
    """Return a WAIT license (safe default)."""
    return DirectionalLicense(
        action=Action.WAIT,
        confidence=0,
        regime=Regime.RANGING,
        setup_type=SetupType.NONE,
        entry_quality=0,
        htf_alignment=HTFAlignment.NEUTRAL,
        reason=reason,
    )
