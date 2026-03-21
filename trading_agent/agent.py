"""Main trading agent — Directional License flow.

Orchestrates: data fetch → regime classification → AI license →
deterministic entry gates → execution (or WAIT).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

from trading_agent import config
from trading_agent.ai_brain import get_directional_license
from trading_agent.indicators import classify_regime, volume_ratio, extension_from_ema
from trading_agent.log_sanitizer import setup_sanitized_logging
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, EntryGateResult, RegimeState,
)
from trading_agent.strategy import evaluate_entry_gates, evaluate_fallback_override

logger = logging.getLogger(__name__)


class TradingAgent:
    """Main bot orchestrator using the Directional License model."""

    def __init__(self):
        self.current_license: Optional[DirectionalLicense] = None
        self.is_running = False
        setup_sanitized_logging()

    async def evaluate_market(
        self,
        candles_1m: Sequence[CandleData],
        candles_5m: Sequence[CandleData],
        candles_15m: Sequence[CandleData],
        candles_1h: Sequence[CandleData],
    ) -> tuple[DirectionalLicense, EntryGateResult]:
        """
        Full evaluation cycle:
        1. Classify regime (deterministic)
        2. Get directional license from AI
        3. Apply entry gates (deterministic)
        4. Return decision
        """
        # Step 1: Regime classification — deterministic, not overridable
        regime = classify_regime(candles_1m)
        logger.info(f"Regime: {regime.regime.value} — {regime.details}")

        # Step 2: Build indicator summary for AI
        indicators = {}
        if candles_1m and len(candles_1m) >= 21:
            indicators["volume_ratio"] = f"{volume_ratio(candles_1m):.2f}x"
            indicators["extension_atr"] = f"{extension_from_ema(candles_1m):.2f}"

        # Step 3: Get AI directional license
        license = await get_directional_license(
            candles_1m, candles_5m, candles_15m, candles_1h,
            regime, indicators,
        )
        logger.info(
            f"AI License: {license.action.value} "
            f"conf={license.confidence} quality={license.entry_quality} "
            f"setup={license.setup_type.value} htf={license.htf_alignment.value}"
        )

        # Step 4: Apply deterministic entry gates
        gate_result = evaluate_entry_gates(
            license, regime, candles_1m, candles_5m, candles_15m, candles_1h,
        )

        if gate_result.passed:
            logger.info("ALL GATES PASSED — trade signal confirmed")
            self.current_license = license
        else:
            logger.info(f"BLOCKED — {len(gate_result.reasons)} gate(s) failed:")
            for reason in gate_result.reasons:
                logger.info(f"  ✗ {reason}")

            # Check fallback override (disabled by default)
            if license.is_trade and config.ENABLE_FALLBACK_OVERRIDE:
                fb_ok, fb_reason = evaluate_fallback_override(
                    license, regime, candles_1m
                )
                if fb_ok:
                    logger.info(f"FALLBACK OVERRIDE: {fb_reason}")
                    gate_result.passed = True
                    gate_result.reasons.append(f"OVERRIDE: {fb_reason}")
                else:
                    logger.info(f"Fallback also blocked: {fb_reason}")

        return license, gate_result
