"""Position sizing engine — adaptive sizing for target net profit.

Analyzes account equity and fee structure to calculate the optimal position size
that yields $1-2 net profit per winning trade. Automatically adjusts leverage
based on equity tier. Preserves all safety systems (drawdown halt, daily limit,
streak reduction).
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
    estimated_net_profit: float = 0.0  # Expected net $ on TP hit

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
    elif entry_quality >= config.TRADE_QUALITY_MIN:
        return config.QUALITY_SIZE_MULTIPLIER_B * 0.75  # Reduced size for lower quality
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
# Equity-aware leverage
# ---------------------------------------------------------------------------

def get_max_leverage_for_equity(equity: float) -> int:
    """Dynamic leverage cap based on account size.
    Micro accounts ($5-100) get higher leverage to hit profit targets.
    Larger accounts use less leverage for safety."""
    if equity < config.MICRO_EQUITY_THRESHOLD:
        return config.MICRO_MAX_LEVERAGE  # 20x for micro
    else:
        return config.STANDARD_MAX_LEVERAGE  # 10x for standard


# ---------------------------------------------------------------------------
# Adaptive position sizing (target net profit)
# ---------------------------------------------------------------------------

def calculate_min_position_for_profit(
    entry_price: float,
    tp_pct: float,
    target_profit_usd: float = 0.0,
    is_maker_entry: bool = True,
) -> tuple[float, float]:
    """Calculate minimum position size (USD) needed to net target_profit_usd after fees.

    Returns (min_size_usd, estimated_net_profit_per_unit).

    Math:
        net_profit = qty * (tp_distance - fee_cost_per_unit - slip_cost_per_unit)
        qty = target_profit / net_per_unit
        position_usd = qty * entry_price
    """
    if target_profit_usd <= 0:
        target_profit_usd = config.TARGET_NET_PROFIT_USD

    # Fee structure per unit
    entry_fee = config.MAKER_FEE_RATE if is_maker_entry else config.TAKER_FEE_RATE
    exit_fee = config.TAKER_FEE_RATE  # SL/TP always taker
    slip_rate = config.MAX_ALLOWED_SLIPPAGE_BPS / 10000
    slip_entry = 0.0 if is_maker_entry else slip_rate
    slip_exit = slip_rate

    total_cost_rate = entry_fee + exit_fee + slip_entry + slip_exit
    tp_rate = tp_pct / 100  # Convert from % to decimal

    # Net profit per dollar of position
    net_per_dollar = tp_rate - total_cost_rate

    if net_per_dollar <= 0:
        logger.warning(f"TP {tp_pct:.2f}% too small to cover fees {total_cost_rate*100:.4f}%")
        return 0.0, 0.0

    # Position size needed
    min_size_usd = target_profit_usd / net_per_dollar

    return min_size_usd, net_per_dollar


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
    tp_pct: float = 0.0,
) -> PositionSizeResult:
    """
    Calculate position size with adaptive mode.

    ADAPTIVE mode (default for micro accounts):
      Works backwards from TARGET_NET_PROFIT_USD to determine position size,
      then applies all safety adjustments (streak, drawdown, daily limit).

    FIXED_FRACTIONAL mode (legacy):
      position_size = (equity × risk_pct × adjustments) / (entry - SL)
    """
    # Capital protection checks (always enforced)
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
        return _halted_result(f"Entry quality {entry_quality} < {config.TRADE_QUALITY_MIN} — blocked")

    # Streak multiplier
    streak_mult = get_streak_multiplier(consecutive_losses)

    # Drawdown multiplier
    dd_mult, halted, dd_reason = get_drawdown_multiplier(drawdown_pct)
    if halted:
        return _halted_result(dd_reason)

    # Dynamic leverage cap based on equity
    max_lev = get_max_leverage_for_equity(equity)

    # ── ADAPTIVE MODE: size from target net profit ──
    is_maker = config.PREFER_POST_ONLY_ENTRIES
    estimated_net_profit = 0.0

    if config.POSITION_SIZING_MODE == "adaptive" and tp_pct > 0:
        target_profit = config.TARGET_NET_PROFIT_USD
        min_size, net_per_dollar = calculate_min_position_for_profit(
            entry_price, tp_pct, target_profit, is_maker
        )

        if min_size > 0:
            # Apply safety multipliers to target (reduce size in bad conditions)
            safety_mult = quality_mult * streak_mult * dd_mult
            # Scale target profit by safety (min $0.50 floor)
            adjusted_target = max(config.MIN_NET_PROFIT_USD, target_profit * safety_mult)
            adjusted_size = adjusted_target / net_per_dollar if net_per_dollar > 0 else 0

            # Enforce leverage cap
            max_size_usd = equity * max_lev
            size_usd = min(adjusted_size, max_size_usd)

            # Enforce max risk % of equity (don't risk more than MAX_RISK_PER_TRADE_PCT on SL)
            sl_distance_pct = abs(entry_price - sl_price) / entry_price * 100 if entry_price > 0 else 1
            risk_if_sl_hit = (size_usd * sl_distance_pct / 100)
            max_risk_usd = equity * (config.MAX_RISK_PER_TRADE_PCT / 100)
            if risk_if_sl_hit > max_risk_usd:
                size_usd = max_risk_usd / (sl_distance_pct / 100)
                size_usd = min(size_usd, max_size_usd)

            size_contracts = size_usd / entry_price if entry_price > 0 else 0
            leverage = size_usd / equity if equity > 0 else 0
            estimated_net_profit = size_usd * net_per_dollar if net_per_dollar > 0 else 0

            logger.info(
                f"ADAPTIVE SIZE: ${size_usd:.0f} ({leverage:.1f}x) | "
                f"target_net=${adjusted_target:.2f} | est_net=${estimated_net_profit:.2f} | "
                f"safety={safety_mult:.2f} | tier={tier}"
            )

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
                estimated_net_profit=estimated_net_profit,
            )

    # ── FALLBACK: fixed fractional mode ──
    effective_risk_pct = base_risk_pct * quality_mult * streak_mult * dd_mult
    effective_risk_pct = min(effective_risk_pct, config.MAX_RISK_PER_TRADE_PCT)
    risk_amount = equity * (effective_risk_pct / 100)

    risk_per_unit = abs(entry_price - sl_price)
    if risk_per_unit <= 0:
        return _halted_result("Invalid SL distance (zero or negative)")

    size_contracts = risk_amount / risk_per_unit
    size_usd = size_contracts * entry_price

    leverage = size_usd / equity if equity > 0 else 0
    if leverage > max_lev:
        size_usd = equity * max_lev
        size_contracts = size_usd / entry_price if entry_price > 0 else 0
        leverage = max_lev

    # Estimate net profit for logging
    if tp_pct > 0:
        _, net_per_dollar = calculate_min_position_for_profit(entry_price, tp_pct, 1.0, is_maker)
        estimated_net_profit = size_usd * net_per_dollar if net_per_dollar > 0 else 0

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
        estimated_net_profit=estimated_net_profit,
    )


def _halted_result(reason: str) -> PositionSizeResult:
    """Return a halted position sizing result."""
    return PositionSizeResult(
        size_usd=0, size_contracts=0, risk_pct=0,
        base_risk_pct=0, quality_mult=0, streak_mult=0,
        drawdown_mult=0, tier="HALTED", leverage=0,
        halted=True, halt_reason=reason,
    )
