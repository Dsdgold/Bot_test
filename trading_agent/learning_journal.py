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
