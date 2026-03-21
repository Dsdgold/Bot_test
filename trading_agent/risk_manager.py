"""Risk manager stub — full implementation in later chunks."""

from __future__ import annotations

import logging

from trading_agent import config

logger = logging.getLogger(__name__)


def check_risk_limits() -> tuple[bool, str]:
    """Check if we're within risk limits. Stub for now."""
    return True, "Risk limits OK (stub)"


def calculate_position_size(price: float) -> float:
    """Calculate position size in contracts. Stub for now."""
    return config.POSITION_SIZE_USD / price if price > 0 else 0.0
