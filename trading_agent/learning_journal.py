"""
Learning Journal (Phase 7.5).
The bot's memory — records observations and conclusions after every trade outcome.
"""
import logging
import time
from datetime import datetime
from typing import Optional

from .ai_brain import ClaudeAIBrain
from .models import Position, Trade
from .persistence import BotDatabase

logger = logging.getLogger("learning_journal")


class LearningJournal:
    """Manages the bot's self-reflection and learning journal."""

    def __init__(self, db: BotDatabase, ai_brain: Optional[ClaudeAIBrain] = None, config=None):
        self.db = db
        self.ai_brain = ai_brain
        self.config = config
        self.last_skip_review: float = 0
        self.skip_review_interval: int = 4 * 3600  # 4 hours

    async def record_post_trade(self, trade: Trade, position: Optional[Position] = None):
        """Record learning entry after a trade closes."""
        entry_type = "POST_WIN" if trade.pnl > 0 else "POST_LOSS"

        # Build context for LLM reflection
        context = (
            f"Trade closed: {trade.side.value} {'+'if trade.pnl > 0 else ''}"
            f"${trade.pnl:.2f} | {trade.hold_duration_sec}s hold | "
            f"Quality {trade.entry_quality} | Confidence {trade.signal_confidence:.0f}\n"
            f"Regime: {trade.regime} | Setup: {trade.setup_type} | "
            f"HTF: {trade.htf_alignment}\n"
        )

        if position:
            context += (
                f"MFE: ${position.max_favorable_excursion:.2f} | "
                f"MAE: ${position.max_adverse_excursion:.2f}\n"
            )

        context += f"Exit: {trade.reason}\n"

        # Try LLM reflection
        observation = f"{trade.side.value} {entry_type.lower()} ${trade.pnl:+.2f}"
        conclusion = ""
        suggested_action = ""
        confidence = 0

        if self.ai_brain and self.ai_brain.enabled:
            try:
                result = await self.ai_brain.generate_journal_entry(
                    context, max_tokens=150
                )
                if result:
                    observation = result.get("observation", observation)
                    conclusion = result.get("conclusion", "")
                    suggested_action = result.get("suggested_action", "")
                    confidence = result.get("confidence_in_conclusion", 0)
            except Exception as e:
                logger.error(f"Journal LLM failed: {e}")

        self.db.save_journal_entry({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "entry_type": entry_type,
            "trade_id": trade.trade_id,
            "trigger": f"Trade closed: {trade.reason}",
            "observation": observation,
            "conclusion": conclusion,
            "confidence_in_conclusion": confidence,
            "suggested_action": suggested_action,
        })

        logger.info(f"Journal [{entry_type}]: {observation[:100]}")

    async def maybe_skip_review(self, skip_count: int, skip_reasons: dict):
        """Periodic skip review — every 4 hours of bot uptime."""
        now = time.time()
        if now - self.last_skip_review < self.skip_review_interval:
            return

        self.last_skip_review = now

        if skip_count == 0:
            return

        top_reasons = sorted(skip_reasons.items(), key=lambda x: -x[1])[:5]
        reasons_str = ", ".join(f"{r}: {c}" for r, c in top_reasons)

        observation = f"{skip_count} trades skipped. Top reasons: {reasons_str}"
        conclusion = "Filters are actively blocking trades."
        suggested_action = "Review skip reasons — verify filters are not too tight."

        if self.ai_brain and self.ai_brain.enabled:
            try:
                context = (
                    f"Skip review: {skip_count} skips in last 4h.\n"
                    f"Top reasons: {reasons_str}\n"
                    f"Respond JSON: observation, conclusion, suggested_action, confidence_in_conclusion"
                )
                result = await self.ai_brain.generate_journal_entry(context, max_tokens=300)
                if result:
                    observation = result.get("observation", observation)
                    conclusion = result.get("conclusion", conclusion)
                    suggested_action = result.get("suggested_action", suggested_action)
            except Exception:
                pass

        self.db.save_journal_entry({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "entry_type": "POST_SKIP_REVIEW",
            "trigger": f"{skip_count} skips in review period",
            "observation": observation,
            "conclusion": conclusion,
            "suggested_action": suggested_action,
        })

        logger.info(f"Journal [SKIP_REVIEW]: {observation[:100]}")

    def record_regime_shift(self, old_regime: str, new_regime: str):
        """Record when market regime changes."""
        self.db.save_journal_entry({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "entry_type": "REGIME_SHIFT",
            "trigger": f"Regime: {old_regime} → {new_regime}",
            "observation": f"Market regime shifted from {old_regime} to {new_regime}",
            "conclusion": f"Trading conditions changed — {'expect fewer setups' if new_regime in ('RANGING', 'DEAD_LOW_VOL') else 'opportunities may emerge'}",
        })
        logger.info(f"Journal [REGIME_SHIFT]: {old_regime} → {new_regime}")

    def record_parameter_change(self, param: str, old_val, new_val, reason: str):
        """Record a parameter change in the journal."""
        self.db.save_journal_entry({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "entry_type": "PARAMETER_INSIGHT",
            "trigger": f"Parameter changed: {param}",
            "observation": f"{param}: {old_val} → {new_val}",
            "conclusion": reason,
        })
