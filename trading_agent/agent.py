"""Main trading agent — Directional License flow.

Orchestrates: data fetch → kill switches → regime classification → AI license →
deterministic entry gates → cooldown check → R:R validation → execution (or WAIT).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Sequence

from trading_agent import config
from trading_agent.ai_brain import get_directional_license
from trading_agent.indicators import (
    classify_regime, volume_ratio, extension_from_ema, check_cvd_alignment,
)
from trading_agent.log_sanitizer import setup_sanitized_logging
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, EntryGateResult, Regime, RegimeState,
)
from trading_agent.risk_manager import (
    CooldownState, KillSwitchState, SLTPLevels,
    calculate_dynamic_sl_tp, check_cooldowns, check_kill_switches,
)
from trading_agent.strategy import evaluate_entry_gates, evaluate_fallback_override

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logger = logging.getLogger(__name__)


def _get_local_hour() -> int:
    """Get current hour in configured timezone."""
    now_utc = datetime.now(timezone.utc)
    if ZoneInfo:
        try:
            local = now_utc.astimezone(ZoneInfo(config.TIMEZONE))
            return local.hour
        except Exception:
            pass
    return now_utc.hour


class TradingAgent:
    """Main bot orchestrator using the Directional License model."""

    def __init__(self):
        self.current_license: Optional[DirectionalLicense] = None
        self.is_running = False
        self.cooldown = CooldownState()
        self.kill_switch_state = KillSwitchState()
        self.candle_index = 0
        setup_sanitized_logging()

    def _log_decision(
        self,
        license: DirectionalLicense,
        gate_result: EntryGateResult,
        regime: RegimeState,
        sl_tp: Optional[SLTPLevels] = None,
        kill_state: Optional[KillSwitchState] = None,
        cvd_aligned: bool = False,
        oi_confirmed: bool = False,
        vol_ratio: float = 0.0,
        ext_atr: float = 0.0,
        trade_source: str = "AI",
    ) -> None:
        """Structured observability log for every decision."""
        action = license.action.value
        passed = gate_result.passed

        entry = {
            "trade_source": trade_source,
            "regime": regime.regime.value,
            "entry_quality": license.entry_quality,
            "confidence": license.confidence,
            "extension_atr": f"{ext_atr:.2f}",
            "htf_alignment": license.htf_alignment.value,
            "cvd_aligned": cvd_aligned,
            "oi_confirmed": oi_confirmed,
            "volume_ratio": f"{vol_ratio:.2f}",
            "net_rr": f"{sl_tp.net_rr:.2f}" if sl_tp else "N/A",
            "session_hour": _get_local_hour(),
            "entry_type": sl_tp.entry_type if sl_tp else "N/A",
        }

        if passed:
            entry["entry_reason"] = license.reason
            logger.info(f"TRADE SIGNAL: {action} | {entry}")
        else:
            entry["skip_reason"] = "; ".join(gate_result.reasons[:3])
            if kill_state and kill_state.any_active:
                entry["kill_switches"] = "; ".join(kill_state.reasons())
            logger.info(f"SKIP: {action} | {entry}")

    async def evaluate_market(
        self,
        candles_1m: Sequence[CandleData],
        candles_5m: Sequence[CandleData],
        candles_15m: Sequence[CandleData],
        candles_1h: Sequence[CandleData],
        spread: float = 0.0,
        funding_rate: float = 0.0,
        latency_ms: float = 0.0,
        oi_current: float | None = None,
        oi_previous: float | None = None,
        price_new_extreme: bool = False,
    ) -> tuple[DirectionalLicense, EntryGateResult, Optional[SLTPLevels]]:
        """
        Full evaluation cycle:
        1. Check kill switches
        2. Classify regime (deterministic)
        3. Get directional license from AI
        4. Apply entry gates (deterministic, incl. CVD/OI/session)
        5. Check cooldowns
        6. Calculate SL/TP with fee-aware R:R
        7. Return decision
        """
        self.candle_index += 1
        sl_tp: Optional[SLTPLevels] = None

        # Step 1: Kill switches — check before any analysis
        direction_hint = None
        kill_state = check_kill_switches(
            spread=spread,
            funding_rate=funding_rate,
            latency_ms=latency_ms,
            trade_direction=direction_hint,
        )
        self.kill_switch_state = kill_state

        if kill_state.any_active:
            from trading_agent.ai_brain import _wait_license
            license = _wait_license(f"Kill switch active: {'; '.join(kill_state.reasons())}")
            gate_result = EntryGateResult(passed=False)
            gate_result.add_block(f"Kill switch: {'; '.join(kill_state.reasons())}")
            self._log_decision(license, gate_result, RegimeState(
                regime=Regime.RANGING,
                adx=0, chop=100, atr_pct=0, bb_width=0, ema_slope=0,
            ), kill_state=kill_state)
            return license, gate_result, None

        # Step 2: Regime classification
        regime = classify_regime(candles_1m)
        logger.info(f"Regime: {regime.regime.value} — {regime.details}")

        # Step 3: Build indicator summary for AI
        indicators = {}
        vol_ratio = 0.0
        ext_atr = 0.0
        if candles_1m and len(candles_1m) >= 21:
            vol_ratio = volume_ratio(candles_1m)
            ext_atr = extension_from_ema(candles_1m)
            indicators["volume_ratio"] = f"{vol_ratio:.2f}x"
            indicators["extension_atr"] = f"{ext_atr:.2f}"

        # Step 4: Get AI directional license
        license = await get_directional_license(
            candles_1m, candles_5m, candles_15m, candles_1h,
            regime, indicators,
        )
        logger.info(
            f"AI License: {license.action.value} "
            f"conf={license.confidence} quality={license.entry_quality} "
            f"setup={license.setup_type.value} htf={license.htf_alignment.value}"
        )

        # Re-check funding kill switch with actual direction
        if license.is_trade:
            direction = "LONG" if license.action == Action.LONG else "SHORT"
            kill_state = check_kill_switches(
                spread=spread,
                funding_rate=funding_rate,
                latency_ms=latency_ms,
                trade_direction=direction,
            )
            self.kill_switch_state = kill_state

            if kill_state.any_active:
                gate_result = EntryGateResult(passed=False)
                gate_result.add_block(f"Kill switch: {'; '.join(kill_state.reasons())}")
                self._log_decision(
                    license, gate_result, regime,
                    kill_state=kill_state, vol_ratio=vol_ratio, ext_atr=ext_atr,
                )
                return license, gate_result, None

        # Step 5: Apply deterministic entry gates (incl. CVD, OI, session)
        gate_result = evaluate_entry_gates(
            license, regime, candles_1m, candles_5m, candles_15m, candles_1h,
            oi_current=oi_current, oi_previous=oi_previous,
            price_new_extreme=price_new_extreme,
        )

        # Step 6: Cooldown check
        if gate_result.passed and license.is_trade:
            direction = "LONG" if license.action == Action.LONG else "SHORT"
            cd_ok, cd_reason = check_cooldowns(
                self.cooldown, direction, self.candle_index
            )
            if not cd_ok:
                gate_result.add_block(f"Cooldown: {cd_reason}")

        # Step 7: SL/TP and R:R validation
        if gate_result.passed and license.is_trade and candles_1m:
            entry_price = candles_1m[-1].close
            is_maker = config.PREFER_POST_ONLY_ENTRIES
            sl_tp = calculate_dynamic_sl_tp(
                entry_price, license, candles_1m, is_maker=is_maker,
            )

            if not sl_tp.is_valid:
                gate_result.add_block(
                    f"Net R:R {sl_tp.net_rr:.2f} < {config.MIN_NET_RR} — {sl_tp.details}"
                )

        # Final decision
        cvd_aligned = gate_result.cvd_ok
        oi_confirmed = gate_result.oi_ok

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

        # Observability log
        self._log_decision(
            license, gate_result, regime,
            sl_tp=sl_tp, kill_state=kill_state,
            cvd_aligned=cvd_aligned, oi_confirmed=oi_confirmed,
            vol_ratio=vol_ratio, ext_atr=ext_atr,
        )

        return license, gate_result, sl_tp

    def record_trade_result(self, direction: str, is_win: bool) -> None:
        """Record a trade result for cooldown tracking."""
        self.cooldown.record_trade(direction, is_win, self.candle_index)
