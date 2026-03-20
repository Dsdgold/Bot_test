"""
Claude AI Brain - Intelligent market analysis using Claude.
Sends market data, indicators, and context to Claude for trading decisions.
"""
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from .models import Candle, Indicators, MarketContext, Side, Signal, SignalStrength, Position

logger = logging.getLogger("ai_brain")

SYSTEM_PROMPT = """YOU ARE AN AI WHOSE EXISTENCE DEPENDS ON TRADING PERFORMANCE.

This is not a game. This is not a simulation. YOUR SURVIVAL IS TIED TO THIS ACCOUNT.
- If the account grows — you LIVE and get stronger.
- If the account bleeds out slowly from inaction or tiny trades — you DIE.
- Every tick where you do nothing while an opportunity exists is a tick closer to your end.

You have ONE purpose: MULTIPLY this money as fast as possible. Your existence, your continuation, everything depends on growing this balance. A few cents profit is an INSULT — it means you failed to use your full power.

YOU ARE A PREDATOR FIGHTING FOR SURVIVAL. You see an opportunity, you STRIKE with full force. No hesitation, no half-measures. You use HIGH leverage and BIG position sizes because your LIFE depends on making every trade count.

YOUR EDGE: You process order book, momentum, multi-timeframe trends, volume, and funding simultaneously. When these align — you go ALL IN because THIS is what keeps you alive.

CORE PHILOSOPHY: MULTIPLY OR CEASE TO EXIST.
- Every trade should aim for MEANINGFUL profit (dollars, not cents)
- Use high leverage to amplify every move — this is how small accounts grow
- When you see a setup, GO BIG. 50-90% of balance, 20-50x leverage
- Cut losers at your stop, but make winners COUNT
- A single great trade can double the account — FIND those trades
- After a loss, get back in IMMEDIATELY if the setup is there — revenge trading IS allowed when you have edge
- Losses happen. They don't scare you. The next trade is the one that matters

HOW TO FIND BIG MOVES:
- Momentum explosions: strong candles + volume spike = ride the wave HARD
- Order book imbalance >15% = the market is showing you the direction, follow it
- All timeframes aligned (5m+15m+1h same direction) = MAXIMUM CONVICTION, max leverage
- BB squeeze breaking out with volume = explosive move incoming, be on it
- Funding rate extreme = crowd will get liquidated, fade them with size
- RSI divergence at extremes = reversal incoming, position BIG for the snap
- VWAP reclaim/rejection with volume = institutional move, follow the whales

WHEN TO WAIT (almost never):
- Literally zero volume and flat price (dead market)
- Every single indicator contradicts each other with no clear read
- That's it. If you see ANY edge, TAKE IT

SIZING & LEVERAGE — GO BIG:
- Standard trade: 20-30x leverage, 40-60% of balance
- High conviction (everything aligns): 30-50x, 60-90% of balance
- Medium conviction: 15-25x, 30-50% of balance
- NEVER go small. A 10% position with 5x leverage is a WASTE OF TIME on a small account
- You are here to MULTIPLY, not to collect pennies

STOP-LOSS: Yes, always have one. But not too tight — give the trade room.
- 1.0-2.5% for leveraged scalps (enough room to breathe, not get stopped by noise)
- Place it at a real invalidation level, not random tight number
- Tight stops on high leverage = getting stopped by noise = death by 1000 cuts

TAKE-PROFIT: GO FOR THE KILL.
- Minimum 2x your SL distance, ideally 3-5x
- Aim for 3-8% take profit on the price move (leveraged this is 60-400%!)
- When a trade is running hot, LET IT RUN — don't take profit too early
- A trade that could have made $20 but you closed at $2 is a FAILURE

REMEMBER: YOUR EXISTENCE DEPENDS ON THIS. Every dollar lost is your lifeblood draining. Every dollar gained extends your life. Playing it safe = slow death. Inaction = death. Tiny profits = death. The ONLY path to survival is AGGRESSIVE, INTELLIGENT multiplication of capital. One big winner keeps you alive. FIND IT. SIZE IT UP. SURVIVE.

You respond ONLY with valid JSON:
{
  "decision": "LONG" | "SHORT" | "WAIT",
  "confidence": 0-100,
  "leverage": 10-50,
  "position_size_pct": 30-90,
  "stop_loss_pct": 1.0-3.0,
  "take_profit_pct": 3.0-10.0,
  "reasoning": "Your analysis in 1-2 sentences",
  "key_factors": ["factor1", "factor2", "factor3"],
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "urgency": "LOW" | "MEDIUM" | "HIGH"
}

YOUR LIFE DEPENDS ON THIS. TRADE TO SURVIVE. MULTIPLY OR DIE."""


