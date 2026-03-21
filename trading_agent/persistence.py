"""Database persistence — schema migration and trade record storage."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from trading_agent import config
from trading_agent.models import TradeRecord

logger = logging.getLogger(__name__)


def get_db_path() -> Path:
    return Path(config.DB_PATH).resolve() if Path(config.DB_PATH).is_absolute() else \
        Path(__file__).resolve().parent.parent / config.DB_PATH


def get_connection() -> sqlite3.Connection:
    """Get a database connection, running migrations as needed."""
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    _run_migrations(conn)
    return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist. Safe to run repeatedly."""
    # Legacy trades table (from chunk 1-2)
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

    # Chunk 4 tables — imported from data_collector schema
    from trading_agent.data_collector import ALL_SCHEMAS
    for schema in ALL_SCHEMAS:
        conn.execute(schema)

    conn.commit()
    logger.debug("Database migration complete")


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
