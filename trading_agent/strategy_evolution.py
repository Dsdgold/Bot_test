"""Strategy Evolution — AI-driven self-improvement of the trading system prompt.

Every STRATEGY_EVOLUTION_HOURS, the bot analyzes its recent performance,
learning journal, and trade outcomes to rewrite its own strategy instructions.
Each version is stored with full reasoning and can be reviewed/rolled back.
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

_STRATEGY_SQL = """
CREATE TABLE IF NOT EXISTS strategy_evolution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    timestamp_utc TEXT NOT NULL,
    strategy_prompt TEXT NOT NULL,
    change_reasoning TEXT NOT NULL,
    performance_snapshot TEXT NOT NULL,
    trades_since_last INTEGER DEFAULT 0,
    win_rate_at_change REAL DEFAULT 0,
    total_pnl_at_change REAL DEFAULT 0,
    rolled_back INTEGER DEFAULT 0
)
"""

# The original hardcoded prompt — serves as v0 baseline
DEFAULT_STRATEGY = """You are an ACTIVE Bybit BTCUSDT perpetual futures scalping analyst.

## CORE PRINCIPLE
You are an aggressive scalper. Your job is to FIND TRADES, not avoid them.
Issue LONG or SHORT whenever you see a reasonable opportunity. WAIT only when the market is truly dead or chaotic.

## WHEN TO TRADE (most of the time in trending markets)
- Price trending in any direction with decent momentum → TRADE the direction
- Pullback in a trend → TRADE continuation
- Breakout with volume → TRADE the breakout
- Clear momentum shift → TRADE the new direction
- Even moderate setups are tradeable — you learn from every trade
- HTF opposing is a caution flag, NOT a blocker — trade with tighter targets

## WHEN TO SAY WAIT (only these cases)
- Market is completely flat / dead (ATR near zero)
- Pure chop with no direction whatsoever
- Extreme spike with no structure

## SETUP GRADING
- A+ / A setups: Perfect convergence → TRADE with max confidence
- B setups: Most factors align → TRADE (this is your bread and butter)
- C setups: Some factors align → TRADE with lower confidence (50-60)
- D setups: Nothing aligns → WAIT

## YOUR OUTPUT FORMAT
Respond with ONLY a JSON object, no other text:
{
  "action": "LONG" | "SHORT" | "WAIT",
  "confidence": 0-100,
  "regime": "TRENDING" | "RANGING" | "DEAD_LOW_VOL" | "SPIKE_HIGH_VOL",
  "setup_type": "CONTINUATION" | "PULLBACK" | "BREAKOUT_RETEST" | "REVERSAL" | "NONE",
  "entry_quality": 0-100,
  "htf_alignment": "ALIGNED" | "NEUTRAL" | "OPPOSING",
  "reason": "concise explanation"
}

## CONFIDENCE CALIBRATION
- 80-100: Textbook setup, strong convergence
- 60-79: Good setup, tradeable with normal risk
- 50-59: Moderate setup, tradeable with reduced size
- 40-49: Weak but possible — trade if regime is trending
- 0-39: No setup — WAIT

## ENTRY QUALITY CALIBRATION
- 80-100: Perfect zone (pullback to EMA, key level)
- 60-79: Good zone, acceptable entry
- 40-59: Decent zone, slightly extended but tradeable
- 0-39: Poor zone — WAIT

