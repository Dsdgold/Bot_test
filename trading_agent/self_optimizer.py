"""Autonomous self-optimization engine with tiered safety guardrails.

Tier 1: Full autonomy (within bounds)
Tier 2: Supervised autonomy (probation + rollback)
Tier 3: Manual only (never auto-changed)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from trading_agent import config

logger = logging.getLogger(__name__)

_PARAM_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS parameter_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    parameter_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    tier INTEGER NOT NULL,
    trigger TEXT,
    supporting_evidence TEXT,
    sample_size INTEGER DEFAULT 0,
    statistical_confidence REAL DEFAULT 0,
    status TEXT DEFAULT 'APPLIED',
    probation_start_trade INTEGER,
    probation_end_trade INTEGER,
    pre_change_expectancy REAL,
    post_change_expectancy REAL,
    rollback_reason TEXT
)
"""


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

@dataclass
class ParamSpec:
    """Specification for a tunable parameter."""
    name: str
    tier: int
    min_val: float
    max_val: float
    max_delta: float
    attr_name: str  # attribute name on config module
    is_int: bool = False


# Ranges allow BOTH loosening and tightening — bot learns to TRADE MORE when profitable
TIER_1_PARAMS = [
    ParamSpec("MIN_CONFIDENCE", 1, 20, 70, 5, "MIN_CONFIDENCE", True),
    ParamSpec("TRADE_QUALITY_MIN", 1, 15, 60, 5, "TRADE_QUALITY_MIN", True),
    ParamSpec("MAX_ENTRY_EXTENSION_ATR", 1, 2.0, 6.0, 0.5, "MAX_ENTRY_EXTENSION_ATR"),
    ParamSpec("MIN_VOLUME_RATIO", 1, 0.05, 1.0, 0.1, "MIN_VOLUME_RATIO"),
    ParamSpec("ADX_MIN", 1, 8, 25, 2, "ADX_MIN"),
    ParamSpec("CHOP_MAX", 1, 55, 85, 3, "CHOP_MAX"),
    ParamSpec("REENTRY_COOLDOWN_CANDLES", 1, 0, 5, 1, "REENTRY_COOLDOWN_CANDLES", True),
    ParamSpec("POST_LOSS_COOLDOWN_SEC", 1, 10, 300, 30, "POST_LOSS_COOLDOWN_SEC", True),
]

TIER_2_PARAMS = [
    ParamSpec("TARGET_RR_A_PLUS", 2, 1.0, 3.0, 0.2, "TARGET_RR_A_PLUS"),
    ParamSpec("TARGET_RR_A", 2, 0.8, 2.5, 0.15, "TARGET_RR_A"),
    ParamSpec("ATR_STOP_MULT", 2, 0.5, 1.5, 0.1, "ATR_STOP_MULT"),
    ParamSpec("MIN_NET_RR", 2, 0.3, 1.5, 0.1, "MIN_NET_RR"),
    ParamSpec("BASE_RISK_PER_TRADE_PCT", 2, 5.0, 20.0, 2.0, "BASE_RISK_PER_TRADE_PCT"),
    ParamSpec("REVERSAL_QUALITY_MIN", 2, 30, 80, 5, "REVERSAL_QUALITY_MIN", True),
]

TIER_2_PARAMS += [
    # Former Tier 3 — now autonomous with strict safety bounds
    ParamSpec("MAX_LEVERAGE", 2, 3, 30, 2, "MAX_LEVERAGE", True),
    ParamSpec("DAILY_MAX_LOSS_PCT", 2, 2.0, 5.0, 0.5, "DAILY_MAX_LOSS_PCT"),
    ParamSpec("WEEKLY_MAX_LOSS_PCT", 2, 4.0, 10.0, 1.0, "WEEKLY_MAX_LOSS_PCT"),
    ParamSpec("EQUITY_FLOOR_USDT", 2, 20, 500, 20, "EQUITY_FLOOR_USDT"),
    ParamSpec("DD_HALT_PCT", 2, 8.0, 15.0, 1.0, "DD_HALT_PCT"),
    ParamSpec("TIER_1_EQUITY", 2, 500, 5000, 200, "TIER_1_EQUITY"),
    ParamSpec("TIER_2_EQUITY", 2, 1000, 10000, 500, "TIER_2_EQUITY"),
    ParamSpec("TIER_3_EQUITY", 2, 2500, 20000, 1000, "TIER_3_EQUITY"),
    ParamSpec("TIER_4_EQUITY", 2, 5000, 50000, 2000, "TIER_4_EQUITY"),
]

