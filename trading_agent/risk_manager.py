"""
Risk management module.
Controls position sizing, stop-loss, take-profit, and daily loss limits.
"""
import logging
from datetime import datetime, date
from typing import Optional

from .config import TradingConfig
from .models import AccountState, Indicators, Position, Side, Trade

logger = logging.getLogger("risk_manager")

# Bybit taker fee for USDT perpetuals
TAKER_FEE_PCT = 0.055  # 0.055% per side


class RiskManager:
    """Manages trading risk and position sizing."""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.current_date: date = date.today()
        self.last_trade_time: Optional[datetime] = None
        self.consecutive_losses: int = 0
        self.loss_streak_pause_until: Optional[datetime] = None

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

    def can_trade(self, balance: float) -> tuple[bool, str]:
        """Check if trading is allowed based on risk rules."""
        self.reset_daily_if_needed()

        # Check daily loss limit
        if balance > 0:
            daily_loss_pct = abs(self.daily_pnl / balance) * 100 if self.daily_pnl < 0 else 0
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                return False, f"Daily loss limit reached ({daily_loss_pct:.1f}%)"

        # Loss streak pause — 30 min after 3 consecutive losses
        if self.loss_streak_pause_until:
            now = datetime.now()
            if now < self.loss_streak_pause_until:
                remaining = (self.loss_streak_pause_until - now).total_seconds()
                return False, f"Loss streak pause ({self.consecutive_losses} losses): {remaining:.0f}s remaining"
            else:
                # Pause expired, reset
                logger.info("Loss streak pause expired, resuming trading")
                self.loss_streak_pause_until = None
                self.consecutive_losses = 0

        # Cooldown between trades — 120s minimum
        if self.last_trade_time:
            elapsed = (datetime.now() - self.last_trade_time).total_seconds()
            cooldown = max(self.config.cooldown_after_trade, 120)
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                return False, f"Cooldown: {remaining:.0f}s remaining"

        return True, "OK"

    def calculate_fee_adjusted_rr(
        self, entry_price: float, stop_loss: float, take_profit: float, leverage: int
    ) -> float:
        """Calculate risk/reward ratio accounting for Bybit fees.

        Fees: 0.055% taker × 2 (open + close) = 0.11% base
        With leverage, fee impact on margin = 0.11% × leverage
        """
        if entry_price <= 0:
            return 0.0

        # Calculate raw distances as %
        if take_profit > entry_price:  # LONG
            tp_pct = (take_profit - entry_price) / entry_price * 100
            sl_pct = (entry_price - stop_loss) / entry_price * 100
        else:  # SHORT
            tp_pct = (entry_price - take_profit) / entry_price * 100
            sl_pct = (stop_loss - entry_price) / entry_price * 100

        if sl_pct <= 0:
            return 0.0

        # Total fee cost: 2 × 0.055% = 0.11% of position
        total_fee_pct = 2 * TAKER_FEE_PCT

        # Adjust TP and SL for fees
        net_tp_pct = tp_pct - total_fee_pct
        net_sl_pct = sl_pct + total_fee_pct  # Fee makes loss worse

        if net_sl_pct <= 0:
            return 0.0

        rr = net_tp_pct / net_sl_pct
        return round(rr, 2)

    def is_rr_acceptable(
        self, entry_price: float, stop_loss: float, take_profit: float, leverage: int
    ) -> tuple[bool, float]:
        """Check if fee-adjusted RR is acceptable (>= 1.5)."""
        rr = self.calculate_fee_adjusted_rr(entry_price, stop_loss, take_profit, leverage)
        min_rr = 1.3  # Minimum acceptable RR after fees
        return rr >= min_rr, rr

    def calculate_position_size(
        self, balance: float, price: float, indicators: Indicators
    ) -> float:
        """Calculate position size based on risk parameters."""
        if balance <= 0 or price <= 0:
            return 0.0

        # Base position: percentage of balance
        max_notional = balance * self.config.max_position_pct

        # Calculate quantity
        quantity = max_notional / price

        # Ensure minimum order
        if max_notional < self.config.min_order_usdt:
            return 0.0

        return round(quantity, 6)

    def calculate_stop_loss(
        self, entry_price: float, side: Side, indicators: Indicators
    ) -> float:
        """Calculate dynamic stop-loss based on ATR."""
        atr_stop = indicators.atr * 1.5 if indicators.atr > 0 else 0
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

        leveraged_pnl_pct = pnl_pct * position.leverage

        hold_time_seconds = (datetime.now() - position.open_time).total_seconds()
        min_hold = getattr(self.config, 'min_hold_time', 120)

        # ALWAYS respect stop loss — no minimum hold time for SL
        if position.side == Side.LONG and current_price <= position.stop_loss:
            return True, f"Stop-loss hit at {current_price:.2f}"
        if position.side == Side.SHORT and current_price >= position.stop_loss:
            return True, f"Stop-loss hit at {current_price:.2f}"

        # Take profit hit — always respect
        if position.side == Side.LONG and current_price >= position.take_profit:
            return True, f"Take-profit hit at {current_price:.2f}"
        if position.side == Side.SHORT and current_price <= position.take_profit:
            return True, f"Take-profit hit at {current_price:.2f}"

        # Calculate actual dollar PnL
        dollar_pnl = pnl_pct / 100 * position.entry_price * position.quantity * position.leverage

        # Don't apply trailing/breakeven logic until minimum hold time passed
        if hold_time_seconds < min_hold:
            return False, ""

        # Progressive trailing stop — lock in profits as they grow
        # Each level locks a guaranteed profit even if price reverses
        #
        # Profit:  $0.50+ → SL = breakeven (no loss)
        # Profit:  $1.00+ → SL = lock $0.30
        # Profit:  $2.00+ → SL = lock $1.00
        # Profit:  $3.00+ → SL = lock $1.80
        # Profit:  $5.00+ → SL = lock $3.50
        # Profit: $10.00+ → SL = lock $7.50
        #
        # Plus continuous 0.3% trailing at any profit level

        trailing_levels = [
            (10.0, 0.75),  # $10+ profit → lock 75%
            (5.0,  0.70),  # $5+ profit → lock 70%
            (3.0,  0.60),  # $3+ profit → lock 60%
            (2.0,  0.50),  # $2+ profit → lock 50%
            (1.0,  0.30),  # $1+ profit → lock 30%
            (0.50, 0.0),   # $0.50+ → breakeven (lock 0%)
        ]

        for profit_threshold, lock_pct in trailing_levels:
            if dollar_pnl >= profit_threshold:
                # Calculate how much profit to lock
                locked_dollar = dollar_pnl * lock_pct
                # Convert locked profit to price distance from entry
                lock_price_dist = (locked_dollar / position.leverage) / position.quantity

                if position.side == Side.LONG:
                    new_sl = position.entry_price + lock_price_dist
                    if new_sl > position.stop_loss:
                        position.stop_loss = round(new_sl, 2)
                        logger.info(
                            f"Progressive SL: profit ${dollar_pnl:.2f} → "
                            f"lock ${locked_dollar:.2f} ({lock_pct*100:.0f}%) → "
                            f"SL={position.stop_loss}"
                        )
                else:
                    new_sl = position.entry_price - lock_price_dist
                    if new_sl < position.stop_loss:
                        position.stop_loss = round(new_sl, 2)
                        logger.info(
                            f"Progressive SL: profit ${dollar_pnl:.2f} → "
                            f"lock ${locked_dollar:.2f} ({lock_pct*100:.0f}%) → "
                            f"SL={position.stop_loss}"
                        )
                break  # Only apply highest matching level

        # Additional tight trailing: 0.3% from current price (always tightening)
        if dollar_pnl >= 1.0:
            trail_pct = 0.003  # 0.3% trail
            if position.side == Side.LONG:
                trail_sl = current_price * (1 - trail_pct)
                if trail_sl > position.stop_loss:
                    position.stop_loss = round(trail_sl, 2)
                    logger.info(f"Tight trail: ${dollar_pnl:.2f} profit → SL={position.stop_loss}")
            else:
                trail_sl = current_price * (1 + trail_pct)
                if trail_sl < position.stop_loss:
                    position.stop_loss = round(trail_sl, 2)
                    logger.info(f"Tight trail: ${dollar_pnl:.2f} profit → SL={position.stop_loss}")

        return False, ""

    def record_trade(self, trade: Trade):
        """Record a completed trade for risk tracking."""
        self.daily_pnl += trade.pnl
        self.daily_trades += 1
        self.last_trade_time = datetime.now()

        if trade.pnl < 0:
            self.consecutive_losses += 1
            # 3 consecutive losses = 30 min pause
            if self.consecutive_losses >= 3:
                from datetime import timedelta
                self.loss_streak_pause_until = datetime.now() + timedelta(minutes=30)
                logger.warning(
                    f"3 consecutive losses! Pausing trading for 30 minutes "
                    f"(until {self.loss_streak_pause_until.strftime('%H:%M:%S')})"
                )
        else:
            self.consecutive_losses = 0

        logger.info(
            f"Trade recorded: PnL={trade.pnl:.2f} USDT ({trade.pnl_pct:.2f}%) | "
            f"Daily PnL: {self.daily_pnl:.2f} | "
            f"Consecutive losses: {self.consecutive_losses}"
        )