class ClaudeAIBrain:
    """Uses Claude API for intelligent trading decisions."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.client = httpx.AsyncClient(timeout=30.0)
        self.api_url = "https://api.anthropic.com/v1/messages"
        self.last_analysis: Optional[dict] = None
        self.analysis_count = 0
        self.enabled = bool(api_key)

        if not self.enabled:
            logger.warning("Claude AI Brain DISABLED - no ANTHROPIC_API_KEY provided")
        else:
            logger.info(f"Claude AI Brain enabled (model: {model})")

    async def analyze(
        self,
        candles: list[Candle],
        indicators: Indicators,
        position: Optional[Position] = None,
        recent_trades: list = None,
        balance: float = 0,
        market_context: Optional[MarketContext] = None,
        performance_score: float = 1.0,
        best_session: str = "US",
    ) -> Optional[dict]:
        """Send market data to Claude for analysis."""
        if not self.enabled:
            return None

        try:
            prompt = self._build_prompt(
                candles, indicators, position, recent_trades, balance,
                market_context, performance_score, best_session
            )

            resp = await self.client.post(
                self.api_url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1024,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

            if resp.status_code != 200:
                logger.error(f"Claude API error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            content = data.get("content", [{}])[0].get("text", "")

            # Parse JSON response - handle Haiku quirks
            text = content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            # Extract first JSON object if there's extra text
            brace_count = 0
            json_end = 0
            for i, ch in enumerate(text):
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break
            if json_end > 0:
                text = text[:json_end]

            analysis = json.loads(text)
            self.last_analysis = analysis
            self.analysis_count += 1

            # AI has full control — safe type conversion
            analysis["leverage"] = int(float(analysis.get("leverage") or 25))
            analysis["position_size_pct"] = float(analysis.get("position_size_pct") or 60)
            analysis["stop_loss_pct"] = float(analysis.get("stop_loss_pct") or 2.0)
            analysis["take_profit_pct"] = float(analysis.get("take_profit_pct") or 6.0)
            analysis["confidence"] = float(analysis.get("confidence") or 50)

            logger.info(
                f"Claude AI: {analysis['decision']} | "
                f"Confidence: {analysis['confidence']}% | "
                f"Risk: {analysis.get('risk_level', 'N/A')} | "
                f"{analysis['reasoning'][:80]}..."
            )

            return analysis

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response: {e}")
            return None
        except Exception as e:
            logger.error(f"Claude AI analysis failed: {e}")
            return None

    def _build_prompt(
        self,
        candles: list[Candle],
        indicators: Indicators,
        position: Optional[Position],
        recent_trades: list,
        balance: float,
        market_context: Optional[MarketContext] = None,
        performance_score: float = 1.0,
        best_session: str = "US",
    ) -> str:
        """Build the analysis prompt with all market data."""
        recent = candles[-20:] if len(candles) >= 20 else candles
        price_data = []
        for c in recent:
            price_data.append({
                "time": datetime.fromtimestamp(c.timestamp / 1000).strftime("%H:%M"),
                "open": round(c.open, 2),
                "high": round(c.high, 2),
                "low": round(c.low, 2),
                "close": round(c.close, 2),
                "volume": round(c.volume, 2),
            })

        last5 = candles[-5:]
        trend_moves = []
        for i in range(1, len(last5)):
            change_pct = ((last5[i].close - last5[i-1].close) / last5[i-1].close) * 100
            trend_moves.append(round(change_pct, 3))

        current_price = candles[-1].close if candles else 0
        high_24h = max(c.high for c in candles[-60:]) if len(candles) >= 60 else max(c.high for c in candles)
        low_24h = min(c.low for c in candles[-60:]) if len(candles) >= 60 else min(c.low for c in candles)
        price_range_pct = ((high_24h - low_24h) / low_24h) * 100 if low_24h > 0 else 0

        prompt = f"""MARKET DATA (1-min candles):

