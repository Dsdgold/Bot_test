"""
Autonomous Self-Optimization Engine (Phase 12).
Three-tier parameter tuning with probation, rollback, and lockout.
"""
import logging
import os
from datetime import datetime
from typing import Optional

from .persistence import BotDatabase

logger = logging.getLogger("self_optimizer")

# Tier 1: Full autonomy — safe to auto-adjust within bounds
TIER_1_PARAMS = {
    "MIN_CONFIDENCE": {"min": 55, "max": 80, "max_change": 3, "type": float},
    "TRADE_QUALITY_MIN": {"min": 60, "max": 90, "max_change": 3, "type": int},
    "MAX_ENTRY_EXTENSION_ATR": {"min": 0.3, "max": 1.2, "max_change": 0.1, "type": float},
    "MIN_VOLUME_RATIO": {"min": 1.0, "max": 2.0, "max_change": 0.1, "type": float},
    "ADX_MIN": {"min": 14, "max": 28, "max_change": 2, "type": float},
    "CHOP_MAX": {"min": 50, "max": 70, "max_change": 2, "type": float},
    "REENTRY_COOLDOWN_CANDLES": {"min": 2, "max": 8, "max_change": 1, "type": int},
    "POST_LOSS_COOLDOWN_SEC": {"min": 300, "max": 1800, "max_change": 120, "type": int},
}

# Tier 2: Supervised autonomy — auto-apply with rollback probation
TIER_2_PARAMS = {
    "TARGET_RR_A_PLUS": {"min": 1.2, "max": 2.5, "max_change": 0.15, "type": float},
    "TARGET_RR_A": {"min": 1.0, "max": 2.0, "max_change": 0.1, "type": float},
    "ATR_STOP_MULT": {"min": 0.7, "max": 1.5, "max_change": 0.1, "type": float},
    "MIN_NET_RR": {"min": 1.0, "max": 1.5, "max_change": 0.05, "type": float},
    "BASE_RISK_PER_TRADE_PCT": {"min": 0.5, "max": 2.0, "max_change": 0.15, "type": float},
    "REVERSAL_QUALITY_MIN": {"min": 70, "max": 95, "max_change": 3, "type": int},
}

# Tier 3: Manual only — never auto-adjusted
TIER_3_PARAMS = [
    "MAX_LEVERAGE", "DAILY_MAX_LOSS_PCT", "WEEKLY_MAX_LOSS_PCT",
    "EQUITY_FLOOR_USDT", "DD_HALT_PCT", "MAX_OPEN_POSITIONS",
    "ENABLE_FALLBACK_OVERRIDE", "POSITION_SIZING_MODE",
]


