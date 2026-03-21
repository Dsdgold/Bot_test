"""Database persistence for trade records (stub for chunk 1-2)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from trading_agent.models import TradeRecord

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "bot_data.db"


def get_connection() -> sqlite3.Connection:
    """Get a database connection, creating tables if needed."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            entry_time TEXT,
            exit_time TEXT,
            pnl REAL,
            pnl_pct REAL,
            fees REAL,
            confidence INTEGER,
            entry_quality INTEGER,
            regime TEXT,
            setup_type TEXT,
            htf_alignment TEXT,
            source TEXT,
            reason TEXT
        )
    """)
    conn.commit()
    return conn


def save_trade(trade: TradeRecord) -> int:
    """Save a trade record and return its ID."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO trades
            (symbol, direction, entry_price, exit_price, entry_time, exit_time,
             pnl, pnl_pct, fees, confidence, entry_quality, regime, setup_type,
             htf_alignment, source, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.symbol, trade.direction, trade.entry_price, trade.exit_price,
                trade.entry_time.isoformat() if trade.entry_time else None,
                trade.exit_time.isoformat() if trade.exit_time else None,
                trade.pnl, trade.pnl_pct, trade.fees, trade.confidence,
                trade.entry_quality, trade.regime, trade.setup_type,
                trade.htf_alignment, trade.source, trade.reason,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()
