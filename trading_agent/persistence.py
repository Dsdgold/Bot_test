"""
SQLite persistence for trades, positions, and bot state.
Survives restarts — no more lost history.
"""
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .models import Position, Side, Trade

logger = logging.getLogger("persistence")

DB_PATH = Path(__file__).parent.parent / "bot_data.db"


class BotDatabase:
    """SQLite database for bot persistence."""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        logger.info(f"Database initialized: {db_path}")

    def _create_tables(self):
        self.conn.executescript("""
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
        """)
        self.conn.commit()

    # ── Trades ──────────────────────────────────────────

    def save_trade(self, trade: Trade):
        """Save completed trade to history."""
        self.conn.execute(
            """INSERT INTO trades (symbol, side, entry_price, exit_price, quantity,
               leverage, pnl, pnl_pct, entry_time, exit_time, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.symbol,
                trade.side.value,
                trade.entry_price,
                trade.exit_price,
                trade.quantity,
                trade.leverage,
                trade.pnl,
                trade.pnl_pct,
                trade.entry_time.isoformat(),
                trade.exit_time.isoformat(),
                trade.reason,
            ),
        )
        self.conn.commit()
        logger.info(f"Trade saved to DB: {trade.side.value} PnL={trade.pnl:+.2f}")

    def load_trades(self, limit: int = 50) -> List[Trade]:
        """Load recent trades from DB."""
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

        trades = []
        for r in reversed(rows):  # Oldest first
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

        if trades:
            logger.info(f"Loaded {len(trades)} trades from DB")
        return trades

    # ── Position ──────────────────────────────────────────

    def save_position(self, position: Position):
        """Save current open position (upsert)."""
        self.conn.execute("DELETE FROM open_position")
        self.conn.execute(
            """INSERT INTO open_position (id, symbol, side, entry_price, quantity,
               leverage, stop_loss, take_profit, order_id, open_time, original_quantity)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position.symbol,
                position.side.value,
                position.entry_price,
                position.quantity,
                position.leverage,
                position.stop_loss,
                position.take_profit,
                position.order_id,
                position.open_time.isoformat(),
                position.original_quantity,
            ),
        )
        self.conn.commit()
        logger.info(f"Position saved to DB: {position.side.value} @ {position.entry_price}")

    def load_position(self) -> Optional[Position]:
        """Load open position from DB."""
        row = self.conn.execute("SELECT * FROM open_position WHERE id = 1").fetchone()
        if not row:
            return None

        pos = Position(
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
        logger.info(f"Position restored from DB: {pos.side.value} @ {pos.entry_price}")
        return pos

    def clear_position(self):
        """Remove saved position (after close)."""
        self.conn.execute("DELETE FROM open_position")
        self.conn.commit()

    # ── State ──────────────────────────────────────────

    def save_state(self, key: str, value: str):
        """Save a key-value state."""
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def load_state(self, key: str, default: str = "") -> str:
        """Load a key-value state."""
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def close(self):
        """Close database connection."""
        self.conn.close()
