"""
Risk management module — Selective Execution Mode.
Dynamic SL/TP, fee-aware R:R, position sizing, kill switches, cooldowns.
"""
import logging
import math
from datetime import datetime, date, timedelta
from typing import Optional

from .config import TradingConfig
from .models import (
    AccountState, EntryQuality, Indicators, MarketContext,
    Position, Side, Trade
)

logger = logging.getLogger("risk_manager")

TAKER_FEE_PCT = 0.055  # 0.055% per side


class RiskManager:
    """Manages trading risk, position sizing, and capital protection."""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.current_date: date = date.today()
        self.last_trade_time: Optional[datetime] = None
        self.consecutive_losses: int = 0
        self.loss_streak_pause_until: Optional[datetime] = None

        # Extended tracking
        self.weekly_pnl: float = 0.0
        self.week_start: date = date.today()
        self.peak_equity: float = 0.0
        self.same_side_losses: dict = {"LONG": 0, "SHORT": 0}
        self.same_side_pause_until: dict = {"LONG": None, "SHORT": None}
        self.last_entry_direction: Optional[Side] = None
        self.candles_since_last_trade: int = 0
        self.api_latency_ms: int = 0
        self.is_halted: bool = False
        self.halt_reason: str = ""

    def reset_daily_if_needed(self):
        """Reset daily counters at midnight."""
        today = date.today()
        if today != self.current_date:
            logger.info(f"New day - resetting daily stats. Previous PnL: {self.daily_pnl:.2f}")
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.current_date = today
            self.consecutive_losses = 0
            self.loss_streak_pause_until = None

        # Reset weekly
        if today.weekday() == 0 and today != self.week_start:
            self.weekly_pnl = 0.0
            self.week_start = today

    def update_peak_equity(self, equity: float):
        """Track peak equity for drawdown calculation."""
        if equity > self.peak_equity:
            self.peak_equity = equity

    def get_drawdown_pct(self, equity: float) -> float:
        """Current drawdown from peak."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0, (self.peak_equity - equity) / self.peak_equity * 100)

    # ── Kill Switches ────────────────────────────────────

    def check_kill_switches(
        self,
        spread_usdt: float = 0,
        funding_rate: float = 0,
        api_latency_ms: int = 0,
        trade_direction: Optional[Side] = None,
    ) -> tuple[bool, str]:
        """Check all kill switches. Returns (blocked, reason)."""
        self.api_latency_ms = api_latency_ms

        # Spread kill switch
        if spread_usdt > self.config.max_spread_tolerance_usdt:
            return True, f"SPREAD_KILL: {spread_usdt:.2f} > {self.config.max_spread_tolerance_usdt}"

        # Funding rate kill switch
        if self.config.freeze_on_extreme_funding and trade_direction:
            if abs(funding_rate) > self.config.max_funding_rate_abs:
                # Block if funding is against the trade direction
                if trade_direction == Side.LONG and funding_rate > self.config.max_funding_rate_abs:
                    return True, f"FUNDING_KILL: rate={funding_rate:.6f} against LONG"
                elif trade_direction == Side.SHORT and funding_rate < -self.config.max_funding_rate_abs:
                    return True, f"FUNDING_KILL: rate={funding_rate:.6f} against SHORT"

        # Latency kill switch
        if api_latency_ms > self.config.latency_kill_switch_ms:
            return True, f"LATENCY_KILL: {api_latency_ms}ms > {self.config.latency_kill_switch_ms}ms"

        return False, ""

    # ── Capital Protection ───────────────────────────────

    def check_capital_protection(self, balance: float) -> tuple[bool, str]:
        """Check daily/weekly loss limits and equity floor."""
        self.reset_daily_if_needed()

        # Equity floor
        if balance > 0 and balance < self.config.equity_floor_usdt:
            self.is_halted = True
            self.halt_reason = f"EQUITY_FLOOR: ${balance:.2f} < ${self.config.equity_floor_usdt}"
            return True, self.halt_reason

        # Daily loss limit
        if balance > 0 and self.daily_pnl < 0:
            daily_loss_pct = abs(self.daily_pnl / balance) * 100
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                return True, f"DAILY_LOSS_LIMIT: {daily_loss_pct:.1f}% >= {self.config.max_daily_loss_pct}%"

        # Weekly loss limit
        if balance > 0 and self.weekly_pnl < 0:
            weekly_loss_pct = abs(self.weekly_pnl / balance) * 100
            if weekly_loss_pct >= self.config.weekly_max_loss_pct:
                return True, f"WEEKLY_LOSS_LIMIT: {weekly_loss_pct:.1f}% >= {self.config.weekly_max_loss_pct}%"

        # Drawdown halt
        dd = self.get_drawdown_pct(balance)
        if dd >= self.config.dd_halt_pct:
            self.is_halted = True
            self.halt_reason = f"DRAWDOWN_HALT: {dd:.1f}% >= {self.config.dd_halt_pct}%"
            return True, self.halt_reason

        return False, ""

    # ── Cooldowns ────────────────────────────────────────

    def check_cooldowns(self, trade_direction: Optional[Side] = None) -> tuple[bool, str]:
        """Check all cooldown conditions."""
        now = datetime.now()

        # Global halt
        if self.is_halted:
            return True, f"HALTED: {self.halt_reason}"

        # Loss streak pause
        if self.loss_streak_pause_until and now < self.loss_streak_pause_until:
            remaining = (self.loss_streak_pause_until - now).total_seconds()
            return True, f"LOSS_PAUSE: {self.consecutive_losses} losses, {remaining:.0f}s left"

        # Post-trade cooldown
        if self.last_trade_time:
            elapsed = (now - self.last_trade_time).total_seconds()
            cooldown = self.config.post_loss_cooldown_sec if self.consecutive_losses > 0 else self.config.cooldown_after_trade
            if elapsed < cooldown:
                return True, f"COOLDOWN: {cooldown - elapsed:.0f}s left"

        # Re-entry cooldown (candle-based)
        if self.candles_since_last_trade < self.config.reentry_cooldown_candles:
            return True, f"REENTRY_COOLDOWN: {self.candles_since_last_trade}/{self.config.reentry_cooldown_candles} candles"

        # Same-side loss pause
        if trade_direction:
            side_key = trade_direction.value
            pause_until = self.same_side_pause_until.get(side_key)
            if pause_until and now < pause_until:
                remaining = (pause_until - now).total_seconds()
                return True, f"SAME_SIDE_PAUSE: {side_key} frozen, {remaining:.0f}s left"

        return False, ""

    def can_trade(self, balance: float) -> tuple[bool, str]:
        """Check if trading is allowed based on all risk rules."""
        # Capital protection
        blocked, reason = self.check_capital_protection(balance)
        if blocked:
            return False, reason

        # Cooldowns
        blocked, reason = self.check_cooldowns()
        if blocked:
            return False, reason

        return True, "OK"

    # ── Fee-Aware R:R ────────────────────────────────────

    def calculate_fee_adjusted_rr(
        self, entry_price: float, stop_loss: float, take_profit: float, leverage: int = 1
    ) -> float:
        """Calculate risk/reward ratio accounting for Bybit fees."""
        if entry_price <= 0:
            return 0.0

        if take_profit > entry_price:  # LONG
            tp_pct = (take_profit - entry_price) / entry_price * 100
            sl_pct = (entry_price - stop_loss) / entry_price * 100
        else:  # SHORT
            tp_pct = (entry_price - take_profit) / entry_price * 100
            sl_pct = (stop_loss - entry_price) / entry_price * 100

        if sl_pct <= 0:
            return 0.0

        total_fee_pct = 2 * TAKER_FEE_PCT
        slippage_pct = self.config.max_allowed_slippage_bps / 100.0  # bps to %

        net_tp_pct = tp_pct - total_fee_pct - slippage_pct
        net_sl_pct = sl_pct + total_fee_pct + slippage_pct

        if net_sl_pct <= 0:
            return 0.0

        return round(net_tp_pct / net_sl_pct, 2)

    def is_rr_acceptable(
        self, entry_price: float, stop_loss: float, take_profit: float, leverage: int = 1
    ) -> tuple[bool, float]:
        """Check if fee-adjusted RR meets minimum threshold."""
        rr = self.calculate_fee_adjusted_rr(entry_price, stop_loss, take_profit, leverage)
        return rr >= self.config.min_net_rr, rr

    # ── Dynamic SL/TP ────────────────────────────────────

    def calculate_dynamic_sl_tp(
        self,
        entry_price: float,
        side: Side,
        indicators: Indicators,
        entry_quality: int = 70,
    ) -> tuple[float, float]:
        """Calculate dynamic SL/TP based on ATR and setup quality."""
        atr_val = indicators.atr if indicators.atr > 0 else entry_price * 0.005

        # SL: ATR-based with guardrails
        sl_distance = atr_val * self.config.atr_stop_mult
        sl_pct = (sl_distance / entry_price) * 100
        sl_pct = max(self.config.min_sl_pct, min(sl_pct, self.config.max_sl_pct))
        sl_distance = entry_price * (sl_pct / 100)

        # TP: Scale by quality
        if entry_quality >= 90:
            target_rr = self.config.target_rr_a_plus
        elif entry_quality >= 80:
            target_rr = self.config.target_rr_a
        else:
            target_rr = self.config.target_rr_b

        tp_distance = sl_distance * target_rr
        tp_pct = (tp_distance / entry_price) * 100
        tp_pct = max(self.config.min_tp_pct, min(tp_pct, self.config.max_tp_pct))
        tp_distance = entry_price * (tp_pct / 100)

        if side == Side.LONG:
            stop_loss = round(entry_price - sl_distance, 2)
            take_profit = round(entry_price + tp_distance, 2)
        else:
            stop_loss = round(entry_price + sl_distance, 2)
            take_profit = round(entry_price - tp_distance, 2)

        return stop_loss, take_profit

    # ── Position Sizing ──────────────────────────────────

    def calculate_dynamic_position_size(
        self,
        balance: float,
        entry_price: float,
        stop_loss: float,
        entry_quality: int = 70,
        leverage: int = 10,
    ) -> float:
        """Dynamic position sizing: fixed fractional with quality multiplier."""
        if balance <= 0 or entry_price <= 0:
            return 0.0

        # Base risk from equity tier
        risk_pct = self._get_tier_risk_pct(balance)

        # Quality multiplier
        if entry_quality >= 90:
            quality_mult = self.config.quality_size_mult_a_plus
        elif entry_quality >= 80:
            quality_mult = self.config.quality_size_mult_a
        else:
            quality_mult = self.config.quality_size_mult_b

        # Streak adjustment
        streak_mult = 1.0
        if self.consecutive_losses >= 3:
            streak_mult = self.config.streak_loss_reduction_3
        elif self.consecutive_losses >= 2:
            streak_mult = self.config.streak_loss_reduction_2
        elif self.consecutive_losses >= 1:
            streak_mult = self.config.streak_loss_reduction_1

        # Drawdown adjustment
        dd = self.get_drawdown_pct(balance)
        dd_mult = 1.0
        if dd >= self.config.dd_tier_3_pct:
            dd_mult = self.config.dd_tier_3_mult
        elif dd >= self.config.dd_tier_2_pct:
            dd_mult = self.config.dd_tier_2_mult
        elif dd >= self.config.dd_tier_1_pct:
            dd_mult = self.config.dd_tier_1_mult

        # Final risk
        effective_risk_pct = risk_pct * quality_mult * streak_mult * dd_mult
        effective_risk_pct = min(effective_risk_pct, self.config.max_risk_per_trade_pct)

        # Convert risk % to position size
        risk_amount = balance * (effective_risk_pct / 100)

        # Distance to stop loss
        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return 0.0

        # Position size = risk / (SL distance per unit * leverage effect)
        # With leverage, PnL = price_change * qty * leverage
        # So: risk = sl_distance * qty * leverage
        # qty = risk / (sl_distance * leverage)
        quantity = risk_amount / (sl_distance * leverage)

        # Check max position cap
        max_notional = balance * self.config.max_position_pct
        max_qty = max_notional / entry_price
        quantity = min(quantity, max_qty)

        return round(quantity, 6)

    def _get_tier_risk_pct(self, equity: float) -> float:
        """Get risk % based on equity growth tier."""
        if equity >= self.config.tier_4_equity:
            return self.config.tier_4_risk_pct
        elif equity >= self.config.tier_3_equity:
            return self.config.tier_3_risk_pct
        elif equity >= self.config.tier_2_equity:
            return self.config.tier_2_risk_pct
        else:
            return self.config.tier_1_risk_pct

    def get_current_tier(self, equity: float) -> str:
        """Get current equity tier name."""
        if equity >= self.config.tier_4_equity:
            return f"Tier4 {self.config.tier_4_risk_pct}%/trade"
        elif equity >= self.config.tier_3_equity:
            return f"Tier3 {self.config.tier_3_risk_pct}%/trade"
        elif equity >= self.config.tier_2_equity:
            return f"Tier2 {self.config.tier_2_risk_pct}%/trade"
        else:
            return f"Tier1 {self.config.tier_1_risk_pct}%/trade"

    # ── Position Monitoring ──────────────────────────────

    def calculate_position_size(
        self, balance: float, price: float, indicators: Indicators
    ) -> float:
        """Legacy position size calculation."""
        if balance <= 0 or price <= 0:
            return 0.0
        max_notional = balance * self.config.max_position_pct
        if max_notional < self.config.min_order_usdt:
            return 0.0
        return round(max_notional / price, 6)

    def calculate_stop_loss(
        self, entry_price: float, side: Side, indicators: Indicators
    ) -> float:
        """Calculate dynamic stop-loss based on ATR."""
        atr_stop = indicators.atr * self.config.atr_stop_mult if indicators.atr > 0 else 0
        pct_stop = entry_price * (self.config.stop_loss_pct / 100)
        stop_distance = max(atr_stop, pct_stop)
        if side == Side.LONG:
            return round(entry_price - stop_distance, 2)
        else:
            return round(entry_price + stop_distance, 2)

    def calculate_take_profit(
        self, entry_price: float, side: Side, indicators: Indicators
    ) -> float:
        """Calculate take-profit level."""
        atr_tp = indicators.atr * 2.5 if indicators.atr > 0 else 0
        pct_tp = entry_price * (self.config.take_profit_pct / 100)
        tp_distance = max(atr_tp, pct_tp)
        if side == Side.LONG:
            return round(entry_price + tp_distance, 2)
        else:
            return round(entry_price - tp_distance, 2)

    def should_close_position(
        self, position: Position, current_price: float, indicators: Indicators
    ) -> tuple[bool, str]:
        """Check if an open position should be closed."""
        if position.side == Side.LONG:
            pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
        else:
            pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100

        hold_time_seconds = (datetime.now() - position.open_time).total_seconds()
        min_hold = self.config.min_hold_time

        # ALWAYS respect stop loss
        if position.side == Side.LONG and current_price <= position.stop_loss:
            return True, f"Stop-loss hit at {current_price:.2f}"
        if position.side == Side.SHORT and current_price >= position.stop_loss:
            return True, f"Stop-loss hit at {current_price:.2f}"

        # Take profit hit
        if position.side == Side.LONG and current_price >= position.take_profit:
            return True, f"Take-profit hit at {current_price:.2f}"
        if position.side == Side.SHORT and current_price <= position.take_profit:
            return True, f"Take-profit hit at {current_price:.2f}"

        # Dollar PnL
        dollar_pnl = pnl_pct / 100 * position.entry_price * position.quantity * position.leverage

        # Progressive profit locking after min hold
        if hold_time_seconds >= min_hold:
            if getattr(self.config, 'progressive_stop_enabled', True):
                self._apply_progressive_stop(position, current_price, dollar_pnl)
            elif not getattr(self.config, 'disable_legacy_profit_protection', True):
                self._apply_legacy_trailing(position, current_price, dollar_pnl)

        return False, ""

    # ── Progressive Profit Locking ───────────────────────

    def calculate_progressive_stop(
        self,
        entry_price: float,
        quantity: float,
        leverage: int,
        side: Side,
        current_price: float,
        current_sl: float,
    ) -> Optional[float]:
        """Calculate new SL based on progressive net-PnL step locking."""
        fee_rate = self.config.taker_fee_rate
        slippage = self.config.slippage_buffer_usd
        step = self.config.profit_step_net_usd
        lock = self.config.lock_step_net_usd
        min_improve = self.config.min_stop_improvement_usd

        if side == Side.LONG:
            gross_pnl = (current_price - entry_price) * quantity * leverage
        else:
            gross_pnl = (entry_price - current_price) * quantity * leverage

        if gross_pnl <= 0:
            return None

        entry_fee = entry_price * quantity * fee_rate
        exit_fee = current_price * quantity * fee_rate
        fee_buffer = entry_fee + exit_fee + slippage
        net_pnl = gross_pnl - fee_buffer

        if step <= 0:
            return None

        steps = int(math.floor(net_pnl / step))
        if steps < 1:
            return None

        locked_net = steps * lock
        locked_gross = fee_buffer + locked_net
        price_dist = locked_gross / (quantity * leverage)

        if side == Side.LONG:
            new_sl = entry_price + price_dist
            if new_sl <= current_sl or new_sl >= current_price:
                return None
            improvement_usd = (new_sl - current_sl) * quantity * leverage
        else:
            new_sl = entry_price - price_dist
            if new_sl >= current_sl or new_sl <= current_price:
                return None
            improvement_usd = (current_sl - new_sl) * quantity * leverage

        if improvement_usd < min_improve:
            return None

        return round(new_sl, 2)

    def _apply_progressive_stop(
        self, position: Position, current_price: float, dollar_pnl: float
    ):
        """Apply progressive stop locking to a live position."""
        new_sl = self.calculate_progressive_stop(
            entry_price=position.entry_price,
            quantity=position.quantity,
            leverage=position.leverage,
            side=position.side,
            current_price=current_price,
            current_sl=position.stop_loss,
        )
        if new_sl is None:
            return

        old_sl = position.stop_loss
        position.stop_loss = new_sl
        logger.info(f"Progressive SL UPDATE | SL {old_sl:.2f} → {new_sl:.2f}")

    def _apply_legacy_trailing(
        self, position: Position, current_price: float, dollar_pnl: float
    ):
        """Legacy trailing stop logic."""
        trailing_levels = [
            (10.0, 0.75), (5.0, 0.70), (3.0, 0.60),
            (2.0, 0.50), (1.0, 0.30), (0.50, 0.0),
        ]

        for profit_threshold, lock_pct in trailing_levels:
            if dollar_pnl >= profit_threshold:
                locked_dollar = dollar_pnl * lock_pct
                lock_price_dist = (locked_dollar / position.leverage) / position.quantity
                if position.side == Side.LONG:
                    new_sl = position.entry_price + lock_price_dist
                    if new_sl > position.stop_loss:
                        position.stop_loss = round(new_sl, 2)
                else:
                    new_sl = position.entry_price - lock_price_dist
                    if new_sl < position.stop_loss:
                        position.stop_loss = round(new_sl, 2)
                break

    def record_trade(self, trade: Trade):
        """Record a completed trade for risk tracking."""
        self.daily_pnl += trade.pnl
        self.weekly_pnl += trade.pnl
        self.daily_trades += 1
        self.last_trade_time = datetime.now()
        self.candles_since_last_trade = 0

        if trade.pnl < 0:
            self.consecutive_losses += 1
            # Same-side tracking
            side_key = trade.side.value
            self.same_side_losses[side_key] = self.same_side_losses.get(side_key, 0) + 1

            if self.same_side_losses[side_key] >= self.config.same_side_loss_pause_count:
                self.same_side_pause_until[side_key] = (
                    datetime.now() + timedelta(seconds=self.config.same_side_loss_pause_sec)
                )
                logger.warning(
                    f"{self.same_side_losses[side_key]} consecutive {side_key} losses — "
                    f"freezing {side_key} for {self.config.same_side_loss_pause_sec}s"
                )

            # Global loss streak pause
            if self.consecutive_losses >= 3:
                self.loss_streak_pause_until = (
                    datetime.now() + timedelta(seconds=self.config.post_loss_cooldown_sec)
                )
                logger.warning(
                    f"{self.consecutive_losses} consecutive losses! "
                    f"Pausing for {self.config.post_loss_cooldown_sec}s"
                )
        else:
            self.consecutive_losses = 0
            # Reset same-side losses on win
            side_key = trade.side.value
            self.same_side_losses[side_key] = 0

        logger.info(
            f"Trade recorded: PnL={trade.pnl:.2f} USDT | "
            f"Daily PnL: {self.daily_pnl:.2f} | Weekly: {self.weekly_pnl:.2f} | "
            f"Consecutive losses: {self.consecutive_losses}"
        )

    def increment_candle_count(self):
        """Called each tick to track candles since last trade."""
        self.candles_since_last_trade += 1