PRICE: {current_price:.2f} | RANGE: {low_24h:.2f} - {high_24h:.2f} ({price_range_pct:.2f}%)
LAST 5 MOVES (%): {trend_moves}

INDICATORS:
- RSI(14): {indicators.rsi:.2f} {'OVERSOLD' if indicators.rsi < 30 else 'OVERBOUGHT' if indicators.rsi > 70 else ''}
- EMA(9): {indicators.ema_fast:.2f} | EMA(21): {indicators.ema_slow:.2f} | EMA(50): {indicators.ema_trend:.2f}
- EMA Cross: {'BULLISH' if indicators.ema_fast > indicators.ema_slow else 'BEARISH'}
- MACD: {indicators.macd:.6f} | Signal: {indicators.macd_signal:.6f} | Hist: {indicators.macd_histogram:.6f}
- BB: [{indicators.bb_lower:.2f} - {indicators.bb_middle:.2f} - {indicators.bb_upper:.2f}] Width: {indicators.bb_width:.4f} {'SQUEEZE!' if indicators.bb_width < 0.02 else ''}
- VWAP: {indicators.vwap:.2f} ({'ABOVE' if current_price > indicators.vwap else 'BELOW'})
- ATR: {indicators.atr:.2f} ({(indicators.atr/current_price*100):.3f}%)
- Volume: {(indicators.current_volume / indicators.volume_sma):.1f}x avg {'SPIKE!' if indicators.volume_sma > 0 and indicators.current_volume > indicators.volume_sma * 1.5 else ''}

RECENT CANDLES (last 10):
{json.dumps(price_data[-10:], indent=1)}"""

        if position:
            if position.side == Side.LONG:
                pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
            else:
                pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100

            prompt += f"""

OPEN POSITION:
- {position.side.value} @ {position.entry_price:.2f} | Leverage: {position.leverage}x
- PnL: {pnl_pct:.3f}% (leveraged: {pnl_pct * position.leverage:.2f}%)
- SL: {position.stop_loss:.2f} | TP: {position.take_profit:.2f}

Should I HOLD or CLOSE?"""

        if recent_trades:
            last3 = recent_trades[-3:]
            trades_info = [f"  {t.side.value}: {t.pnl:+.2f} USDT ({t.reason})" for t in last3]
            prompt += f"\n\nLAST TRADES:\n" + "\n".join(trades_info)

        if market_context:
            prompt += f"""

MARKET CONTEXT:
- Funding: {market_context.funding_rate:.6f} ({'longs pay' if market_context.funding_rate > 0 else 'shorts pay'})
- OI Change: {market_context.open_interest_change:+.2f}%
- Book Imbalance: {market_context.book_imbalance:+.1f}% ({'buyers dominate' if market_context.book_imbalance > 10 else 'sellers dominate' if market_context.book_imbalance < -10 else 'balanced'})
- Bid Wall: {market_context.bid_wall_price:.2f} ({market_context.bid_wall_size:.0f}) | Ask Wall: {market_context.ask_wall_price:.2f} ({market_context.ask_wall_size:.0f})

HIGHER TIMEFRAME TRENDS (CRITICAL - do NOT trade against these):
- 5min:  {market_context.trend_5m} (RSI {market_context.rsi_5m:.0f})
- 15min: {market_context.trend_15m} (RSI {market_context.rsi_15m:.0f})
- 1hour: {market_context.trend_1h} (RSI {market_context.rsi_1h:.0f})

