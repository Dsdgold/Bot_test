"""Position sizing engine — fixed fractional with quality, streak, drawdown adjustments.

Automatically compounds profits, reduces risk in drawdowns, and enforces
hard capital protection limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from trading_agent import config

logger = logging.getLogger(__name__)


@dataclass
class PositionSizeResult:
    """Result of position sizing calculation."""
    size_usd: float
    size_contracts: float
    risk_pct: float
    base_risk_pct: float
    quality_mult: float
    streak_mult: float
    drawdown_mult: float
    tier: str
    leverage: float
    halted: bool = False
    halt_reason: str = ""

    @property
    def effective_risk_pct(self) -> float:
        return self.risk_pct * self.quality_mult * self.streak_mult * self.drawdown_mult


# ---------------------------------------------------------------------------
# Growth tier
# ---------------------------------------------------------------------------

def get_growth_tier(equity: float) -> tuple[str, float]:
    """Determine growth tier and base risk % from equity level."""
    if equity >= config.TIER_4_EQUITY:
        return "TIER_4", config.TIER_4_RISK_PCT
    elif equity >= config.TIER_3_EQUITY:
        return "TIER_3", config.TIER_3_RISK_PCT
    elif equity >= config.TIER_2_EQUITY:
        return "TIER_2", config.TIER_2_RISK_PCT
    elif equity >= config.TIER_1_EQUITY:
        return "TIER_1", config.TIER_1_RISK_PCT
    else:
        return "STARTER", config.BASE_RISK_PER_TRADE_PCT


# ---------------------------------------------------------------------------
# Quality multiplier
# ---------------------------------------------------------------------------

def get_quality_multiplier(entry_quality: int) -> float:
    """Scale position size by setup quality."""
    if entry_quality >= 90:
        return config.QUALITY_SIZE_MULTIPLIER_A_PLUS
    elif entry_quality >= 80:
        return config.QUALITY_SIZE_MULTIPLIER_A
    elif entry_quality >= 70:
        return config.QUALITY_SIZE_MULTIPLIER_B
    else:
        return 0.0  # Blocked


# ---------------------------------------------------------------------------
# Streak adjustment
# ---------------------------------------------------------------------------

def get_streak_multiplier(consecutive_losses: int) -> float:
    """Reduce size after consecutive losses."""
    if consecutive_losses >= 3:
        return config.STREAK_LOSS_REDUCTION_3
    elif consecutive_losses >= 2:
        return config.STREAK_LOSS_REDUCTION_2
    elif consecutive_losses >= 1:
        return config.STREAK_LOSS_REDUCTION_1
    return 1.0


# ---------------------------------------------------------------------------
# Drawdown adjustment
# ---------------------------------------------------------------------------

def get_drawdown_multiplier(drawdown_pct: float) -> tuple[float, bool, str]:
    """
    Reduce size in drawdowns. Returns (multiplier, halted, reason).
    >DD_HALT_PCT → HALT ALL TRADING.
    """
    if drawdown_pct >= config.DD_HALT_PCT:
        return 0.0, True, f"Drawdown {drawdown_pct:.1f}% >= {config.DD_HALT_PCT}% — HALT"
    elif drawdown_pct >= config.DD_TIER_3_PCT:
        return config.DD_TIER_3_MULT, False, f"DD tier 3: {drawdown_pct:.1f}%"
    elif drawdown_pct >= config.DD_TIER_2_PCT:
        return config.DD_TIER_2_MULT, False, f"DD tier 2: {drawdown_pct:.1f}%"
    elif drawdown_pct >= config.DD_TIER_1_PCT:
        return config.DD_TIER_1_MULT, False, f"DD tier 1: {drawdown_pct:.1f}%"
    return 1.0, False, ""


# ---------------------------------------------------------------------------
# Capital protection checks
# ---------------------------------------------------------------------------

def check_daily_loss_limit(daily_pnl: float, equity: float) -> tuple[bool, str]:
    """Check if daily loss limit has been hit."""
    if equity <= 0:
        return False, "No equity"
    loss_pct = abs(daily_pnl) / equity * 100 if daily_pnl < 0 else 0
    if loss_pct >= config.DAILY_MAX_LOSS_PCT:
        return False, f"Daily loss {loss_pct:.1f}% >= {config.DAILY_MAX_LOSS_PCT}% — HALT until next UTC day"
    return True, ""


def check_weekly_loss_limit(weekly_pnl: float, equity: float) -> tuple[bool, str]:
    """Check if weekly loss limit has been hit."""
    if equity <= 0:
        return False, "No equity"
    loss_pct = abs(weekly_pnl) / equity * 100 if weekly_pnl < 0 else 0
    if loss_pct >= config.WEEKLY_MAX_LOSS_PCT:
        return False, f"Weekly loss {loss_pct:.1f}% >= {config.WEEKLY_MAX_LOSS_PCT}% — HALT until Monday"
    return True, ""


def check_equity_floor(equity: float) -> tuple[bool, str]:
    """Check if equity has dropped below floor."""
    if equity <= config.EQUITY_FLOOR_USDT:
        return False, f"Equity ${equity:.2f} <= floor ${config.EQUITY_FLOOR_USDT} — HALT permanently"
    return True, ""


# ---------------------------------------------------------------------------
# Main position sizing
# ---------------------------------------------------------------------------

def calculate_position_size(
    equity: float,
    entry_price: float,
    sl_price: float,
    entry_quality: int,
    consecutive_losses: int = 0,
    drawdown_pct: float = 0.0,
    daily_pnl: float = 0.0,
    weekly_pnl: float = 0.0,
    current_open_positions: int = 0,
) -> PositionSizeResult:
    """
    Calculate position size using fixed fractional risk with all adjustments.

    position_size = (equity × risk_pct × adjustments) / (entry - SL)
    """
    # Capital protection checks
    floor_ok, floor_reason = check_equity_floor(equity)
    if not floor_ok:
        return _halted_result(floor_reason)

    daily_ok, daily_reason = check_daily_loss_limit(daily_pnl, equity)
    if not daily_ok:
        return _halted_result(daily_reason)

    weekly_ok, weekly_reason = check_weekly_loss_limit(weekly_pnl, equity)
    if not weekly_ok:
        return _halted_result(weekly_reason)

    if current_open_positions >= config.MAX_OPEN_POSITIONS:
        return _halted_result(f"Max open positions ({config.MAX_OPEN_POSITIONS}) reached")

    # Growth tier
    tier, base_risk_pct = get_growth_tier(equity)

    # Quality multiplier
    quality_mult = get_quality_multiplier(entry_quality)
    if quality_mult == 0:
        return _halted_result(f"Entry quality {entry_quality} < 70 — blocked")

    # Streak multiplier
    streak_mult = get_streak_multiplier(consecutive_losses)

    # Drawdown multiplier
    dd_mult, halted, dd_reason = get_drawdown_multiplier(drawdown_pct)
    if halted:
        return _halted_result(dd_reason)

    # Calculate risk amount
    effective_risk_pct = base_risk_pct * quality_mult * streak_mult * dd_mult
    effective_risk_pct = min(effective_risk_pct, config.MAX_RISK_PER_TRADE_PCT)
    risk_amount = equity * (effective_risk_pct / 100)

    # Calculate position size from risk
    risk_per_unit = abs(entry_price - sl_price)
    if risk_per_unit <= 0:
        return _halted_result("Invalid SL distance (zero or negative)")

    size_contracts = risk_amount / risk_per_unit
    size_usd = size_contracts * entry_price

    # Leverage check
    leverage = size_usd / equity if equity > 0 else 0
    if leverage > config.MAX_LEVERAGE:
        # Cap at max leverage
        size_usd = equity * config.MAX_LEVERAGE
        size_contracts = size_usd / entry_price if entry_price > 0 else 0
        leverage = config.MAX_LEVERAGE

    return PositionSizeResult(
        size_usd=size_usd,
        size_contracts=size_contracts,
        risk_pct=base_risk_pct,
        base_risk_pct=base_risk_pct,
        quality_mult=quality_mult,
        streak_mult=streak_mult,
        drawdown_mult=dd_mult,
        tier=tier,
        leverage=leverage,
    )


def _halted_result(reason: str) -> PositionSizeResult:
    """Return a halted position sizing result."""
    return PositionSizeResult(
        size_usd=0, size_contracts=0, risk_pct=0,
        base_risk_pct=0, quality_mult=0, streak_mult=0,
        drawdown_mult=0, tier="HALTED", leverage=0,
        halted=True, halt_reason=reason,
    )