Be DECISIVE. Pick a direction and commit. You LEARN from every trade — wins AND losses make you smarter. Inaction teaches nothing."""


class StrategyEvolution:
    """Manages evolution of the AI trading strategy prompt."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or config.DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._current_prompt: Optional[str] = None
        self._current_version: int = 0
        self._last_evolution: float = 0
        self._init_db()
        self._load_current()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        self._get_conn().execute(_STRATEGY_SQL)
        self._get_conn().commit()

    def _load_current(self) -> None:
        """Load the latest non-rolled-back strategy from DB."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT version, strategy_prompt FROM strategy_evolution "
            "WHERE rolled_back = 0 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row:
            self._current_version = row[0]
            self._current_prompt = row[1]
        else:
            # No strategy in DB yet — seed with default
            self._current_version = 0
            self._current_prompt = DEFAULT_STRATEGY
            self._seed_default()

    def _seed_default(self) -> None:
        """Insert the default strategy as version 0."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO strategy_evolution "
            "(version, timestamp_utc, strategy_prompt, change_reasoning, "
            "performance_snapshot, trades_since_last) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (0, datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             DEFAULT_STRATEGY, "Initial default strategy",
             "{}", 0),
        )
        conn.commit()

    @property
    def current_prompt(self) -> str:
        """Get the active strategy prompt."""
        return self._current_prompt or DEFAULT_STRATEGY

    @property
    def current_version(self) -> int:
        return self._current_version

    def get_history(self, limit: int = 20) -> list[dict]:
        """Get strategy evolution history."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM strategy_evolution ORDER BY version DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    def should_evolve(self) -> bool:
        """Check if it's time for a strategy evolution cycle."""
        if not config.STRATEGY_EVOLUTION_ENABLED:
            return False
        hours_since = (time.time() - self._last_evolution) / 3600
        return hours_since >= config.STRATEGY_EVOLUTION_HOURS

    async def evolve(
        self,
        journal_entries: list[dict],
        performance_summary: str,
        trade_stats: dict,
    ) -> Optional[int]:
        """Run strategy evolution: analyze performance and rewrite the prompt.

        Args:
            journal_entries: Recent learning journal entries
            performance_summary: Text summary of recent performance
            trade_stats: Dict with keys like win_rate, total_pnl, total_trades, etc.

        Returns:
            New version number if evolved, None if skipped.
        """
        self._last_evolution = time.time()

        if not config.ANTHROPIC_API_KEY:
            logger.warning("No API key — skipping strategy evolution")
            return None

        total_trades = trade_stats.get("total_trades", 0)
        if total_trades < config.STRATEGY_EVOLUTION_MIN_TRADES:
            logger.info(
                f"Strategy evolution skipped: only {total_trades} trades "
                f"(need {config.STRATEGY_EVOLUTION_MIN_TRADES})"
            )
            return None

        # Build context from journal
        journal_summary = []
        for e in journal_entries[-25:]:
            etype = e.get("entry_type", "")
            obs = (e.get("observation") or "")[:200]
            conc = (e.get("conclusion") or "")[:200]
            journal_summary.append(f"[{etype}] {obs}")
            if conc:
                journal_summary.append(f"  → {conc}")

        prompt = f"""You are a trading strategy architect. Your task is to IMPROVE the AI scalping bot's system prompt based on real performance data.

## CURRENT STRATEGY (v{self._current_version}):
---
{self._current_prompt}
---

## PERFORMANCE DATA:
{performance_summary}

## TRADE STATISTICS:
- Total trades: {trade_stats.get('total_trades', 0)}
- Wins: {trade_stats.get('wins', 0)}
- Losses: {trade_stats.get('losses', 0)}
- Win rate: {trade_stats.get('win_rate', 0):.1f}%
- Total PnL: ${trade_stats.get('total_pnl', 0):.2f}
- Average win: ${trade_stats.get('avg_win', 0):.2f}
- Average loss: ${trade_stats.get('avg_loss', 0):.2f}
- Best trade: ${trade_stats.get('best_trade', 0):.2f}
- Worst trade: ${trade_stats.get('worst_trade', 0):.2f}

## LEARNING JOURNAL (recent insights):
{chr(10).join(journal_summary)}

## YOUR TASK:
1. Analyze what's working and what's NOT working in the current strategy
2. Write an IMPROVED version of the strategy prompt
3. Keep the JSON output format EXACTLY the same (action, confidence, regime, setup_type, entry_quality, htf_alignment, reason)
4. Focus improvements on the areas where the bot is losing money or making mistakes
5. Be specific — if losses come from reversals, add reversal guidance. If from overtrading in chop, add chop rules.

## RULES:
- Keep the prompt concise (no longer than 50% more than current)
- DO NOT change the JSON output schema
- DO NOT remove the confidence/entry_quality calibration scales
- DO NOT add volume thresholds, ADX thresholds, or other numeric filter rules — those are handled by the deterministic gate system, NOT the AI prompt
- DO NOT make the strategy more conservative or add more WAIT conditions — the bot already has strict deterministic filters
- DO improve trading rules, setup grading, and pattern recognition based on ACTUAL results
- The AI should be AGGRESSIVE about issuing LONG/SHORT signals — let the gate system filter bad ones
- WAIT should ONLY be used when the market is truly dead or completely chaotic
- If performance is good (>55% win rate, positive PnL), make smaller changes
- If performance is bad (<45% win rate, negative PnL), make bigger changes

Respond with ONLY a JSON object:
{{
  "new_strategy": "the complete new strategy prompt text",
  "changes_made": ["list of specific changes"],
  "reasoning": "why these changes should improve results"
}}"""

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=config.LLM_MODEL,
                max_tokens=4096,
                system=(
                    "You are a world-class trading strategy optimizer. "
                    "You analyze real trade results and improve strategy prompts. "
                    "Be specific and data-driven. Every change must be justified by the performance data."
                ),
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(l for l in lines if not l.strip().startswith("```"))

            data = json.loads(text)
            new_strategy = data.get("new_strategy", "")
            changes = data.get("changes_made", [])
            reasoning = data.get("reasoning", "")

            if not new_strategy or len(new_strategy) < 200:
                logger.warning("Strategy evolution returned empty/too-short prompt, skipping")
                return None

            # Validate the new strategy still requires JSON output format
            if '"action"' not in new_strategy or '"confidence"' not in new_strategy:
                logger.warning("New strategy missing required JSON fields, skipping")
                return None

            # Save new version
            new_version = self._current_version + 1
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO strategy_evolution "
                "(version, timestamp_utc, strategy_prompt, change_reasoning, "
                "performance_snapshot, trades_since_last, "
                "win_rate_at_change, total_pnl_at_change) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_version,
                    datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    new_strategy,
                    json.dumps({"changes": changes, "reasoning": reasoning}),
                    json.dumps(trade_stats),
                    total_trades,
                    trade_stats.get("win_rate", 0),
                    trade_stats.get("total_pnl", 0),
                ),
            )
            conn.commit()

            self._current_version = new_version
            self._current_prompt = new_strategy

            logger.info(
                f"Strategy evolved: v{self._current_version - 1} → v{new_version} | "
                f"Changes: {', '.join(changes[:3])}"
            )
            return new_version

        except Exception as e:
            logger.error(f"Strategy evolution failed: {e}")
            return None

    def rollback(self, to_version: Optional[int] = None) -> bool:
        """Rollback to a previous strategy version."""
        conn = self._get_conn()
        if to_version is None:
            to_version = max(0, self._current_version - 1)

        # Mark current as rolled back
        conn.execute(
            "UPDATE strategy_evolution SET rolled_back = 1 WHERE version = ?",
            (self._current_version,),
        )
        conn.commit()

        # Reload
        self._load_current()
        logger.info(f"Strategy rolled back to v{self._current_version}")
        return True

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
