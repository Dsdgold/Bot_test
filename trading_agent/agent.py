"""
Main AI Trading Agent.
Orchestrates market analysis, signal generation, and trade execution.
Uses Claude AI for intelligent decision-making.
"""
import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from .config import AgentConfig
from .mexc_client import MEXCClient
from .strategy import ScalpingStrategy
from .risk_manager import RiskManager
from .ai_brain import ClaudeAIBrain
from .models import (
    AccountState, Candle, Position, Signal, Side, Trade, Ticker
)

logger = logging.getLogger("agent")


class TradingAgent:
    """
    AI Trading Agent for MEXC Futures.

    Flow:
    1. Fetch market data (candles, ticker)
    2. Calculate technical indicators
    3. Send data to Claude AI for intelligent analysis
    4. Combine AI decision with technical signals
    5. Check risk rules
    6. Execute trade if confidence is high enough
    7. Monitor open positions (AI-assisted close decisions)
    8. Repeat
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self.client = MEXCClient(config.mexc)
        self.strategy = ScalpingStrategy(config.trading)
        self.risk_manager = RiskManager(config.trading)
        self.ai_brain = ClaudeAIBrain(config.ai.api_key, config.ai.model)

        self.running = False
        self.position: Optional[Position] = None
        self.trades: List[Trade] = []
        self.signals: List[Signal] = []
        self.candles: List[Candle] = []
        self.ticker: Optional[Ticker] = None
        self.account = AccountState()
        self.tick_count = 0

        # Paper trading state
        self.paper_balance: float = 10000.0
        self.paper_position: Optional[Position] = None

        # AI state
        self.ai_reasoning: str = ""
        self.ai_risk_level: str = ""
        self.ai_enabled: bool = bool(config.ai.api_key)

    async def start(self):
        """Start the trading agent."""
        logger.info("=" * 60)
        logger.info("  AI TRADING AGENT - MEXC FUTURES SCALPER")
        logger.info(f"  Symbol: {self.config.trading.symbol}")
        logger.info(f"  Leverage: {self.config.trading.leverage}x")
        logger.info(f"  Mode: {'PAPER' if self.config.paper_trading else 'LIVE'}")
        logger.info(f"  AI Brain: {'ENABLED' if self.ai_enabled else 'DISABLED (no API key)'}")
        if self.ai_enabled:
            logger.info(f"  AI Model: {self.config.ai.model}")
        logger.info("=" * 60)

        self.running = True

        if not self.config.paper_trading:
            await self.client.set_leverage(
                self.config.trading.symbol,
                self.config.trading.leverage,
            )

        while self.running:
            try:
                await self._tick()
                await asyncio.sleep(self.config.trading.analysis_interval)
            except Exception as e:
                logger.error(f"Agent error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def stop(self):
        """Stop the trading agent."""
        self.running = False
        logger.info("Agent stopping...")

        if self.position:
            await self._close_position("Agent shutdown")

        await self.client.close()
        await self.ai_brain.close()
        logger.info("Agent stopped.")

    async def _tick(self):
        """Single analysis-execution cycle."""
        self.tick_count += 1
        symbol = self.config.trading.symbol

        # 1. Fetch data
        self.ticker = await self.client.get_ticker(symbol)
        new_candles = await self.client.get_klines(
            symbol, self.config.trading.candle_interval, 200
        )
        if new_candles:
            self.candles = new_candles

        if not self.candles or not self.ticker:
            logger.warning("No market data available")
            return

        # 2. Update account
        await self._update_account()

        # 3. Calculate technical indicators (always)
        tech_signal = self.strategy.analyze(self.candles)

        # 4. Monitor existing position
        if self.position:
            await self._monitor_position(tech_signal)
            return

        # 5. AI-enhanced signal generation
        signal = await self._generate_ai_signal(tech_signal)
        self.signals.append(signal)
        if len(self.signals) > 500:
            self.signals = self.signals[-500:]

        # 6. Check if we should trade
        if signal.side is None or signal.confidence < self.config.trading.min_confidence:
            return

        can_trade, reason = self.risk_manager.can_trade(self.account.balance)
        if not can_trade:
            logger.info(f"Cannot trade: {reason}")
            return

        # 7. Execute trade
        await self._open_position(signal)

    async def _generate_ai_signal(self, tech_signal: Signal) -> Signal:
        """Combine technical analysis with Claude AI reasoning."""
        # Only call AI every N ticks to save API costs
        use_ai = (
            self.ai_enabled
            and self.tick_count % self.config.ai.analysis_every_n_ticks == 0
        )

        if not use_ai:
            return tech_signal

        # Ask Claude to analyze the market
        analysis = await self.ai_brain.analyze(
            candles=self.candles,
            indicators=tech_signal.indicators or self.strategy.prev_indicators,
            position=None,
            recent_trades=self.trades[-5:] if self.trades else [],
            balance=self.account.balance,
        )

        if not analysis:
            return tech_signal

        # Store AI state for dashboard
        self.ai_reasoning = analysis.get("reasoning", "")
        self.ai_risk_level = analysis.get("risk_level", "")

        # Convert AI analysis to signal
        ai_signal = self.ai_brain.get_signal_from_analysis(analysis, tech_signal)

        # COMBINE: AI decision has priority, but tech must at least partially agree
        # This prevents trading against clear technical signals
        if ai_signal.side and tech_signal.side:
            if ai_signal.side == tech_signal.side:
                # AI and technicals agree → boost confidence
                combined_confidence = min(
                    (ai_signal.confidence * 0.6 + tech_signal.confidence * 0.4),
                    100,
                )
                ai_signal.confidence = combined_confidence
                ai_signal.reasons.append(f"Technical confirmation ({tech_signal.confidence:.0f}%)")
                logger.info(f"AI + Technicals AGREE: {ai_signal.side.value} @ {combined_confidence:.1f}%")
            else:
                # AI and technicals disagree → reduce confidence significantly
                ai_signal.confidence *= 0.4
                ai_signal.reasons.append(f"Technical CONFLICT ({tech_signal.side.value} {tech_signal.confidence:.0f}%)")
                logger.info(f"AI vs Technicals CONFLICT - reducing confidence")
        elif ai_signal.side and not tech_signal.side:
            # AI has a signal but technicals are neutral → moderate confidence
            ai_signal.confidence *= 0.7
            ai_signal.reasons.append("Technicals neutral")

        # Apply AI-suggested SL/TP multipliers
        sl_mult = analysis.get("suggested_sl_multiplier", 1.0)
        tp_mult = analysis.get("suggested_tp_multiplier", 1.0)
        if sl_mult != 1.0:
            ai_signal.reasons.append(f"AI SL mult: {sl_mult:.1f}x")
        if tp_mult != 1.0:
            ai_signal.reasons.append(f"AI TP mult: {tp_mult:.1f}x")

        # Store multipliers on signal for use in position opening
        ai_signal._sl_multiplier = sl_mult
        ai_signal._tp_multiplier = tp_mult

        return ai_signal

    async def _update_account(self):
        """Update account state."""
        if self.config.paper_trading:
            self.account.balance = self.paper_balance
            self.account.available = self.paper_balance
            if self.position:
                price = self.ticker.last_price if self.ticker else 0
                if self.position.side == Side.LONG:
                    pnl = (price - self.position.entry_price) * self.position.quantity
                else:
                    pnl = (self.position.entry_price - price) * self.position.quantity
                self.account.unrealized_pnl = pnl * self.position.leverage
        else:
            balance = await self.client.get_balance()
            if balance > 0:
                self.account.balance = balance
                self.account.available = balance
                logger.info(f"Live balance updated: ${balance:.2f}")
            elif self.account.balance == 0:
                logger.warning("Balance is 0 - check API keys and futures account")

        self.account.daily_pnl = self.risk_manager.daily_pnl
        self.account.total_trades = len(self.trades)
        self.account.win_trades = sum(1 for t in self.trades if t.pnl > 0)
        self.account.loss_trades = sum(1 for t in self.trades if t.pnl <= 0)

    async def _open_position(self, signal: Signal):
        """Open a new position based on signal."""
        if not signal.side or not signal.indicators:
            return

        price = self.ticker.last_price if self.ticker else 0
        if price <= 0:
            return

        quantity = self.risk_manager.calculate_position_size(
            self.account.balance, price, signal.indicators
        )
        if quantity <= 0:
            logger.info("Position size too small, skipping")
            return

        # Calculate SL/TP (use AI multipliers if available)
        sl_mult = getattr(signal, '_sl_multiplier', 1.0)
        tp_mult = getattr(signal, '_tp_multiplier', 1.0)

        stop_loss = self.risk_manager.calculate_stop_loss(
            price, signal.side, signal.indicators
        )
        take_profit = self.risk_manager.calculate_take_profit(
            price, signal.side, signal.indicators
        )

        # Apply AI multipliers to SL/TP distance
        if sl_mult != 1.0:
            sl_distance = abs(price - stop_loss) * sl_mult
            stop_loss = price - sl_distance if signal.side == Side.LONG else price + sl_distance

        if tp_mult != 1.0:
            tp_distance = abs(price - take_profit) * tp_mult
            take_profit = price + tp_distance if signal.side == Side.LONG else price - tp_distance

        logger.info(
            f"\n{'='*50}\n"
            f"  OPENING {signal.side.value} @ {price:.2f}\n"
            f"  Qty: {quantity:.6f} | Leverage: {self.config.trading.leverage}x\n"
            f"  SL: {stop_loss:.2f} | TP: {take_profit:.2f}\n"
            f"  Confidence: {signal.confidence:.1f}%\n"
            f"  AI Reasoning: {self.ai_reasoning[:100]}\n"
            f"  Reasons: {', '.join(signal.reasons[:5])}\n"
            f"{'='*50}"
        )

        if self.config.paper_trading:
            self.position = Position(
                symbol=self.config.trading.symbol,
                side=signal.side,
                entry_price=price,
                quantity=quantity,
                leverage=self.config.trading.leverage,
                stop_loss=round(stop_loss, 2),
                take_profit=round(take_profit, 2),
                order_id=f"paper_{int(datetime.now().timestamp())}",
            )
        else:
            order_id = await self.client.open_position(
                self.config.trading.symbol,
                signal.side,
                quantity,
                self.config.trading.leverage,
            )
            if order_id:
                self.position = Position(
                    symbol=self.config.trading.symbol,
                    side=signal.side,
                    entry_price=price,
                    quantity=quantity,
                    leverage=self.config.trading.leverage,
                    stop_loss=round(stop_loss, 2),
                    take_profit=round(take_profit, 2),
                    order_id=order_id,
                )

    async def _monitor_position(self, tech_signal: Signal):
        """Monitor and manage open position with AI assistance."""
        if not self.position or not self.ticker:
            return

        current_price = self.ticker.last_price
        indicators = self.strategy.prev_indicators

        if not indicators:
            return

        # Update unrealized PnL
        if self.position.side == Side.LONG:
            raw_pnl = (current_price - self.position.entry_price) * self.position.quantity
        else:
            raw_pnl = (self.position.entry_price - current_price) * self.position.quantity

        self.position.unrealized_pnl = raw_pnl * self.position.leverage

        # Check technical SL/TP and trailing stop
        should_close, reason = self.risk_manager.should_close_position(
            self.position, current_price, indicators
        )

        if should_close:
            await self._close_position(reason)
            return

        # Ask AI if we should close (every N ticks)
        if (
            self.ai_enabled
            and self.config.ai.ai_close_decisions
            and self.tick_count % self.config.ai.analysis_every_n_ticks == 0
        ):
            ai_close, ai_reason = await self.ai_brain.should_close_position(
                self.candles, indicators, self.position, self.account.balance
            )
            if ai_close:
                await self._close_position(ai_reason)
                return

            # Update AI reasoning for dashboard
            if self.ai_brain.last_analysis:
                self.ai_reasoning = self.ai_brain.last_analysis.get("reasoning", "")

    async def _close_position(self, reason: str):
        """Close the current position."""
        if not self.position:
            return

        price = self.ticker.last_price if self.ticker else self.position.entry_price

        if self.position.side == Side.LONG:
            raw_pnl = (price - self.position.entry_price) * self.position.quantity
        else:
            raw_pnl = (self.position.entry_price - price) * self.position.quantity

        pnl = raw_pnl * self.position.leverage
        pnl_pct = (raw_pnl / self.position.entry_price) * 100 * self.position.leverage

        trade = Trade(
            symbol=self.position.symbol,
            side=self.position.side,
            entry_price=self.position.entry_price,
            exit_price=price,
            quantity=self.position.quantity,
            leverage=self.position.leverage,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=self.position.open_time,
            exit_time=datetime.now(),
            reason=reason,
        )

        logger.info(
            f"\n{'='*50}\n"
            f"  CLOSING {self.position.side.value} @ {price:.2f}\n"
            f"  PnL: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)\n"
            f"  Reason: {reason}\n"
            f"{'='*50}"
        )

        if not self.config.paper_trading:
            await self.client.close_position(
                self.position.symbol,
                self.position.side,
                self.position.quantity,
            )

        if self.config.paper_trading:
            self.paper_balance += pnl

        self.trades.append(trade)
        self.risk_manager.record_trade(trade)
        self.position = None

    def get_state(self) -> dict:
        """Get current agent state for dashboard."""
        recent_signals = self.signals[-50:] if self.signals else []

        return {
            "running": self.running,
            "symbol": self.config.trading.symbol,
            "leverage": self.config.trading.leverage,
            "mode": "PAPER" if self.config.paper_trading else "LIVE",
            "ai_enabled": self.ai_enabled,
            "ai_reasoning": self.ai_reasoning,
            "ai_risk_level": self.ai_risk_level,
            "ai_analysis_count": self.ai_brain.analysis_count,
            "account": {
                "balance": round(self.account.balance, 2),
                "available": round(self.account.available, 2),
                "unrealized_pnl": round(self.account.unrealized_pnl, 2),
                "daily_pnl": round(self.account.daily_pnl, 2),
                "total_trades": self.account.total_trades,
                "win_trades": self.account.win_trades,
                "loss_trades": self.account.loss_trades,
                "win_rate": round(self.account.win_rate, 1),
            },
            "ticker": {
                "price": self.ticker.last_price if self.ticker else 0,
                "change_24h": self.ticker.change_24h if self.ticker else 0,
                "volume_24h": self.ticker.volume_24h if self.ticker else 0,
                "high_24h": self.ticker.high_24h if self.ticker else 0,
                "low_24h": self.ticker.low_24h if self.ticker else 0,
            } if self.ticker else None,
            "position": {
                "side": self.position.side.value,
                "entry_price": self.position.entry_price,
                "quantity": self.position.quantity,
                "leverage": self.position.leverage,
                "stop_loss": self.position.stop_loss,
                "take_profit": self.position.take_profit,
                "unrealized_pnl": round(self.position.unrealized_pnl, 2),
                "open_time": self.position.open_time.isoformat(),
            } if self.position else None,
            "last_signal": {
                "side": recent_signals[-1].side.value if recent_signals and recent_signals[-1].side else None,
                "strength": recent_signals[-1].strength.value if recent_signals else "NEUTRAL",
                "confidence": round(recent_signals[-1].confidence, 1) if recent_signals else 0,
                "reasons": recent_signals[-1].reasons[:8] if recent_signals else [],
            } if recent_signals else None,
            "indicators": {
                "rsi": round(self.strategy.prev_indicators.rsi, 2),
                "ema_fast": round(self.strategy.prev_indicators.ema_fast, 2),
                "ema_slow": round(self.strategy.prev_indicators.ema_slow, 2),
                "ema_trend": round(self.strategy.prev_indicators.ema_trend, 2),
                "macd": round(self.strategy.prev_indicators.macd, 6),
                "macd_signal": round(self.strategy.prev_indicators.macd_signal, 6),
                "macd_histogram": round(self.strategy.prev_indicators.macd_histogram, 6),
                "bb_upper": round(self.strategy.prev_indicators.bb_upper, 2),
                "bb_middle": round(self.strategy.prev_indicators.bb_middle, 2),
                "bb_lower": round(self.strategy.prev_indicators.bb_lower, 2),
                "vwap": round(self.strategy.prev_indicators.vwap, 2),
                "atr": round(self.strategy.prev_indicators.atr, 2),
            } if self.strategy.prev_indicators else None,
            "trades": [
                {
                    "side": t.side.value,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl": round(t.pnl, 2),
                    "pnl_pct": round(t.pnl_pct, 2),
                    "reason": t.reason,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                }
                for t in self.trades[-50:]
            ],
            "candles": [
                {
                    "t": c.timestamp,
                    "o": c.open,
                    "h": c.high,
                    "l": c.low,
                    "c": c.close,
                    "v": c.volume,
                }
                for c in self.candles[-100:]
            ],
        }