# Nothing left in Tier 3 — bot is fully autonomous
TIER_3_PARAMS: list[str] = []

ALL_PARAM_SPECS = {p.name: p for p in TIER_1_PARAMS + TIER_2_PARAMS}


# ---------------------------------------------------------------------------
# Self-optimizer
# ---------------------------------------------------------------------------

class SelfOptimizer:
    """Autonomous parameter tuning with safety guardrails."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or config.DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._last_tuning: float = 0
        self._trade_counter: int = 0
        self._lockouts: dict[str, int] = {}  # param → remaining lockout cycles
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        self._get_conn().execute(_PARAM_HISTORY_SQL)
        self._get_conn().commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def increment_trade_counter(self) -> None:
        self._trade_counter += 1

    # ------------------------------------------------------------------
    # Parameter access
    # ------------------------------------------------------------------

    def get_current_value(self, param_name: str) -> float:
        return float(getattr(config, param_name))

    def set_value(self, param_name: str, value: float, spec: ParamSpec) -> None:
        """Set a config parameter value (runtime only)."""
        if spec.is_int:
            setattr(config, param_name, int(value))
        else:
            setattr(config, param_name, value)

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------

    def _check_bounds(self, spec: ParamSpec, new_val: float) -> tuple[bool, str]:
        if new_val < spec.min_val:
            return False, f"{spec.name}={new_val} < min {spec.min_val}"
        if new_val > spec.max_val:
            return False, f"{spec.name}={new_val} > max {spec.max_val}"
        return True, ""

    def _check_max_delta(self, spec: ParamSpec, old_val: float, new_val: float) -> tuple[bool, str]:
        delta = abs(new_val - old_val)
        if delta > spec.max_delta + 1e-9:  # Float tolerance
            return False, f"Delta {delta:.4f} > max {spec.max_delta} for {spec.name}"
        return True, ""

    def _is_locked_out(self, param_name: str) -> bool:
        return self._lockouts.get(param_name, 0) > 0

    def _get_active_probations(self) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM parameter_history WHERE status = 'PROBATION'"
        ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Tier classification
    # ------------------------------------------------------------------

    @staticmethod
    def get_tier(param_name: str) -> int:
        if param_name in ALL_PARAM_SPECS:
            return ALL_PARAM_SPECS[param_name].tier
        if param_name in TIER_3_PARAMS:
            return 3
        return 3  # Unknown → manual only

    # ------------------------------------------------------------------
    # Apply parameter change
    # ------------------------------------------------------------------

    def apply_change(
        self,
        param_name: str,
        new_value: float,
        trigger: str = "",
        evidence: str = "",
        sample_size: int = 0,
        confidence: float = 0,
        drawdown_pct: float = 0,
    ) -> tuple[bool, str]:
        """
        Apply a parameter change respecting all safety guardrails.
        Returns (success, reason).
        """
        if not config.AUTONOMOUS_TUNING_ENABLED:
            return False, "Autonomous tuning disabled"

        tier = self.get_tier(param_name)

        # Tier 3 → never auto-apply
        if tier == 3:
            self._record_change(
                param_name, str(self.get_current_value(param_name)),
                str(new_value), tier, trigger, evidence,
                sample_size, confidence, "MANUAL_PENDING",
            )
            return False, f"Tier 3 parameter — logged as recommendation only"

        spec = ALL_PARAM_SPECS.get(param_name)
        if not spec:
            return False, f"Unknown parameter: {param_name}"

        old_value = self.get_current_value(param_name)

        # Safety checks
        if drawdown_pct >= config.TUNING_FREEZE_DD_PCT:
            return False, f"Tuning frozen: drawdown {drawdown_pct:.1f}% >= {config.TUNING_FREEZE_DD_PCT}%"

        if sample_size < config.TUNING_MIN_SAMPLE:
            return False, f"Insufficient sample: {sample_size} < {config.TUNING_MIN_SAMPLE}"

        if self._is_locked_out(param_name):
            return False, f"Parameter {param_name} locked out ({self._lockouts[param_name]} cycles remaining)"

        bounds_ok, bounds_reason = self._check_bounds(spec, new_value)
        if not bounds_ok:
            return False, bounds_reason

        delta_ok, delta_reason = self._check_max_delta(spec, old_value, new_value)
        if not delta_ok:
            return False, delta_reason

        # Tier-specific handling
        if tier == 1:
            self.set_value(param_name, new_value, spec)
            self._record_change(
                param_name, str(old_value), str(new_value), tier,
                trigger, evidence, sample_size, confidence, "APPLIED",
            )
            logger.info(f"TIER 1 AUTO: {param_name} {old_value} → {new_value}")
            return True, f"Tier 1 applied: {param_name} = {new_value}"

        elif tier == 2:
            # Check probation limit
            active = self._get_active_probations()
            if active >= config.MAX_CONCURRENT_PROBATIONS:
                return False, f"Max concurrent probations ({config.MAX_CONCURRENT_PROBATIONS}) reached"

            self.set_value(param_name, new_value, spec)
            probation_end = self._trade_counter + config.PROBATION_TRADES
            self._record_change(
                param_name, str(old_value), str(new_value), tier,
                trigger, evidence, sample_size, confidence, "PROBATION",
                probation_start=self._trade_counter, probation_end=probation_end,
            )
            logger.info(f"TIER 2 PROBATION: {param_name} {old_value} → {new_value} "
                        f"(probation until trade #{probation_end})")
            return True, f"Tier 2 probation: {param_name} = {new_value} (until trade #{probation_end})"

        return False, "Unexpected tier"

    # ------------------------------------------------------------------
    # Probation check & rollback
    # ------------------------------------------------------------------

    def check_probations(
        self,
        current_wr: float = 0,
        pre_wr: float = 0,
        current_dd: float = 0,
        current_expectancy: float = 0,
    ) -> list[dict]:
        """Check all active probations and rollback if needed."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        probations = conn.execute(
            """SELECT * FROM parameter_history
               WHERE status = 'PROBATION' AND probation_end_trade <= ?""",
            (self._trade_counter,),
        ).fetchall()
        conn.row_factory = None

        rollbacks = []
        for p in probations:
            p = dict(p)
            param_name = p["parameter_name"]
            old_value = p["old_value"]
            should_rollback = False
            reason = ""

            # Check rollback conditions
            wr_drop = pre_wr - current_wr if pre_wr > 0 else 0
            if wr_drop > config.ROLLBACK_WR_DROP_PCT:
                should_rollback = True
                reason = f"WR dropped {wr_drop:.1f}% (threshold {config.ROLLBACK_WR_DROP_PCT}%)"

            if current_dd > config.ROLLBACK_MAX_DD_PCT:
                should_rollback = True
                reason = f"Drawdown {current_dd:.1f}% (threshold {config.ROLLBACK_MAX_DD_PCT}%)"

            pre_exp = p.get("pre_change_expectancy") or 0
            if pre_exp > 0 and current_expectancy < pre_exp * (1 - config.ROLLBACK_THRESHOLD_PCT / 100):
                should_rollback = True
                reason = f"Expectancy dropped {((pre_exp - current_expectancy) / pre_exp * 100):.0f}%"

            if should_rollback:
                spec = ALL_PARAM_SPECS.get(param_name)
                if spec:
                    self.set_value(param_name, float(old_value), spec)
                conn.execute(
                    """UPDATE parameter_history SET status = 'ROLLED_BACK',
                       post_change_expectancy = ?, rollback_reason = ?
                       WHERE id = ?""",
                    (current_expectancy, reason, p["id"]),
                )

                # Lockout
                self._lockouts[param_name] = self._lockouts.get(param_name, 0) + config.LOCKOUT_CYCLES
                rollbacks.append({"param": param_name, "reverted_to": old_value, "reason": reason})
                logger.warning(f"ROLLBACK: {param_name} → {old_value} ({reason})")
            else:
                # Probation passed
                conn.execute(
                    """UPDATE parameter_history SET status = 'APPLIED',
                       post_change_expectancy = ? WHERE id = ?""",
                    (current_expectancy, p["id"]),
                )
                logger.info(f"PROBATION PASSED: {param_name} = {p['new_value']}")

        conn.commit()

        # Decrement lockout counters
        for param in list(self._lockouts):
            self._lockouts[param] -= 1
            if self._lockouts[param] <= 0:
                del self._lockouts[param]

        return rollbacks

    # ------------------------------------------------------------------
    # Record keeping
    # ------------------------------------------------------------------

    def _record_change(
        self, param_name: str, old_value: str, new_value: str,
        tier: int, trigger: str, evidence: str,
        sample_size: int, confidence: float, status: str,
        probation_start: int | None = None, probation_end: int | None = None,
        pre_expectancy: float | None = None,
    ) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO parameter_history (
                timestamp_utc, parameter_name, old_value, new_value, tier,
                trigger, supporting_evidence, sample_size, statistical_confidence,
                status, probation_start_trade, probation_end_trade, pre_change_expectancy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._now_iso(), param_name, old_value, new_value, tier,
             trigger, evidence, sample_size, confidence, status,
             probation_start, probation_end, pre_expectancy),
        )
        conn.commit()
        return cursor.lastrowid

    def get_history(self, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM parameter_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    def get_active_probation_count(self) -> int:
        return self._get_active_probations()

    def get_history_count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM parameter_history").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Autonomous tuning engine — analyzes performance and tunes params
    # ------------------------------------------------------------------

    def run_tuning_cycle(
        self,
        paper_trades: list[dict],
        real_trade_count: int,
        daily_pnl: float,
        win_rate: float,
        drawdown_pct: float,
        avg_net_rr: float = 0,
        blocked_reasons: dict | None = None,
    ) -> list[dict]:
        """
        Analyze trade performance and auto-tune parameters.
        Returns list of changes applied.
        """
        if not config.AUTONOMOUS_TUNING_ENABLED:
            return []

        changes = []
        sample = len(paper_trades) + real_trade_count

        if sample < config.TUNING_MIN_SAMPLE:
            logger.debug(f"Tuning: insufficient sample ({sample} < {config.TUNING_MIN_SAMPLE})")
            return []

        # --- Analyze paper trades for blocked-trade patterns ---
        blocked = [p for p in paper_trades if p.get("was_blocked")]
        blocked_wins = [p for p in blocked if p.get("pnl", 0) > 0]
        blocked_total = len(blocked)

        if blocked_total >= 5:
            blocked_wr = len(blocked_wins) / blocked_total * 100
            blocked_avg_pnl = sum(p.get("pnl", 0) for p in blocked) / blocked_total

            # If blocked trades are profitable → we're blocking too much
            if blocked_wr > 55 and blocked_avg_pnl > 0:
                # Analyze WHY they were blocked
                reason_counts: dict[str, int] = {}
                for p in blocked:
                    reason = p.get("block_reason", "")
                    if "Net R:R" in reason:
                        reason_counts["NET_RR"] = reason_counts.get("NET_RR", 0) + 1
                    elif "HTF" in reason:
                        reason_counts["HTF"] = reason_counts.get("HTF", 0) + 1
                    elif "Volume" in reason:
                        reason_counts["VOLUME"] = reason_counts.get("VOLUME", 0) + 1
                    elif "confidence" in reason.lower():
                        reason_counts["CONFIDENCE"] = reason_counts.get("CONFIDENCE", 0) + 1

                top_blocker = max(reason_counts, key=reason_counts.get) if reason_counts else None
                evidence = (
                    f"Blocked trades: {blocked_total} total, WR={blocked_wr:.0f}%, "
                    f"avg_PnL=${blocked_avg_pnl:.2f}. Top block: {top_blocker} "
                    f"({reason_counts.get(top_blocker, 0)}x). "
                    f"These would have been profitable — loosening filters."
                )

                if top_blocker == "NET_RR":
                    current = self.get_current_value("MIN_NET_RR")
                    new_val = max(0.3, current - 0.1)
                    ok, msg = self.apply_change(
                        "MIN_NET_RR", new_val,
                        trigger="BLOCKED_PROFITABLE_TRADES",
                        evidence=evidence,
                        sample_size=blocked_total,
                        confidence=blocked_wr,
                        drawdown_pct=drawdown_pct,
                    )
                    if ok:
                        changes.append({"param": "MIN_NET_RR", "old": current, "new": new_val, "reason": msg})

                elif top_blocker == "VOLUME":
                    current = self.get_current_value("MIN_VOLUME_RATIO")
                    new_val = max(0.05, current - 0.1)
                    ok, msg = self.apply_change(
                        "MIN_VOLUME_RATIO", new_val,
                        trigger="BLOCKED_PROFITABLE_TRADES",
                        evidence=evidence,
                        sample_size=blocked_total,
                        confidence=blocked_wr,
                        drawdown_pct=drawdown_pct,
                    )
                    if ok:
                        changes.append({"param": "MIN_VOLUME_RATIO", "old": current, "new": new_val, "reason": msg})

                elif top_blocker == "CONFIDENCE":
                    current = self.get_current_value("MIN_CONFIDENCE")
                    new_val = max(20, current - 5)
                    ok, msg = self.apply_change(
                        "MIN_CONFIDENCE", new_val,
                        trigger="BLOCKED_PROFITABLE_TRADES",
                        evidence=evidence,
                        sample_size=blocked_total,
                        confidence=blocked_wr,
                        drawdown_pct=drawdown_pct,
                    )
                    if ok:
                        changes.append({"param": "MIN_CONFIDENCE", "old": current, "new": new_val, "reason": msg})

        # --- Win rate too low → tighten quality filters ---
        if sample >= 10 and win_rate < 35:
            current_q = self.get_current_value("TRADE_QUALITY_MIN")
            new_val = min(60, current_q + 5)
            evidence = f"Low WR ({win_rate:.0f}%) over {sample} trades — tightening quality filter"
            ok, msg = self.apply_change(
                "TRADE_QUALITY_MIN", new_val,
                trigger="LOW_WIN_RATE",
                evidence=evidence,
                sample_size=sample,
                confidence=100 - win_rate,
                drawdown_pct=drawdown_pct,
            )
            if ok:
                changes.append({"param": "TRADE_QUALITY_MIN", "old": current_q, "new": new_val, "reason": msg})

        # --- Win rate high → loosen to catch more trades ---
        if sample >= 10 and win_rate > 60:
            current_q = self.get_current_value("TRADE_QUALITY_MIN")
            new_val = max(15, current_q - 5)
            evidence = f"High WR ({win_rate:.0f}%) over {sample} trades — loosening quality to catch more"
            ok, msg = self.apply_change(
                "TRADE_QUALITY_MIN", new_val,
                trigger="HIGH_WIN_RATE",
                evidence=evidence,
                sample_size=sample,
                confidence=win_rate,
                drawdown_pct=drawdown_pct,
            )
            if ok:
                changes.append({"param": "TRADE_QUALITY_MIN", "old": current_q, "new": new_val, "reason": msg})

        # --- Drawdown too high → tighten risk ---
        if drawdown_pct > 10:
            current_risk = self.get_current_value("BASE_RISK_PER_TRADE_PCT")
            new_val = max(5.0, current_risk - 2.0)
            evidence = f"High DD ({drawdown_pct:.1f}%) — reducing risk per trade"
            ok, msg = self.apply_change(
                "BASE_RISK_PER_TRADE_PCT", new_val,
                trigger="HIGH_DRAWDOWN",
                evidence=evidence,
                sample_size=sample,
                confidence=drawdown_pct * 5,
                drawdown_pct=drawdown_pct,
            )
            if ok:
                changes.append({"param": "BASE_RISK_PER_TRADE_PCT", "old": current_risk, "new": new_val, "reason": msg})

        # --- SL too tight (avg net_rr well below threshold despite good gross_rr) ---
        if avg_net_rr > 0 and avg_net_rr < 0.4:
            current_atr = self.get_current_value("ATR_STOP_MULT")
            new_val = min(1.5, current_atr + 0.1)
            evidence = f"Avg net R:R={avg_net_rr:.2f} too low — widening ATR stop multiplier"
            ok, msg = self.apply_change(
                "ATR_STOP_MULT", new_val,
                trigger="LOW_NET_RR",
                evidence=evidence,
                sample_size=sample,
                confidence=70,
                drawdown_pct=drawdown_pct,
            )
            if ok:
                changes.append({"param": "ATR_STOP_MULT", "old": current_atr, "new": new_val, "reason": msg})

        # --- Consecutive losses pattern → increase cooldown ---
        recent_completed = paper_trades[-10:] if len(paper_trades) >= 10 else paper_trades
        recent_losses = sum(1 for p in recent_completed if p.get("pnl", 0) <= 0)
        if len(recent_completed) >= 5 and recent_losses >= 4:
            current_cd = self.get_current_value("POST_LOSS_COOLDOWN_SEC")
            new_val = min(300, current_cd + 30)
            evidence = f"{recent_losses}/{len(recent_completed)} recent trades are losses — increasing cooldown"
            ok, msg = self.apply_change(
                "POST_LOSS_COOLDOWN_SEC", new_val,
                trigger="CONSECUTIVE_LOSSES",
                evidence=evidence,
                sample_size=len(recent_completed),
                confidence=recent_losses / len(recent_completed) * 100,
                drawdown_pct=drawdown_pct,
            )
            if ok:
                changes.append({"param": "POST_LOSS_COOLDOWN_SEC", "old": current_cd, "new": new_val, "reason": msg})

        for c in changes:
            logger.info(f"TUNING: {c['param']} {c['old']} → {c['new']} | {c['reason']}")

        return changes
