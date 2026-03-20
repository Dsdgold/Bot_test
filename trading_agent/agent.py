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
from .bybit_client import BybitClient
from .strategy import ScalpingStrategy
from .risk_manager import RiskManager
from .ai_brain import ClaudeAIBrain
from .models import (
    AccountState, Candle, MarketContext, Position, Signal, Side, Trade, Ticker
)

logger = logging.getLogger("agent")


class TradingAgent:
    """
    AI Trading Agent for Bybit Futures.

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
        self.client = BybitClient(config.bybit)
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

        # Market context for advanced analysis
        self.market_context = MarketContext()
        self.prev_open_interest: float = 0.0

        # Auto-tuning state
        self.performance_score: float = 1.0  # Multiplier 0.5-1.5
        self.best_session: str = "US"  # Best performing session
        self.best_hour_win_rate: dict = {}  # Hour -> win rate

    async def start(self):
        """Start the trading agent."""
        logger.info("=" * 60)
        logger.info("  AI TRADING AGENT - BYBIT FUTURES SCALPER")
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

        # 2. Update account (always, even without market data)
        await self._update_account()

        if not self.candles or not self.ticker:
            logger.warning("No market data available")
            return

        # 3. Fetch extended market context (every 3 ticks to save API calls)
        if self.tick_count % 3 == 0:
            await self._update_market_context(symbol)

        # 4. Calculate technical indicators (always)
        tech_signal = self.strategy.analyze(self.candles)

        # 5. Monitor existing position
        if self.position:
            await self._monitor_position(tech_signal)
            return

        # 6. AI-enhanced signal generation
        signal = await self._generate_ai_signal(tech_signal)
        self.signals.append(signal)
        if len(self.signals) > 500:
            self.signals = self.signals[-500:]

        # 7. Check if we should trade
        if signal.side is None or signal.confidence < self.config.trading.min_confidence:
            return

        can_trade, reason = self.risk_manager.can_trade(self.account.balance)
        if not can_trade:
            logger.info(f"Cannot trade: {reason}")
            return

        # 8. Execute trade
        await self._open_position(signal)

    async def _update_market_context(self, symbol: str):
        """Fetch funding rate, open interest, order book, and multi-timeframe data."""
        try:
            # Funding rate
            funding = await self.client.get_funding_rate(symbol)
            if funding:
                self.market_context.funding_rate = float(funding.get("fundingRate", 0))
                self.market_context.next_funding_time = int(funding.get("nextSettleTime", 0))

            # Open interest
            oi_data = await self.client.get_open_interest(symbol)
            if oi_data:
                new_oi = float(oi_data.get("openInterest", oi_data.get("value", 0)))
                if self.prev_open_interest > 0 and new_oi > 0:
                    self.market_context.open_interest_change = (
                        (new_oi - self.prev_open_interest) / self.prev_open_interest * 100
                    )
                self.market_context.open_interest = new_oi
                self.prev_open_interest = new_oi

            # Order book depth
            depth = await self.client.get_depth(symbol, 20)
            if depth:
                bids = depth.get("bids", [])
                asks = depth.get("asks", [])
                if bids and asks:
                    # Find largest bid/ask walls
                    bid_total = 0.0
                    ask_total = 0.0
                    max_bid = (0, 0)
                    max_ask = (0, 0)
                    for b in bids:
                        price = float(b[0]) if isinstance(b, (list, tuple)) else float(b.get("price", 0))
                        size = float(b[1]) if isinstance(b, (list, tuple)) else float(b.get("quantity", 0))
                        bid_total += size
                        if size > max_bid[1]:
                            max_bid = (price, size)
                    for a in asks:
                        price = float(a[0]) if isinstance(a, (list, tuple)) else float(a.get("price", 0))
                        size = float(a[1]) if isinstance(a, (list, tuple)) else float(a.get("quantity", 0))
                        ask_total += size
                        if size > max_ask[1]:
                            max_ask = (price, size)

                    self.market_context.bid_wall_price = max_bid[0]
                    self.market_context.bid_wall_size = max_bid[1]
                    self.market_context.ask_wall_price = max_ask[0]
                    self.market_context.ask_wall_size = max_ask[1]
                    self.market_context.bid_total = bid_total
                    self.market_context.ask_total = ask_total
                    total = bid_total + ask_total
                    self.market_context.book_imbalance = (
                        (bid_total - ask_total) / total * 100 if total > 0 else 0
                    )

            # Multi-timeframe analysis (5m, 15m, 1h)
            mtf = await self.client.get_klines_multi(
                symbol, ["Min5", "Min15", "Min60"], 60
            )
            for interval, candles in mtf.items():
                if len(candles) < 20:
                    continue
                # Simple trend: compare EMA9 vs EMA21 from closes
                closes = [c.close for c in candles]
                ema9 = self._simple_ema(closes, 9)
                ema21 = self._simple_ema(closes, 21)
                rsi = self._simple_rsi(closes, 14)
                trend = "UP" if ema9 > ema21 else "DOWN" if ema9 < ema21 else "NEUTRAL"

                if interval == "Min5":
                    self.market_context.trend_5m = trend
                    self.market_context.rsi_5m = rsi
                elif interval == "Min15":
                    self.market_context.trend_15m = trend
                    self.market_context.rsi_15m = rsi
                elif interval == "Min60":
                    self.market_context.trend_1h = trend
                    self.market_context.rsi_1h = rsi

            # Session timing
            hour = datetime.utcnow().hour
            if 0 <= hour < 8:
                self.market_context.trading_session = "ASIA"
            elif 8 <= hour < 14:
                self.market_context.trading_session = "EUROPE"
            elif 14 <= hour < 21:
                self.market_context.trading_session = "US"
            else:
                self.market_context.trading_session = "OFF_HOURS"

            # Fetch Fear & Greed Index
            try:
                fg_resp = await self.client.client.get(
                    "https://api.alternative.me/fng/?limit=1", timeout=5.0
                )
                fg_data = fg_resp.json()
                if fg_data.get("data"):
                    self.market_context.fear_greed_index = int(fg_data["data"][0].get("value", 50))
                    self.market_context.fear_greed_label = fg_data["data"][0].get("value_classification", "Neutral")
            except Exception:
                pass  # Non-critical

            # Auto-tune based on performance
            self._auto_tune()

            logger.info(
                f"Market context: funding={self.market_context.funding_rate:.6f} | "
                f"OI change={self.market_context.open_interest_change:+.2f}% | "
                f"book={self.market_context.book_imbalance:+.1f}% | "
                f"trends: 5m={self.market_context.trend_5m} 15m={self.market_context.trend_15m} "
                f"1h={self.market_context.trend_1h} | session={self.market_context.trading_session} | "
                f"F&G={self.market_context.fear_greed_index} ({self.market_context.fear_greed_label}) | "
                f"perf_score={self.performance_score:.2f}"
            )

        except Exception as e:
            logger.error(f"Market context update failed: {e}")

    def _auto_tune(self):
        """Analyze past trades and adjust performance score."""
        if len(self.trades) < 3:
            return

        # Analyze last 10 trades
        recent = self.trades[-10:]
        wins = sum(1 for t in recent if t.pnl > 0)
        losses = len(recent) - wins
        recent_win_rate = wins / len(recent) if recent else 0.5

        # Performance score: 0.5 (bad streak) to 1.5 (hot streak)
        if recent_win_rate >= 0.7:
            self.performance_score = min(1.5, self.performance_score + 0.1)
        elif recent_win_rate <= 0.3:
            self.performance_score = max(0.5, self.performance_score - 0.15)
        else:
            # Slowly return to 1.0
            self.performance_score += (1.0 - self.performance_score) * 0.1

        # Track best performing hours
        for t in recent:
            hour = t.entry_time.hour
            if hour not in self.best_hour_win_rate:
                self.best_hour_win_rate[hour] = {"wins": 0, "total": 0}
            self.best_hour_win_rate[hour]["total"] += 1
            if t.pnl > 0:
                self.best_hour_win_rate[hour]["wins"] += 1

        # Adjust session quality
        avg_pnl_by_session = {}
        for t in self.trades:
            hour = t.entry_time.hour
            if 0 <= hour < 8:
                sess = "ASIA"
            elif 8 <= hour < 14:
                sess = "EUROPE"
            elif 14 <= hour < 21:
                sess = "US"
            else:
                sess = "OFF_HOURS"
            if sess not in avg_pnl_by_session:
                avg_pnl_by_session[sess] = []
            avg_pnl_by_session[sess].append(t.pnl)

        if avg_pnl_by_session:
            self.best_session = max(
                avg_pnl_by_session,
                key=lambda s: sum(avg_pnl_by_session[s]) / len(avg_pnl_by_session[s])
            )

    @staticmethod
    def _simple_ema(data: list, period: int) -> float:
        """Calculate simple EMA from a list of values."""
        if len(data) < period:
            return data[-1] if data else 0
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for val in data[period:]:
            ema = (val - ema) * multiplier + ema
        return ema

    @staticmethod
    def _simple_rsi(data: list, period: int = 14) -> float:
        """Calculate RSI from a list of close prices."""
        if len(data) < period + 1:
            return 50.0
        gains = []
        losses = []
        for i in range(1, len(data)):
            change = data[i] - data[i - 1]
            gains.append(max(change, 0))
            losses.append(max(-change, 0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    async def _generate_ai_signal(self, tech_signal: Signal) -> Signal:
        """AI is the PRIMARY decision maker. Called every tick."""
        if not self.ai_enabled:
            return tech_signal

        # Ask Claude to analyze the market with full context
        analysis = await self.ai_brain.analyze(
            candles=self.candles,
            indicators=tech_signal.indicators or self.strategy.prev_indicators,
            position=None,
            recent_trades=self.trades[-5:] if self.trades else [],
            balance=self.account.balance,
            market_context=self.market_context,
            performance_score=self.performance_score,
            best_session=self.best_session,
        )

        if not analysis:
            return tech_signal

        # Store AI state for dashboard
        self.ai_reasoning = analysis.get("reasoning", "")
        self.ai_risk_level = analysis.get("risk_level", "")

        # Convert AI analysis to signal
        ai_signal = self.ai_brain.get_signal_from_analysis(analysis, tech_signal)

        # AI has FULL CONTROL - trust its decision
        # Only log technical agreement/disagreement for info
        if ai_signal.side and tech_signal.side:
            if ai_signal.side == tech_signal.side:
                ai_signal.reasons.append(f"Technicals CONFIRM ({tech_signal.confidence:.0f}%)")
                logger.info(f"AI decision: {ai_signal.side.value} | Technicals AGREE")
            else:
                ai_signal.reasons.append(f"Technicals DISAGREE ({tech_signal.side.value} {tech_signal.confidence:.0f}%)")
                logger.info(f"AI decision: {ai_signal.side.value} | Technicals disagree ({tech_signal.side.value})")
        elif ai_signal.side:
            ai_signal.reasons.append("Technicals neutral")

        logger.info(
            f"AI FULL CONTROL: {analysis.get('decision')} | "
            f"Confidence: {analysis.get('confidence')}% | "
            f"Leverage: {analysis.get('leverage')}x | "
            f"Size: {analysis.get('position_size_pct')}% | "
            f"SL: {analysis.get('stop_loss_pct')}% | "
            f"TP: {analysis.get('take_profit_pct')}%"
        )

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
        """Open a new position based on AI-decided parameters."""
        if not signal.side or not signal.indicators:
            return

        price = self.ticker.last_price if self.ticker else 0
        if price <= 0:
            return

        # Use AI-decided parameters if available, otherwise fall back to config
        ai_leverage = getattr(signal, '_ai_leverage', self.config.trading.leverage)
        ai_position_pct = getattr(signal, '_ai_position_size_pct', self.config.trading.max_position_pct)
        ai_sl_pct = getattr(signal, '_ai_stop_loss_pct', self.config.trading.stop_loss_pct)
        ai_tp_pct = getattr(signal, '_ai_take_profit_pct', self.config.trading.take_profit_pct)

        # AI has full control — only enforce basic sanity
        ai_leverage = max(1, ai_leverage)
        ai_position_pct = max(0.01, min(ai_position_pct, 0.50))

        # Calculate position size using AI-decided percentage
        balance = self.account.balance
        if balance <= 0:
            logger.warning("Cannot open position: balance is 0")
            return

        max_notional = balance * ai_position_pct
        quantity = max_notional / price

        if max_notional < self.config.trading.min_order_usdt:
            logger.info(f"Position size too small (${max_notional:.2f} < ${self.config.trading.min_order_usdt}), skipping")
            return

        quantity = round(quantity, 6)

        # Calculate SL/TP using AI-decided percentages
        sl_distance = price * (ai_sl_pct / 100)
        tp_distance = price * (ai_tp_pct / 100)

        if signal.side == Side.LONG:
            stop_loss = round(price - sl_distance, 2)
            take_profit = round(price + tp_distance, 2)
        else:
            stop_loss = round(price + sl_distance, 2)
            take_profit = round(price - tp_distance, 2)

        logger.info(
            f"\n{'='*50}\n"
            f"  AI OPENING {signal.side.value} @ {price:.2f}\n"
            f"  Qty: {quantity:.6f} | AI Leverage: {ai_leverage}x\n"
            f"  AI SL: {stop_loss:.2f} ({ai_sl_pct}%) | AI TP: {take_profit:.2f} ({ai_tp_pct}%)\n"
            f"  Position: {ai_position_pct*100:.0f}% of ${balance:.2f} = ${max_notional:.2f}\n"
            f"  Confidence: {signal.confidence:.1f}%\n"
            f"  AI Reasoning: {self.ai_reasoning[:100]}\n"
            f"{'='*50}"
        )

        # Set leverage on exchange before opening
        if not self.config.paper_trading:
            await self.client.set_leverage(self.config.trading.symbol, ai_leverage)

        if self.config.paper_trading:
            self.position = Position(
                symbol=self.config.trading.symbol,
                side=signal.side,
                entry_price=price,
                quantity=quantity,
                leverage=ai_leverage,
                stop_loss=stop_loss,
                take_profit=take_profit,
                order_id=f"paper_{int(datetime.now().timestamp())}",
                original_quantity=quantity,
            )
        else:
            # Use market order for reliability
            order_id = await self.client.open_position(
                self.config.trading.symbol,
                signal.side,
                quantity,
                ai_leverage,
                price=None,
                use_limit=False,
            )
            if order_id:
                self.position = Position(
                    symbol=self.config.trading.symbol,
                    side=signal.side,
                    entry_price=price,
                    quantity=quantity,
                    leverage=ai_leverage,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    order_id=order_id,
                    original_quantity=quantity,
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
        leveraged_pnl_pct = (raw_pnl / self.position.entry_price) * 100 * self.position.leverage

        # Partial close: close 50% at first TP level, let rest ride with trailing
        if not self.position.partial_closed and leveraged_pnl_pct >= 1.5:
            half_qty = round(self.position.original_quantity * 0.5, 6)
            if half_qty > 0:
                logger.info(
                    f"PARTIAL CLOSE: Taking 50% profit at {leveraged_pnl_pct:.2f}% "
                    f"(closing {half_qty} of {self.position.quantity})"
                )
                if not self.config.paper_trading:
                    await self.client.close_position_partial(
                        self.position.symbol, self.position.side, half_qty
                    )
                self.position.quantity = round(self.position.quantity - half_qty, 6)
                self.position.partial_closed = True
                # Move SL to break-even after partial close
                self.position.stop_loss = self.position.entry_price
                logger.info(f"SL moved to break-even: {self.position.stop_loss:.2f}")
                if self.config.paper_trading:
                    # Record partial profit for paper trading
                    partial_pnl = raw_pnl * self.position.leverage * 0.5
                    self.paper_balance += partial_pnl

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
                self.candles, indicators, self.position, self.account.balance,
                self.market_context,
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
            "leverage": self.position.leverage if self.position else self.config.trading.leverage,
            "mode": "PAPER" if self.config.paper_trading else "LIVE",
            "ai_enabled": self.ai_enabled,
            "ai_reasoning": self.ai_reasoning,
            "ai_risk_level": self.ai_risk_level,
            "ai_analysis_count": self.ai_brain.analysis_count,
            "market_context": {
                "funding_rate": round(self.market_context.funding_rate, 6),
                "open_interest_change": round(self.market_context.open_interest_change, 2),
                "book_imbalance": round(self.market_context.book_imbalance, 1),
                "trend_5m": self.market_context.trend_5m,
                "trend_15m": self.market_context.trend_15m,
                "trend_1h": self.market_context.trend_1h,
                "trading_session": self.market_context.trading_session,
                "fear_greed_index": self.market_context.fear_greed_index,
                "fear_greed_label": self.market_context.fear_greed_label,
            },
            "performance_score": round(self.performance_score, 2),
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
