"""Manual trading controls — owner override with data isolation.

Manual trades are tagged MANUAL_OWNER, bypass all bot gates,
and are excluded from self-learning/optimization analytics.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from trading_agent import config

logger = logging.getLogger(__name__)


@dataclass
class ManualPosition:
    """State of a manually-opened position."""
    trade_id: str
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    size_usd: float
    opened_at: float  # time.time()
    unrealized_pnl: float = 0.0

    def update_pnl(self, current_price: float) -> float:
        if self.direction == "LONG":
            self.unrealized_pnl = (current_price - self.entry_price) / self.entry_price * self.size_usd
        else:
            self.unrealized_pnl = (self.entry_price - current_price) / self.entry_price * self.size_usd
        return self.unrealized_pnl


class ManualControlsManager:
    """Manages manual trading actions with proper tagging and isolation."""

    def __init__(self):
        self.position: Optional[ManualPosition] = None
        self._last_action_time: float = 0
        self.is_manual_active: bool = False

    @property
    def has_position(self) -> bool:
        return self.position is not None

    @property
    def position_status(self) -> str:
        if not self.position:
            return "FLAT"
        p = self.position
        pnl_str = f"+${p.unrealized_pnl:.2f}" if p.unrealized_pnl >= 0 else f"-${abs(p.unrealized_pnl):.2f}"
        return f"{p.direction} ${p.entry_price:.0f} ({pnl_str} unrealized)"

    def open_long(self, entry_price: float, size_usd: float | None = None) -> dict:
        """Open a manual LONG position."""
        return self._open("LONG", entry_price, size_usd)

    def open_short(self, entry_price: float, size_usd: float | None = None) -> dict:
        """Open a manual SHORT position."""
        return self._open("SHORT", entry_price, size_usd)

    def _open(self, direction: str, entry_price: float, size_usd: float | None) -> dict:
        if self.has_position:
            return {"success": False, "error": "Position already open", "trade_id": None}

        trade_id = f"manual_{uuid.uuid4().hex[:12]}"
        size = size_usd or config.POSITION_SIZE_USD

        self.position = ManualPosition(
            trade_id=trade_id,
            direction=direction,
            entry_price=entry_price,
            size_usd=size,
            opened_at=time.time(),
        )
        self.is_manual_active = True
        self._last_action_time = time.time()

        logger.info(f"MANUAL {direction}: {trade_id} @ ${entry_price:.1f}, size=${size:.0f}")

        return {
            "success": True,
            "trade_id": trade_id,
            "direction": direction,
            "entry_price": entry_price,
            "size_usd": size,
            "trade_source": "MANUAL_OWNER",
        }

    def close_all(self, exit_price: float) -> dict:
        """Close all positions immediately."""
        if not self.has_position:
            return {"success": False, "error": "No position to close", "trade_id": None}

        p = self.position
        p.update_pnl(exit_price)

        if p.direction == "LONG":
            gross_pnl = (exit_price - p.entry_price) / p.entry_price * p.size_usd
        else:
            gross_pnl = (p.entry_price - exit_price) / p.entry_price * p.size_usd

        fees = p.size_usd * config.TAKER_FEE_RATE * 2
        net_pnl = gross_pnl - fees
        hold_sec = int(time.time() - p.opened_at)

        result = {
            "success": True,
            "trade_id": p.trade_id,
            "direction": p.direction,
            "entry_price": p.entry_price,
            "exit_price": exit_price,
            "gross_pnl": gross_pnl,
            "fees": fees,
            "net_pnl": net_pnl,
            "hold_sec": hold_sec,
            "exit_type": "MANUAL_OWNER",
            "trade_source": "MANUAL_OWNER",
        }

        logger.info(
            f"MANUAL CLOSE: {p.trade_id} @ ${exit_price:.1f}, "
            f"PnL=${net_pnl:+.2f} ({hold_sec}s)"
        )

        self.position = None
        self.is_manual_active = False
        self._last_action_time = time.time()

        return result

    def should_exclude_from_learning(self, trade_source: str) -> bool:
        """Check if a trade should be excluded from bot learning."""
        return trade_source == "MANUAL_OWNER"

    def bot_can_resume(self) -> bool:
        """Bot resumes autonomous operation after manual close. No cooldown penalty."""
        return not self.has_position
