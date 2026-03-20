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


class RiskManager:
    """Manages trading risk and position sizing."""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.current_date: date = date.today()
        self.last_trade_time: Optional[datetime] = None
        self.consecutive_losses: int = 0

    def reset_daily_if_needed(self):
        """Reset daily counters at midnight."""
        today = date.today()
        if today != self.current_date:
            logger.info(f"New day - resetting daily stats. Previous PnL: {self.daily_pnl:.2f}")
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.current_date = today
            self.consecutive_losses = 0

    def can_trade(self, balance: float) -> tuple[bool, str]:
        """Check if trading is allowed based on risk rules."""
        self.reset_daily_if_needed()

        # Check daily loss limit
        if balance > 0:
            daily_loss_pct = abs(self.daily_pnl / balance) * 100 if self.daily_pnl < 0 else 0
            if daily_loss_pct >= self.config.max_daily_loss_pct:
                return False, f"Daily loss limit reached ({daily_loss_pct:.1f}%)"

        # Cooldown after trade
        if self.last_trade_time:
            elapsed = (datetime.now() - self.last_trade_time).total_seconds()
            if elapsed < self.config.cooldown_after_trade:
                remaining = self.config.cooldown_after_trade - elapsed
                return False, f"Cooldown: {remaining:.0f}s remaining"

        # Reduce activity after consecutive losses
        if self.consecutive_losses >= 3:
            return False, f"Paused: {self.consecutive_losses} consecutive losses (waiting for reset)"

        return True, "OK"

    def calculate_position_size(
        self, balance: float, price: float, indicators: Indicators
    ) -> float:
        """Calculate position size based on risk parameters."""
        if balance <= 0 or price <= 0:
            return 0.0

        # Base position: percentage of balance
        max_notional = balance * self.config.max_position_pct

        # Adjust by ATR (higher volatility → smaller position)
        if indicators.atr > 0 and price > 0:
            atr_pct = indicators.atr / price
            if atr_pct > 0.02:  # High volatility
                max_notional *= 0.6
                logger.info(f"High volatility (ATR: {atr_pct:.4f}), reducing position")
            elif atr_pct > 0.01:
                max_notional *= 0.8

        # Reduce after losses
        if self.consecutive_losses >= 2:
            max_notional *= 0.5
            logger.info("Reducing position size after consecutive losses")

        # Calculate quantity (contracts/coins)
        quantity = max_notional / price

        # Ensure minimum order
        if max_notional < self.config.min_order_usdt:
            return 0.0

        return round(quantity, 6)

    def calculate_stop_loss(
        self, entry_price: float, side: Side, indicators: Indicators
    ) -> float:
        """Calculate dynamic stop-loss based on ATR."""
        # Use ATR for dynamic SL, with minimum based on config
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

        # Stop loss hit
        if position.side == Side.LONG and current_price <= position.stop_loss:
            return True, f"Stop-loss hit at {current_price:.2f}"
        if position.side == Side.SHORT and current_price >= position.stop_loss:
            return True, f"Stop-loss hit at {current_price:.2f}"

        # Take profit hit
        if position.side == Side.LONG and current_price >= position.take_profit:
            return True, f"Take-profit hit at {current_price:.2f}"
        if position.side == Side.SHORT and current_price <= position.take_profit:
            return True, f"Take-profit hit at {current_price:.2f}"

        # Trailing stop (move stop-loss in profit direction)
        if leveraged_pnl_pct > 1.0:
            trail_pct = self.config.trailing_stop_pct / 100
            if position.side == Side.LONG:
                new_sl = current_price * (1 - trail_pct)
                if new_sl > position.stop_loss:
                    position.stop_loss = round(new_sl, 2)
                    logger.info(f"Trailing stop moved to {position.stop_loss}")
            else:
                new_sl = current_price * (1 + trail_pct)
                if new_sl < position.stop_loss:
                    position.stop_loss = round(new_sl, 2)
                    logger.info(f"Trailing stop moved to {position.stop_loss}")

        # Trend reversal detection
        if position.side == Side.LONG:
            if indicators.ema_fast < indicators.ema_slow and indicators.rsi > 65:
                return True, "Trend reversal (EMA cross + high RSI)"
        else:
            if indicators.ema_fast > indicators.ema_slow and indicators.rsi < 35:
                return True, "Trend reversal (EMA cross + low RSI)"

        return False, ""

    def record_trade(self, trade: Trade):
        """Record a completed trade for risk tracking."""
        self.daily_pnl += trade.pnl
        self.daily_trades += 1
        self.last_trade_time = datetime.now()

        if trade.pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        logger.info(
            f"Trade recorded: PnL={trade.pnl:.2f} USDT ({trade.pnl_pct:.2f}%) | "
            f"Daily PnL: {self.daily_pnl:.2f} | "
            f"Consecutive losses: {self.consecutive_losses}"
        )