class SelfOptimizer:
    """Autonomous self-tuning engine with tier-based safety."""

    def __init__(self, db: BotDatabase, config=None):
        self.db = db
        self.config = config
        self.enabled = True
        self.last_tuning_cycle: float = 0
        self.probation_params: dict = {}  # param -> {old_value, new_value, start_trade, trades}
        self.lockout: dict = {}  # param -> lockout_until_cycle
        self.cycle_count: int = 0
        self.total_trades_at_cycle: int = 0

    def get_param_tier(self, param_name: str) -> int:
        """Get the tier for a parameter."""
        if param_name in TIER_1_PARAMS:
            return 1
        elif param_name in TIER_2_PARAMS:
            return 2
        elif param_name in TIER_3_PARAMS:
            return 3
        return 0

    def get_param_bounds(self, param_name: str) -> Optional[dict]:
        """Get bounds for a tunable parameter."""
        if param_name in TIER_1_PARAMS:
            return TIER_1_PARAMS[param_name]
        elif param_name in TIER_2_PARAMS:
            return TIER_2_PARAMS[param_name]
        return None

    def can_tune(self, drawdown_pct: float, trades_since_last: int) -> tuple[bool, str]:
        """Check if tuning cycle can run."""
        if not self.enabled:
            return False, "Tuning disabled"

        freeze_dd = getattr(self.config, "tuning_freeze_dd_pct", 5.0) if self.config else 5.0
        if drawdown_pct >= freeze_dd:
            return False, f"Drawdown {drawdown_pct:.1f}% >= freeze threshold {freeze_dd}%"

        min_sample = getattr(self.config, "tuning_min_sample", 30) if self.config else 30
        if trades_since_last < min_sample:
            return False, f"Insufficient trades: {trades_since_last} < {min_sample}"

        return True, "OK"

    def propose_change(
        self,
        param_name: str,
        current_value,
        recommended_value,
        evidence: str = "",
        sample_size: int = 0,
        confidence: float = 0,
    ) -> Optional[dict]:
        """Propose a parameter change with bounds enforcement."""
        bounds = self.get_param_bounds(param_name)
        if not bounds:
            return None

        tier = self.get_param_tier(param_name)

        # Tier 3 — never auto-apply
        if tier == 3:
            logger.info(f"TIER 3 recommendation: {param_name} → {recommended_value} (requires manual approval)")
            self.db.save_parameter_change({
                "parameter_name": param_name,
                "old_value": current_value,
                "new_value": recommended_value,
                "tier": 3,
                "trigger": evidence,
                "sample_size": sample_size,
                "statistical_confidence": confidence,
                "status": "MANUAL_PENDING",
            })
            return None

        # Check lockout
        if param_name in self.lockout and self.cycle_count < self.lockout[param_name]:
            logger.info(f"LOCKED OUT: {param_name} until cycle {self.lockout[param_name]}")
            return None

        # Clamp change to max_change
        max_change = bounds["max_change"]
        change = recommended_value - current_value
        if abs(change) > max_change:
            change = max_change if change > 0 else -max_change
            recommended_value = current_value + change

        # Clamp to bounds
        recommended_value = max(bounds["min"], min(recommended_value, bounds["max"]))

        if recommended_value == current_value:
            return None

        # Check max concurrent probations for Tier 2
        if tier == 2:
            max_probations = getattr(self.config, "max_concurrent_probations", 2) if self.config else 2
            active_probations = sum(1 for v in self.probation_params.values() if v.get("active", False))
            if active_probations >= max_probations:
                logger.info(f"Max probations ({max_probations}) reached — queueing {param_name}")
                return None

        result = {
            "parameter_name": param_name,
            "old_value": current_value,
            "new_value": bounds["type"](recommended_value),
            "tier": tier,
            "trigger": evidence,
            "sample_size": sample_size,
            "statistical_confidence": confidence,
        }

        return result

    def apply_change(self, change: dict, total_trades: int = 0):
        """Apply a parameter change."""
        tier = change["tier"]
        param = change["parameter_name"]

        if tier == 1:
            change["status"] = "APPLIED"
            logger.info(
                f"TIER 1 AUTO-APPLY: {param} {change['old_value']} → {change['new_value']}"
            )
        elif tier == 2:
            change["status"] = "PROBATION"
            probation_trades = getattr(self.config, "probation_trades", 30) if self.config else 30
            self.probation_params[param] = {
                "old_value": change["old_value"],
                "new_value": change["new_value"],
                "start_trade": total_trades,
                "end_trade": total_trades + probation_trades,
                "active": True,
                "pre_expectancy": change.get("pre_change_expectancy", 0),
            }
            logger.info(
                f"TIER 2 PROBATION: {param} {change['old_value']} → {change['new_value']} "
                f"(probation: {probation_trades} trades)"
            )

        self.db.save_parameter_change(change)

    def check_probation(self, total_trades: int, recent_expectancy: float) -> list:
        """Check if any probation parameters should be confirmed or rolled back."""
        actions = []

        for param, info in list(self.probation_params.items()):
            if not info.get("active"):
                continue

            if total_trades < info["end_trade"]:
                continue

            # Probation period complete — evaluate
            pre_exp = info.get("pre_expectancy", 0)
            rollback_threshold = getattr(self.config, "rollback_threshold_pct", 15) if self.config else 15

            if pre_exp > 0 and recent_expectancy < pre_exp * (1 - rollback_threshold / 100):
                # ROLLBACK
                actions.append({
                    "action": "ROLLBACK",
                    "parameter_name": param,
                    "old_value": info["new_value"],
                    "new_value": info["old_value"],
                    "reason": f"Post-change expectancy {recent_expectancy:.2f} < "
                              f"pre-change {pre_exp:.2f} by > {rollback_threshold}%",
                })

                # Lockout
                lockout_cycles = getattr(self.config, "lockout_cycles", 5) if self.config else 5
                self.lockout[param] = self.cycle_count + lockout_cycles

                logger.warning(
                    f"ROLLBACK: {param} {info['new_value']} → {info['old_value']} "
                    f"(locked out for {lockout_cycles} cycles)"
                )
            else:
                # CONFIRM
                actions.append({
                    "action": "CONFIRM",
                    "parameter_name": param,
                    "value": info["new_value"],
                })
                logger.info(f"CONFIRMED: {param} = {info['new_value']}")

            info["active"] = False

        return actions

    def run_tuning_cycle(self, recommendations: list, total_trades: int):
        """Execute a tuning cycle with the given recommendations."""
        self.cycle_count += 1
        self.total_trades_at_cycle = total_trades

        applied = []
        for rec in recommendations:
            change = self.propose_change(
                param_name=rec.get("parameter_name", ""),
                current_value=rec.get("current_value", 0),
                recommended_value=rec.get("recommended_value", 0),
                evidence=rec.get("evidence", ""),
                sample_size=rec.get("sample_size", 0),
                confidence=rec.get("confidence", 0),
            )
            if change:
                self.apply_change(change, total_trades)
                applied.append(change)

        return applied
