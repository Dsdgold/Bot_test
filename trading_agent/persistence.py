"""
SQLite persistence — Selective Execution Mode.
Includes all Phase 7 data collection tables.
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import Position, Side, Trade

logger = logging.getLogger("persistence")

DB_PATH = Path(__file__).parent.parent / "bot_data.db"


class BotDatabase:
    """SQLite database for bot persistence and data collection."""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        logger.info(f"Database initialized: {db_path}")

    def _create_tables(self):
        self.conn.executescript("""
            -- Legacy trades table (kept for backward compatibility)
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                leverage INTEGER NOT NULL,
                pnl REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                reason TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS open_position (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                leverage INTEGER NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                order_id TEXT,
                open_time TEXT NOT NULL,
                original_quantity REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Phase 7: Trade decisions (the black box)
            CREATE TABLE IF NOT EXISTS trade_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                decision TEXT NOT NULL,
                trade_id TEXT,
                price REAL, bid REAL, ask REAL, spread_usdt REAL,
                atr_1m REAL, atr_5m REAL, atr_15m REAL, volatility_pct REAL,
                volume_current REAL, volume_avg_20 REAL, volume_ratio REAL,
                ema9 REAL, ema21 REAL, ema50 REAL, rsi_14 REAL,
                adx REAL, chop_index REAL, bb_width REAL, macd_hist REAL,
                ema_slope_1m REAL, ema_slope_15m REAL, ema_slope_1h REAL,
                cvd_value REAL, cvd_aligned INTEGER, oi_current REAL,
                oi_change_pct REAL, oi_confirmed INTEGER, funding_rate REAL,
                orderbook_imbalance REAL,
                regime TEXT, htf_alignment TEXT, setup_type TEXT,
                entry_quality INTEGER, confidence INTEGER, extension_atr REAL,
                trade_source TEXT, entry_type TEXT, entry_price REAL,
                sl_price REAL, tp_price REAL, planned_rr REAL, net_rr REAL,
                position_size_usd REAL, leverage REAL,
                skip_reason TEXT, entry_reason TEXT,
                exit_price REAL, exit_type TEXT, gross_pnl_usd REAL,
                fees_paid_usd REAL, net_pnl_usd REAL, net_pnl_pct REAL,
                slippage_bps REAL, hold_duration_sec INTEGER,
                max_favorable_excursion REAL, max_adverse_excursion REAL,
                session_hour_local INTEGER, day_of_week INTEGER,
                equity_before REAL, equity_after REAL, drawdown_pct REAL,
                api_latency_ms INTEGER, ai_response_time_ms INTEGER
            );

            -- Phase 7: Market snapshots
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                price REAL, atr_1m REAL, volume_ratio REAL,
                regime TEXT, adx REAL, chop_index REAL,
                cvd_value REAL, oi_current REAL, funding_rate REAL,
                spread_usdt REAL, bb_width REAL
            );

            -- Phase 7: Equity curve
            CREATE TABLE IF NOT EXISTS equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                equity_usdt REAL, unrealized_pnl REAL, total_equity REAL,
                peak_equity REAL, drawdown_pct REAL, drawdown_duration_min INTEGER,
                total_trades INTEGER, total_wins INTEGER, win_rate REAL,
                total_net_pnl REAL, position_size_tier TEXT
            );

            -- Phase 7: Daily sessions
            CREATE TABLE IF NOT EXISTS daily_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                trades_taken INTEGER DEFAULT 0, trades_skipped INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
                win_rate REAL DEFAULT 0, gross_pnl REAL DEFAULT 0,
                total_fees REAL DEFAULT 0, net_pnl REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                best_trade_pnl REAL DEFAULT 0, worst_trade_pnl REAL DEFAULT 0,
                avg_hold_time_sec REAL DEFAULT 0, dominant_regime TEXT,
                equity_start REAL DEFAULT 0, equity_end REAL DEFAULT 0,
                equity_growth_pct REAL DEFAULT 0, skip_reasons_json TEXT
            );

            -- Phase 7: Learning journal
            CREATE TABLE IF NOT EXISTS learning_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                trade_id TEXT,
                trigger TEXT, observation TEXT, conclusion TEXT,
                confidence_in_conclusion INTEGER DEFAULT 0,
                supporting_sample_size INTEGER DEFAULT 0,
                suggested_action TEXT,
                applied INTEGER DEFAULT 0, applied_timestamp TEXT,
                outcome_after_applied TEXT
            );

            -- Phase 10: Token usage
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                call_type TEXT, input_tokens INTEGER, output_tokens INTEGER,
                total_tokens INTEGER, cached INTEGER DEFAULT 0,
                model TEXT, cost_usd REAL DEFAULT 0
            );

            -- Phase 12: Parameter history
            CREATE TABLE IF NOT EXISTS parameter_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                parameter_name TEXT NOT NULL,
                old_value TEXT, new_value TEXT,
                tier INTEGER DEFAULT 1,
                trigger TEXT, supporting_evidence TEXT,
                sample_size INTEGER DEFAULT 0, statistical_confidence REAL DEFAULT 0,
                status TEXT DEFAULT 'APPLIED',
                probation_start_trade INTEGER, probation_end_trade INTEGER,
                pre_change_expectancy REAL, post_change_expectancy REAL,
                rollback_reason TEXT
            );
        """)
        self.conn.commit()

    # ── Legacy Trade Methods ─────────────────────────────

    def save_trade(self, trade: Trade):
        """Save completed trade to history."""
        self.conn.execute(
            """INSERT INTO trades (symbol, side, entry_price, exit_price, quantity,
               leverage, pnl, pnl_pct, entry_time, exit_time, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.symbol, trade.side.value, trade.entry_price,
                trade.exit_price, trade.quantity, trade.leverage,
                trade.pnl, trade.pnl_pct,
                trade.entry_time.isoformat(), trade.exit_time.isoformat(),
                trade.reason,
            ),
        )
        self.conn.commit()

    def load_trades(self, limit: int = 50) -> List[Trade]:
        """Load recent trades from DB."""
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        trades = []
        for r in reversed(rows):
            trades.append(Trade(
                symbol=r["symbol"],
                side=Side.LONG if r["side"] == "LONG" else Side.SHORT,
                entry_price=r["entry_price"],
                exit_price=r["exit_price"],
                quantity=r["quantity"],
                leverage=r["leverage"],
                pnl=r["pnl"],
                pnl_pct=r["pnl_pct"],
                entry_time=datetime.fromisoformat(r["entry_time"]),
                exit_time=datetime.fromisoformat(r["exit_time"]),
                reason=r["reason"] or "",
            ))
        return trades

    # ── Position ─────────────────────────────────────────

    def save_position(self, position: Position):
        self.conn.execute("DELETE FROM open_position")
        self.conn.execute(
            """INSERT INTO open_position (id, symbol, side, entry_price, quantity,
               leverage, stop_loss, take_profit, order_id, open_time, original_quantity)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position.symbol, position.side.value, position.entry_price,
                position.quantity, position.leverage, position.stop_loss,
                position.take_profit, position.order_id,
                position.open_time.isoformat(), position.original_quantity,
            ),
        )
        self.conn.commit()

    def load_position(self) -> Optional[Position]:
        row = self.conn.execute("SELECT * FROM open_position WHERE id = 1").fetchone()
        if not row:
            return None
        return Position(
            symbol=row["symbol"],
            side=Side.LONG if row["side"] == "LONG" else Side.SHORT,
            entry_price=row["entry_price"],
            quantity=row["quantity"],
            leverage=row["leverage"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            order_id=row["order_id"] or "",
            open_time=datetime.fromisoformat(row["open_time"]),
            original_quantity=row["original_quantity"],
        )

    def clear_position(self):
        self.conn.execute("DELETE FROM open_position")
        self.conn.commit()

    # ── State ────────────────────────────────────────────

    def save_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def load_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    # ── Trade Decisions (Phase 7) ────────────────────────

    def save_trade_decision(self, data: dict):
        """Save a trade decision snapshot."""
        cols = [
            "timestamp_utc", "decision", "trade_id", "price", "bid", "ask",
            "spread_usdt", "atr_1m", "volatility_pct", "volume_current",
            "volume_avg_20", "volume_ratio", "ema9", "ema21", "ema50", "rsi_14",
            "adx", "chop_index", "bb_width", "macd_hist", "ema_slope_1m",
            "cvd_value", "cvd_aligned", "oi_current", "oi_change_pct",
            "oi_confirmed", "funding_rate", "orderbook_imbalance",
            "regime", "htf_alignment", "setup_type", "entry_quality",
            "confidence", "extension_atr", "trade_source", "entry_type",
            "entry_price", "sl_price", "tp_price", "planned_rr", "net_rr",
            "position_size_usd", "leverage", "skip_reason", "entry_reason",
            "exit_price", "exit_type", "gross_pnl_usd", "fees_paid_usd",
            "net_pnl_usd", "net_pnl_pct", "slippage_bps", "hold_duration_sec",
            "max_favorable_excursion", "max_adverse_excursion",
            "session_hour_local", "day_of_week", "equity_before", "equity_after",
            "drawdown_pct", "api_latency_ms",
        ]
        present = [c for c in cols if c in data]
        placeholders = ",".join(["?"] * len(present))
        col_str = ",".join(present)
        values = [data[c] for c in present]

        self.conn.execute(
            f"INSERT INTO trade_decisions ({col_str}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()

    def update_trade_exit(self, trade_id: str, exit_data: dict):
        """Update a trade decision with exit data."""
        sets = []
        values = []
        for key, val in exit_data.items():
            sets.append(f"{key} = ?")
            values.append(val)
        values.append(trade_id)

        if sets:
            self.conn.execute(
                f"UPDATE trade_decisions SET {', '.join(sets)} WHERE trade_id = ? AND decision LIKE 'ENTRY_%'",
                values,
            )
            self.conn.commit()

    # ── Market Snapshots ─────────────────────────────────

    def save_market_snapshot(self, data: dict):
        self.conn.execute(
            """INSERT INTO market_snapshots
            (timestamp_utc, price, atr_1m, volume_ratio, regime, adx,
             chop_index, cvd_value, oi_current, funding_rate, spread_usdt, bb_width)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("timestamp_utc", datetime.utcnow().isoformat()),
                data.get("price", 0), data.get("atr_1m", 0),
                data.get("volume_ratio", 0), data.get("regime", "UNKNOWN"),
                data.get("adx", 0), data.get("chop_index", 0),
                data.get("cvd_value", 0), data.get("oi_current", 0),
                data.get("funding_rate", 0), data.get("spread_usdt", 0),
                data.get("bb_width", 0),
            ),
        )
        self.conn.commit()

    # ── Equity Curve ─────────────────────────────────────

    def save_equity_snapshot(self, data: dict):
        self.conn.execute(
            """INSERT INTO equity_curve
            (timestamp_utc, equity_usdt, unrealized_pnl, total_equity,
             peak_equity, drawdown_pct, total_trades, total_wins,
             win_rate, total_net_pnl, position_size_tier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("timestamp_utc", datetime.utcnow().isoformat()),
                data.get("equity_usdt", 0), data.get("unrealized_pnl", 0),
                data.get("total_equity", 0), data.get("peak_equity", 0),
                data.get("drawdown_pct", 0), data.get("total_trades", 0),
                data.get("total_wins", 0), data.get("win_rate", 0),
                data.get("total_net_pnl", 0), data.get("position_size_tier", ""),
            ),
        )
        self.conn.commit()

    # ── Daily Sessions ───────────────────────────────────

    def save_daily_session(self, data: dict):
        self.conn.execute(
            """INSERT OR REPLACE INTO daily_sessions
            (date, trades_taken, trades_skipped, wins, losses, win_rate,
             gross_pnl, total_fees, net_pnl, max_drawdown_pct,
             best_trade_pnl, worst_trade_pnl, avg_hold_time_sec,
             dominant_regime, equity_start, equity_end, equity_growth_pct,
             skip_reasons_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("date"), data.get("trades_taken", 0),
                data.get("trades_skipped", 0), data.get("wins", 0),
                data.get("losses", 0), data.get("win_rate", 0),
                data.get("gross_pnl", 0), data.get("total_fees", 0),
                data.get("net_pnl", 0), data.get("max_drawdown_pct", 0),
                data.get("best_trade_pnl", 0), data.get("worst_trade_pnl", 0),
                data.get("avg_hold_time_sec", 0), data.get("dominant_regime", ""),
                data.get("equity_start", 0), data.get("equity_end", 0),
                data.get("equity_growth_pct", 0),
                json.dumps(data.get("skip_reasons", {})),
            ),
        )
        self.conn.commit()

    # ── Learning Journal ─────────────────────────────────

    def save_journal_entry(self, data: dict):
        self.conn.execute(
            """INSERT INTO learning_journal
            (timestamp_utc, entry_type, trade_id, trigger, observation,
             conclusion, confidence_in_conclusion, supporting_sample_size,
             suggested_action, applied)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                data.get("timestamp_utc", datetime.utcnow().isoformat()),
                data.get("entry_type", ""),
                data.get("trade_id"),
                data.get("trigger", ""),
                data.get("observation", ""),
                data.get("conclusion", ""),
                data.get("confidence_in_conclusion", 0),
                data.get("supporting_sample_size", 0),
                data.get("suggested_action", ""),
            ),
        )
        self.conn.commit()

    def get_journal_entries(self, limit: int = 20) -> list:
        rows = self.conn.execute(
            "SELECT * FROM learning_journal ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Token Usage ──────────────────────────────────────

    def save_token_usage(self, data: dict):
        self.conn.execute(
            """INSERT INTO token_usage
            (timestamp_utc, call_type, input_tokens, output_tokens,
             total_tokens, cached, model, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("timestamp_utc", datetime.utcnow().isoformat()),
                data.get("call_type", ""),
                data.get("input_tokens", 0), data.get("output_tokens", 0),
                data.get("total_tokens", 0), data.get("cached", 0),
                data.get("model", ""), data.get("cost_usd", 0),
            ),
        )
        self.conn.commit()

    # ── Parameter History ────────────────────────────────

    def save_parameter_change(self, data: dict):
        self.conn.execute(
            """INSERT INTO parameter_history
            (timestamp_utc, parameter_name, old_value, new_value, tier,
             trigger, supporting_evidence, sample_size, statistical_confidence,
             status, pre_change_expectancy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("timestamp_utc", datetime.utcnow().isoformat()),
                data.get("parameter_name", ""),
                str(data.get("old_value", "")), str(data.get("new_value", "")),
                data.get("tier", 1), data.get("trigger", ""),
                data.get("supporting_evidence", ""),
                data.get("sample_size", 0),
                data.get("statistical_confidence", 0),
                data.get("status", "APPLIED"),
                data.get("pre_change_expectancy", 0),
            ),
        )
        self.conn.commit()

    def get_parameter_history(self, limit: int = 50) -> list:
        rows = self.conn.execute(
            "SELECT * FROM parameter_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Utility ──────────────────────────────────────────

    @staticmethod
    def generate_trade_id() -> str:
        return str(uuid.uuid4())[:12]

    def close(self):
        self.conn.close()
