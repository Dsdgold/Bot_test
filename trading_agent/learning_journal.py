"""Learning journal — persistent memory for trade insights and patterns.

Creates entries after trades, on regime shifts, edge decay, and periodic skip reviews.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from trading_agent import config

logger = logging.getLogger(__name__)

_JOURNAL_SQL = """
CREATE TABLE IF NOT EXISTS learning_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    trade_id TEXT,
    trigger TEXT,
    observation TEXT,
    conclusion TEXT,
    confidence_in_conclusion INTEGER DEFAULT 0,
    supporting_sample_size INTEGER DEFAULT 0,
    suggested_action TEXT,
    applied INTEGER DEFAULT 0,
    applied_timestamp TEXT,
    outcome_after_applied TEXT
)
"""


class LearningJournal:
    """Persistent learning memory backed by SQLite."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or config.DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._last_skip_review: float = 0
        self._last_regime: str = ""
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        self._get_conn().execute(_JOURNAL_SQL)
        self._get_conn().commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _insert(self, entry_type: str, trade_id: str | None = None,
                trigger: str = "", observation: str = "",
                conclusion: str = "", confidence: int = 0,
                sample_size: int = 0, suggested_action: str = "") -> int:
        if not config.LEARNING_JOURNAL_ENABLED:
            return 0
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO learning_journal (timestamp_utc, entry_type, trade_id,
               trigger, observation, conclusion, confidence_in_conclusion,
               supporting_sample_size, suggested_action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._now_iso(), entry_type, trade_id, trigger, observation,
             conclusion, confidence, sample_size, suggested_action),
        )
        conn.commit()
        return cursor.lastrowid

    # ------------------------------------------------------------------
    # Post-trade entries
    # ------------------------------------------------------------------

    def record_post_trade(
        self,
        trade_id: str,
        is_win: bool,
        net_pnl: float,
        entry_quality: int,
        confidence: int,
        regime: str,
        setup_type: str,
        mfe: float = 0,
        mae: float = 0,
        hold_sec: int = 0,
        sl_price: float = 0,
        tp_price: float = 0,
        entry_price: float = 0,
    ) -> int:
        """Create journal entry after trade close."""
        if not config.JOURNAL_POST_TRADE:
            return 0

        entry_type = "POST_WIN" if is_win else "POST_LOSS"
        trigger = f"Trade {trade_id} closed: {'WIN' if is_win else 'LOSS'} ${net_pnl:+.2f}"

        observation = (
            f"PnL=${net_pnl:+.2f} | quality={entry_quality} conf={confidence} | "
            f"regime={regime} setup={setup_type} | "
            f"MFE=${mfe:.1f} MAE=${mae:.1f} | hold={hold_sec}s"
        )

        # Build conclusion
        conclusions = []
        if is_win:
            if mfe > 0 and tp_price and entry_price:
                tp_dist = abs(tp_price - entry_price)
                if mfe > tp_dist * 1.5:
                    conclusions.append("TP left significant money on table")
            if hold_sec < 30:
                conclusions.append("Very fast win — possible scalp opportunity")
        else:
            if mfe > 0 and mfe > abs(net_pnl) * 0.3:
                conclusions.append("Trade was profitable before reversing — SL may be too tight")
            if mae > 0 and sl_price and entry_price:
                sl_dist = abs(entry_price - sl_price)
                if mae <= sl_dist * 1.1:
                    conclusions.append("MAE barely exceeded SL — possible stop hunt")
            if confidence > 75 and not is_win:
                conclusions.append("High confidence loss — model may be overconfident")

        conclusion = "; ".join(conclusions) if conclusions else "Normal trade outcome"
        suggestion = ""
        if not is_win and mfe > 0:
            suggestion = "Consider widening SL or using trailing stop"

        return self._insert(
            entry_type=entry_type, trade_id=trade_id, trigger=trigger,
            observation=observation, conclusion=conclusion,
            confidence=70 if conclusions else 50,
            suggested_action=suggestion,
        )

    def record_post_loss_reflection(
        self,
        trade_id: str,
        net_pnl: float,
        recent_losses: int,
        regime: str,
        mae_exceeded_sl: bool,
    ) -> int:
        """Deeper reflection after a loss."""
        trigger = f"Loss reflection: trade {trade_id}, ${net_pnl:+.2f}"
        observation = f"Recent consecutive losses: {recent_losses} | Regime: {regime}"

        conclusions = []
        if mae_exceeded_sl:
            conclusions.append("Stop was hit cleanly — entry location may be poor")
        else:
            conclusions.append("MAE barely passed SL — potential stop hunt")
        if recent_losses >= 3:
            conclusions.append(f"Loss clustering detected ({recent_losses} consecutive)")

        return self._insert(
            entry_type="POST_LOSS", trade_id=trade_id, trigger=trigger,
            observation=observation,
            conclusion="; ".join(conclusions),
            confidence=60,
            sample_size=recent_losses,
            suggested_action="Review entry quality filters" if recent_losses >= 3 else "",
        )

    # ------------------------------------------------------------------
    # Cycle observation (records AI analysis every N cycles)
    # ------------------------------------------------------------------

    def record_cycle_observation(
        self,
        cycle: int,
        price: float,
        regime: str,
        confidence: int,
        entry_quality: int,
        action: str,
        ai_reason: str,
        gate_passed: bool,
        gate_reasons: list[str],
        adx: float = 0,
        chop: float = 0,
        volume_ratio: float = 0,
        num_open_positions: int = 0,
    ) -> int:
        """Record bot's analysis and thought process every cycle for learning."""
        # Build indicator assessment
        indicators = []
        if adx > 0:
            if adx > 40:
                indicators.append(f"ADX={adx:.1f} (STRONG trend)")
            elif adx > 25:
                indicators.append(f"ADX={adx:.1f} (moderate trend)")
            else:
                indicators.append(f"ADX={adx:.1f} (weak/no trend)")
        if chop > 0:
            if chop > 60:
                indicators.append(f"CHOP={chop:.1f} (choppy, avoid)")
            elif chop < 40:
                indicators.append(f"CHOP={chop:.1f} (trending, good)")
            else:
                indicators.append(f"CHOP={chop:.1f} (neutral)")
        if volume_ratio > 0:
            if volume_ratio > 1.5:
                indicators.append(f"Vol={volume_ratio:.2f}x (HIGH — conviction)")
            elif volume_ratio < 0.3:
                indicators.append(f"Vol={volume_ratio:.2f}x (DEAD — avoid)")
            else:
                indicators.append(f"Vol={volume_ratio:.2f}x (normal)")

        indicator_str = " | ".join(indicators) if indicators else "No indicators"

        # Build conclusion with analysis
        conclusions = []
        if gate_passed:
            conclusions.append(f"TRADE {action} executed (conf={confidence}, quality={entry_quality})")
        elif action == "WAIT":
            conclusions.append(f"AI says WAIT — no clear edge (conf={confidence})")
        else:
            conclusions.append(f"{action} signal BLOCKED: {'; '.join(gate_reasons[:3])}")

        if adx > 40 and chop < 40:
            conclusions.append("Strong trending conditions — good for continuation trades")
        elif adx < 20 and chop > 60:
            conclusions.append("Choppy market — should avoid entries")
        if volume_ratio < 0.1:
            conclusions.append("Extremely low volume — market lacks conviction")
        if num_open_positions > 0:
            conclusions.append(f"{num_open_positions} position(s) open — monitoring")

        observation = (
            f"${price:.0f} | {regime} | {indicator_str} | "
            f"Positions: {num_open_positions}\n"
            f"AI: {action} conf={confidence} quality={entry_quality}\n"
            f"Reasoning: {ai_reason[:400]}"
        )

        return self._insert(
            entry_type="CYCLE_OBSERVATION",
            trigger=f"#{cycle} {action} @ ${price:.0f} | {indicator_str}",
            observation=observation,
            conclusion=" | ".join(conclusions),
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Skip review
    # ------------------------------------------------------------------

    def should_run_skip_review(self) -> bool:
        interval = config.JOURNAL_SKIP_REVIEW_INTERVAL_HOURS * 3600
        return (time.time() - self._last_skip_review) >= interval

    def record_skip_review(
        self,
        skip_count: int,
        top_reasons: dict[str, int],
        total_decisions: int,
    ) -> int:
        """Periodic skip review entry."""
        self._last_skip_review = time.time()
        skip_rate = skip_count / total_decisions * 100 if total_decisions > 0 else 0

        observation = (
            f"Skips: {skip_count}/{total_decisions} ({skip_rate:.0f}%) | "
            f"Top reasons: {json.dumps(dict(list(top_reasons.items())[:5]))}"
        )

        conclusion = ""
        suggestion = ""
        if skip_rate > 95:
            conclusion = "Filters may be too restrictive"
            suggestion = "Consider relaxing MIN_VOLUME_RATIO or MAX_ENTRY_EXTENSION_ATR"
        elif skip_rate < 50:
            conclusion = "Filters may be too permissive"
            suggestion = "Consider tightening entry quality or confidence thresholds"

        return self._insert(
            entry_type="POST_SKIP_REVIEW", trigger=f"Periodic review ({skip_rate:.0f}% skip rate)",
            observation=observation, conclusion=conclusion,
            confidence=65, sample_size=total_decisions,
            suggested_action=suggestion,
        )

    # ------------------------------------------------------------------
    # Regime shift
    # ------------------------------------------------------------------

    def record_regime_shift(self, old_regime: str, new_regime: str,
                            duration_min: int = 0, trades_during: int = 0) -> int:
        if not config.JOURNAL_ON_REGIME_SHIFT:
            return 0
        if old_regime == new_regime:
            return 0

        self._last_regime = new_regime
        return self._insert(
            entry_type="REGIME_SHIFT",
            trigger=f"Regime: {old_regime} → {new_regime}",
            observation=f"Duration in {old_regime}: {duration_min}min, trades: {trades_during}",
            conclusion=f"Market structure changed to {new_regime}",
            confidence=80,
        )

    # ------------------------------------------------------------------
    # Edge decay
    # ------------------------------------------------------------------

    def record_edge_decay(self, rolling_wr: float, window: int,
                          potential_causes: list[str]) -> int:
        if not config.JOURNAL_ON_EDGE_DECAY:
            return 0
        return self._insert(
            entry_type="EDGE_DECAY",
            trigger=f"Rolling {window}-trade WR dropped to {rolling_wr:.1f}%",
            observation=f"Potential causes: {'; '.join(potential_causes)}",
            conclusion="Edge may be decaying — review strategy parameters",
            confidence=60,
            sample_size=window,
            suggested_action="Run analytics engine for detailed breakdown",
        )

    # ------------------------------------------------------------------
    # Tuning cycle entries
    # ------------------------------------------------------------------

    def record_tuning_cycle(self, changes: list[dict], rollbacks: list[dict]) -> int:
        return self._insert(
            entry_type="TUNING_CYCLE",
            trigger="Periodic self-optimization cycle",
            observation=f"Changes: {len(changes)}, Rollbacks: {len(rollbacks)}",
            conclusion=json.dumps({"applied": changes, "rolled_back": rollbacks}),
            confidence=70,
            sample_size=len(changes),
        )

    def record_rollback(self, param: str, old_val: str, new_val: str, reason: str) -> int:
        return self._insert(
            entry_type="ROLLBACK",
            trigger=f"Parameter {param} rolled back",
            observation=f"{param}: {new_val} → {old_val}",
            conclusion=reason,
            confidence=80,
        )

    # ------------------------------------------------------------------
    # LLM-powered deep reflection
    # ------------------------------------------------------------------

    async def llm_post_trade_reflection(
        self,
        trade_id: str,
        is_win: bool,
        net_pnl: float,
        entry_quality: int,
        confidence: int,
        regime: str,
        setup_type: str,
        mfe: float = 0,
        mae: float = 0,
        hold_sec: int = 0,
        similar_trades_summary: str = "",
    ) -> int:
        """Use LLM for deeper post-trade analysis with full AI thought process."""
        if not config.ANTHROPIC_API_KEY:
            return self.record_post_trade(
                trade_id, is_win, net_pnl, entry_quality, confidence,
                regime, setup_type, mfe, mae, hold_sec,
            )

        outcome = "WIN" if is_win else "LOSS"

        # Gather recent journal context for pattern recognition
        recent_context = ""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT entry_type, observation, conclusion FROM learning_journal "
                "ORDER BY id DESC LIMIT 10"
            ).fetchall()
            if rows:
                recent_context = "\n".join(
                    f"  [{r[0]}] {(r[1] or '')[:80]} -> {(r[2] or '')[:80]}"
                    for r in rows
                )
        except Exception:
            pass

        # Calculate derived metrics
        rr_ratio = abs(mfe / mae) if mae > 0 else 0
        efficiency = (abs(net_pnl) / mfe * 100) if mfe > 0 else 0

        prompt = (
            f"=== TRADE RESULT ===\n"
            f"Trade ID: {trade_id}\n"
            f"Outcome: {outcome} | PnL: ${net_pnl:+.2f}\n"
            f"Hold time: {hold_sec}s | Entry quality: {entry_quality}/100 | AI confidence: {confidence}/100\n"
            f"Regime: {regime} | Setup type: {setup_type}\n"
            f"MFE (max favorable): ${mfe:.2f} | MAE (max adverse): ${mae:.2f}\n"
            f"R:R achieved: {rr_ratio:.2f} | Capture efficiency: {efficiency:.0f}%\n"
        )
        if similar_trades_summary:
            prompt += f"\nSimilar recent trades:\n{similar_trades_summary}\n"
        if recent_context:
            prompt += f"\nRecent journal entries:\n{recent_context}\n"

        prompt += (
            "\n=== INSTRUCTIONS ===\n"
            "Think deeply about this trade. Write your full thought process.\n"
            "Respond JSON:\n"
            "{\n"
            '  "thoughts": "Your internal reasoning process — what you notice, '
            'what patterns you see, what concerns you, hypotheses about market behavior...",\n'
            '  "observation": "Factual analysis of trade execution and result",\n'
            '  "conclusion": "What this trade teaches us — be specific about entry timing, '
            'regime fit, position management",\n'
            '  "pattern_detected": "Any recurring pattern you notice across recent trades '
            '(or null if none)",\n'
            '  "suggested_action": "Concrete parameter adjustment or behavioral change",\n'
            '  "confidence_in_conclusion": 0-100,\n'
            '  "self_grade": "A/B/C/D/F grade for this trade decision"\n'
            "}\n"
            "Be thorough. 5-8 sentences for thoughts. Be specific and actionable."
        )

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=800,
                system=(
                    "You are an elite trading performance analyst. "
                    "Analyze the trade and respond with ONLY a JSON object. "
                    "Keep all string values SHORT (under 100 chars each). "
                    "Grade each trade honestly."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(l for l in lines if not l.strip().startswith("```"))

            # Try to fix truncated JSON by closing open strings/braces
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Attempt to salvage truncated JSON
                fixed = text.rstrip()
                if fixed.count('"') % 2 == 1:
                    fixed += '"'
                while fixed.count('{') > fixed.count('}'):
                    fixed += '}'
                data = json.loads(fixed)

            # Build rich observation with AI thoughts
            thoughts = data.get("thoughts", "")
            pattern = data.get("pattern_detected", "")
            grade = data.get("self_grade", "?")
            observation_full = (
                f"[Grade: {grade}] {data.get('observation', '')}\n"
                f"[AI Thoughts] {thoughts}\n"
                f"[Pattern] {pattern or 'None detected'}"
            )

            return self._insert(
                entry_type="POST_WIN" if is_win else "POST_LOSS",
                trade_id=trade_id,
                trigger=f"Trade {trade_id}: {outcome} ${net_pnl:+.2f} | Q={entry_quality} C={confidence} | {regime}/{setup_type} | R:R={rr_ratio:.1f}",
                observation=observation_full[:600],
                conclusion=data.get("conclusion", "")[:400],
                confidence=int(data.get("confidence_in_conclusion", 60)),
                suggested_action=data.get("suggested_action", "")[:300],
            )
        except Exception as e:
            logger.warning(f"LLM post-trade reflection failed: {e}")
            return self.record_post_trade(
                trade_id, is_win, net_pnl, entry_quality, confidence,
                regime, setup_type, mfe, mae, hold_sec,
            )

    async def llm_meta_learning(self, recent_entries: list[dict], performance_summary: str) -> int:
        """Cross-cycle meta-learning: find patterns across multiple tuning cycles."""
        if not config.ANTHROPIC_API_KEY:
            return 0

        # Summarize recent journal entries
        entry_summaries = []
        for e in recent_entries[-15:]:
            etype = e.get("entry_type", "")
            obs = (e.get("observation") or "")[:100]
            conc = (e.get("conclusion") or "")[:100]
            entry_summaries.append(f"[{etype}] {obs} → {conc}")

        prompt = (
            f"Accumulated learning journal (last {len(entry_summaries)} entries):\n"
            + "\n".join(entry_summaries)
            + f"\n\nPerformance: {performance_summary}\n\n"
            "Questions to answer (respond JSON only):\n"
            "1. What cross-pattern insights do you see?\n"
            "2. Is the bot over-trading or under-trading?\n"
            "3. What is the single highest-impact improvement?\n"
            "4. Any emerging regime shift patterns?\n"
            "{\"observation\": \"...\", \"conclusion\": \"...\", "
            "\"suggested_action\": \"...\", \"confidence_in_conclusion\": 0-100}\n"
            "Max 5 sentences total."
        )

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=300,
                system="You are a trading systems meta-analyst focused on MAXIMIZING PROFITS. Find patterns that MAKE MONEY. Never suggest trading less or being more conservative. Focus on: what patterns win? How to win bigger? How to enter more winning trades?",
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(l for l in lines if not l.strip().startswith("```"))

            data = json.loads(text)
            return self._insert(
                entry_type="META_LEARNING",
                trigger="Periodic meta-learning cycle",
                observation=data.get("observation", "")[:400],
                conclusion=data.get("conclusion", "")[:400],
                confidence=int(data.get("confidence_in_conclusion", 50)),
                suggested_action=data.get("suggested_action", "")[:300],
            )
        except Exception as e:
            logger.warning(f"Meta-learning LLM call failed: {e}")
            return 0

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_recent_entries(self, limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM learning_journal ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    def get_entry_count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM learning_journal").fetchone()
        return row[0] if row else 0