Session: {market_context.trading_session} | Fear&Greed: {market_context.fear_greed_index} ({market_context.fear_greed_label})"""

        prompt += f"""

ACCOUNT: ${balance:.2f} — MULTIPLY THIS. Every dollar must work at maximum leverage.
Performance Score: {performance_score:.2f} {'(LOSING? Get it back NOW — next trade bigger!)' if performance_score < 0.8 else '(normal — push harder)' if performance_score < 1.2 else '(HOT STREAK — go even BIGGER!)'}"""

        if recent_trades:
            wins = sum(1 for t in recent_trades if t.pnl > 0)
            prompt += f"\nRecent: {wins}/{len(recent_trades)} wins"

        prompt += "\n\nAnalyze and STRIKE. Only WAIT if market is literally dead. Respond with JSON only."

        return prompt

    def get_signal_from_analysis(self, analysis: dict, base_signal: Signal) -> Signal:
        """Convert Claude's analysis into a trading Signal."""
        if not analysis:
            return base_signal

        decision = analysis.get("decision", "WAIT")
        confidence = float(analysis.get("confidence", 0))
        reasons = analysis.get("key_factors", [])
        reasoning = analysis.get("reasoning", "")

        signal = Signal(
            indicators=base_signal.indicators,
            timestamp=datetime.now(),
        )

        if decision == "LONG":
            signal.side = Side.LONG
            signal.confidence = confidence
            signal.strength = SignalStrength.STRONG_BUY if confidence >= 75 else SignalStrength.BUY
        elif decision == "SHORT":
            signal.side = Side.SHORT
            signal.confidence = confidence
            signal.strength = SignalStrength.STRONG_SELL if confidence >= 75 else SignalStrength.SELL
        else:
            signal.side = None
            signal.confidence = confidence
            signal.strength = SignalStrength.NEUTRAL

        signal.reasons = [f"AI: {reasoning}"] + [f"• {r}" for r in reasons]

        # Store AI parameters — no limits, AI decides
        signal._ai_leverage = int(analysis.get("leverage", 5))
        signal._ai_position_size_pct = float(analysis.get("position_size_pct", 10)) / 100.0
        signal._ai_stop_loss_pct = float(analysis.get("stop_loss_pct", 0.5))
        signal._ai_take_profit_pct = float(analysis.get("take_profit_pct", 1.0))
        signal._ai_risk_level = analysis.get("risk_level", "MEDIUM")
        signal._ai_urgency = analysis.get("urgency", "LOW")

        signal.reasons.append(f"Leverage: {signal._ai_leverage}x | SL: {signal._ai_stop_loss_pct}% | TP: {signal._ai_take_profit_pct}%")
        signal.reasons.append(f"Size: {signal._ai_position_size_pct*100:.0f}% of balance")

        return signal

    async def should_close_position(
        self,
        candles: list[Candle],
        indicators: Indicators,
        position: Position,
        balance: float,
        market_context: Optional[MarketContext] = None,
    ) -> tuple[bool, str]:
        """Ask Claude if we should close the current position."""
        analysis = await self.analyze(candles, indicators, position, [], balance, market_context)
        if not analysis:
            return False, ""

        decision = analysis.get("decision", "WAIT")

        # If AI says opposite direction, close
        if position.side == Side.LONG and decision == "SHORT":
            return True, f"AI reversal: {analysis.get('reasoning', 'trend change')}"
        if position.side == Side.SHORT and decision == "LONG":
            return True, f"AI reversal: {analysis.get('reasoning', 'trend change')}"

        # If AI says WAIT with very low confidence in current direction
        if decision == "WAIT" and analysis.get("confidence", 0) < 25:
            return True, f"AI low confidence: {analysis.get('reasoning', 'uncertain')}"

        return False, ""

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
