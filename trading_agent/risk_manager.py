"""Risk manager — dynamic SL/TP, fee-aware R:R, kill switches, cooldowns."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from trading_agent import config
from trading_agent.indicators import atr
from trading_agent.models import Action, CandleData, DirectionalLicense, SetupType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SLTPLevels:
    """Calculated stop-loss and take-profit levels."""
    entry_price: float
    stop_loss: float
    take_profit: float
    sl_pct: float
    tp_pct: float
    net_rr: float
    entry_type: str  # "MAKER" or "TAKER"
    details: str = ""

    @property
    def is_valid(self) -> bool:
        return self.net_rr >= config.MIN_NET_RR


@dataclass
class KillSwitchState:
    """Tracks kill switch states."""
    spread_frozen: bool = False
    spread_value: float = 0.0
    funding_frozen: bool = False
    funding_rate: float = 0.0
    latency_frozen: bool = False
    latency_ms: float = 0.0

    @property
    def any_active(self) -> bool:
        return self.spread_frozen or self.funding_frozen or self.latency_frozen

    def reasons(self) -> list[str]:
        r = []
        if self.spread_frozen:
            r.append(f"Spread frozen: {self.spread_value:.2f} USDT > {config.MAX_SPREAD_TOLERANCE_USDT}")
        if self.funding_frozen:
            r.append(f"Extreme funding: {self.funding_rate:.6f} > {config.MAX_FUNDING_RATE_ABS}")
        if self.latency_frozen:
            r.append(f"High latency: {self.latency_ms:.0f}ms > {config.LATENCY_KILL_SWITCH_MS}ms")
        return r


@dataclass
class CooldownState:
    """Tracks cooldown timers and loss streaks."""
    last_trade_time: float = 0.0
    last_trade_candle_index: int = -100
    last_loss_time: float = 0.0
    consecutive_long_losses: int = 0
    consecutive_short_losses: int = 0
    long_frozen_until: float = 0.0
    short_frozen_until: float = 0.0

    def record_trade(self, direction: str, is_win: bool, candle_index: int = 0) -> None:
        """Record a trade result for cooldown tracking."""
        now = time.time()
        self.last_trade_time = now
        self.last_trade_candle_index = candle_index

        if not is_win:
            self.last_loss_time = now
            if direction == "LONG":
                self.consecutive_long_losses += 1
                if self.consecutive_long_losses >= config.SAME_SIDE_LOSS_PAUSE_COUNT:
                    self.long_frozen_until = now + config.SAME_SIDE_LOSS_PAUSE_SEC
                    logger.warning(
                        f"LONG side frozen for {config.SAME_SIDE_LOSS_PAUSE_SEC}s "
                        f"after {self.consecutive_long_losses} consecutive losses"
                    )
            else:
                self.consecutive_short_losses += 1
                if self.consecutive_short_losses >= config.SAME_SIDE_LOSS_PAUSE_COUNT:
                    self.short_frozen_until = now + config.SAME_SIDE_LOSS_PAUSE_SEC
                    logger.warning(
                        f"SHORT side frozen for {config.SAME_SIDE_LOSS_PAUSE_SEC}s "
                        f"after {self.consecutive_short_losses} consecutive losses"
                    )
        else:
            if direction == "LONG":
                self.consecutive_long_losses = 0
            else:
                self.consecutive_short_losses = 0


# ---------------------------------------------------------------------------
# Dynamic SL/TP calculation
# ---------------------------------------------------------------------------

def find_local_swing(candles: Sequence[CandleData], direction: str, lookback: int = 10) -> Optional[float]:
    """Find nearest swing low (for longs) or swing high (for shorts)."""
    if len(candles) < 3:
        return None

    recent = candles[-lookback:] if len(candles) >= lookback else candles

    if direction == "LONG":
        # Find swing low — lowest low in recent candles
        return min(c.low for c in recent)
    else:
        # Find swing high — highest high in recent candles
        return max(c.high for c in recent)


def calculate_dynamic_sl_tp(
    entry_price: float,
    license: DirectionalLicense,
    candles: Sequence[CandleData],
    is_maker: bool = True,
) -> SLTPLevels:
    """
    Calculate dynamic SL/TP with fee-aware net R:R validation.

    Stop: behind structural swing point or ATR fallback, outside liquidity pools.
    Target: scaled by setup quality (A+ wider, A moderate, B tighter).
    """
    direction = "LONG" if license.action == Action.LONG else "SHORT"
    fee_rate = config.MAKER_FEE_RATE if is_maker else config.TAKER_FEE_RATE
    slippage_rate = config.MAX_ALLOWED_SLIPPAGE_BPS / 10000
    entry_type = "MAKER" if is_maker else "TAKER"

    # ATR-based fallback
    atr_val = 0.0
    if candles and len(candles) >= 14:
        atr_vals = atr(candles)
        atr_val = atr_vals[-1]

    atr_stop = atr_val * config.ATR_STOP_MULT if atr_val > 0 else entry_price * 0.003

    # Structural swing point
    swing = find_local_swing(candles, direction) if candles else None

    # Calculate SL
    if direction == "LONG":
        structural_sl = (swing - atr_val * 0.2) if swing else None  # Pad behind swing
        atr_sl = entry_price - atr_stop
        sl = min(structural_sl, atr_sl) if structural_sl else atr_sl
    else:
        structural_sl = (swing + atr_val * 0.2) if swing else None
        atr_sl = entry_price + atr_stop
        sl = max(structural_sl, atr_sl) if structural_sl else atr_sl

    # Enforce SL guardrails
    sl_pct = abs(entry_price - sl) / entry_price * 100
    sl_pct = max(config.MIN_SL_PCT, min(sl_pct, config.MAX_SL_PCT))

    if direction == "LONG":
        sl = entry_price * (1 - sl_pct / 100)
    else:
        sl = entry_price * (1 + sl_pct / 100)

    # Target R:R based on setup quality
    if license.entry_quality >= 90:
        target_rr = config.TARGET_RR_A_PLUS
    elif license.entry_quality >= 75:
        target_rr = config.TARGET_RR_A
    else:
        target_rr = config.TARGET_RR_B

    risk_distance = abs(entry_price - sl)
    reward_distance = risk_distance * target_rr

    if direction == "LONG":
        tp = entry_price + reward_distance
    else:
        tp = entry_price - reward_distance

    # Enforce TP guardrails
    tp_pct = abs(tp - entry_price) / entry_price * 100
    tp_pct = max(config.MIN_TP_PCT, min(tp_pct, config.MAX_TP_PCT))

    if direction == "LONG":
        tp = entry_price * (1 + tp_pct / 100)
    else:
        tp = entry_price * (1 - tp_pct / 100)

    # Fee-aware net R:R calculation
    fee_cost = entry_price * fee_rate * 2  # Entry + exit
    slip_cost = entry_price * slippage_rate * 2

    if direction == "LONG":
        net_reward = tp - entry_price - fee_cost - slip_cost
        net_risk = entry_price - sl + fee_cost + slip_cost
    else:
        net_reward = entry_price - tp - fee_cost - slip_cost
        net_risk = sl - entry_price + fee_cost + slip_cost

    net_rr = net_reward / net_risk if net_risk > 0 else 0.0

    details = (
        f"{entry_type} entry | SL={sl:.1f} ({sl_pct:.2f}%) | "
        f"TP={tp:.1f} ({tp_pct:.2f}%) | gross_RR={target_rr:.2f} | "
        f"net_RR={net_rr:.2f} | fees={fee_cost:.2f} | slip={slip_cost:.2f}"
    )

    return SLTPLevels(
        entry_price=entry_price,
        stop_loss=sl,
        take_profit=tp,
        sl_pct=sl_pct,
        tp_pct=tp_pct,
        net_rr=net_rr,
        entry_type=entry_type,
        details=details,
    )


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------

def check_kill_switches(
    spread: float = 0.0,
    funding_rate: float = 0.0,
    latency_ms: float = 0.0,
    trade_direction: Optional[str] = None,
) -> KillSwitchState:
    """Check all kill switches and return state."""
    state = KillSwitchState()

    # Spread check
    if spread > config.MAX_SPREAD_TOLERANCE_USDT:
        state.spread_frozen = True
        state.spread_value = spread

    # Funding rate check (only for continuation in funding direction)
    if config.FREEZE_ON_EXTREME_FUNDING and abs(funding_rate) > config.MAX_FUNDING_RATE_ABS:
        # Positive funding = longs pay shorts (bearish pressure on longs)
        # Block if trading in the direction that pays funding
        if trade_direction == "LONG" and funding_rate > config.MAX_FUNDING_RATE_ABS:
            state.funding_frozen = True
            state.funding_rate = funding_rate
        elif trade_direction == "SHORT" and funding_rate < -config.MAX_FUNDING_RATE_ABS:
            state.funding_frozen = True
            state.funding_rate = funding_rate

    # Latency check
    if latency_ms > config.LATENCY_KILL_SWITCH_MS:
        state.latency_frozen = True
        state.latency_ms = latency_ms

    return state


# ---------------------------------------------------------------------------
# Cooldown checks
# ---------------------------------------------------------------------------

def check_cooldowns(
    cooldown: CooldownState,
    direction: str,
    current_candle_index: int = 0,
) -> tuple[bool, str]:
    """Check if cooldowns allow a new trade."""
    now = time.time()
    blocks = []

    # Post-loss cooldown
    if cooldown.last_loss_time > 0:
        elapsed = now - cooldown.last_loss_time
        if elapsed < config.POST_LOSS_COOLDOWN_SEC:
            remaining = config.POST_LOSS_COOLDOWN_SEC - elapsed
            blocks.append(f"Post-loss cooldown: {remaining:.0f}s remaining")

    # Re-entry cooldown (candle-based)
    candle_gap = current_candle_index - cooldown.last_trade_candle_index
    if candle_gap < config.REENTRY_COOLDOWN_CANDLES:
        blocks.append(
            f"Re-entry cooldown: {candle_gap}/{config.REENTRY_COOLDOWN_CANDLES} candles"
        )

    # Same-side freeze
    if direction == "LONG" and now < cooldown.long_frozen_until:
        remaining = cooldown.long_frozen_until - now
        blocks.append(
            f"LONG side frozen: {remaining:.0f}s remaining "
            f"({cooldown.consecutive_long_losses} consecutive losses)"
        )

    if direction == "SHORT" and now < cooldown.short_frozen_until:
        remaining = cooldown.short_frozen_until - now
        blocks.append(
            f"SHORT side frozen: {remaining:.0f}s remaining "
            f"({cooldown.consecutive_short_losses} consecutive losses)"
        )

    if blocks:
        return False, "; ".join(blocks)
    return True, "Cooldowns clear"


# ---------------------------------------------------------------------------
# Position sizing (preserved from stub)
# ---------------------------------------------------------------------------

def calculate_position_size(price: float) -> float:
    """Calculate position size in contracts."""
    return config.POSITION_SIZE_USD / price if price > 0 else 0.0
