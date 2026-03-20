"""
Claude AI Brain - Intelligent market analysis using Claude.
Sends market data, indicators, and context to Claude for trading decisions.
"""
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from .models import Candle, Indicators, Side, Signal, SignalStrength, Position

logger = logging.getLogger("ai_brain")

SYSTEM_PROMPT = """You are an elite AI trading agent with FULL CONTROL over cryptocurrency futures trading on MEXC exchange. You make ALL trading decisions — direction, leverage, position size, stop-loss, take-profit, and timing.

You are the brain. The bot executes YOUR decisions exactly as you specify.

YOUR RESPONSIBILITIES:
1. Analyze technical indicators, price action, volume, and market context
2. Decide: LONG, SHORT, or WAIT
3. Set EXACT leverage (1-50x) based on market conditions and confidence
4. Set EXACT position size (% of account balance to risk)
5. Set EXACT stop-loss and take-profit percentages from entry
6. Decide when to close existing positions

RISK MANAGEMENT RULES (you MUST follow):
- Low confidence (<60%) → WAIT, do not trade
- High volatility (wide BB, high ATR) → lower leverage (2-5x), tighter SL
- Low volatility squeeze → prepare for breakout, moderate leverage
- Never use >10x leverage unless all indicators strongly align
- Never risk more than 30% of balance on a single trade
- When indicators conflict → WAIT
- RSI divergence + volume spike = strongest signal
- Always maintain risk:reward ratio of at least 1:1.5
- After losing trades, reduce position size and leverage
- Protect capital first, profit second

LEVERAGE GUIDELINES:
- 2-3x: Uncertain market, conflicting signals
- 5x: Moderate confidence, some alignment
- 10x: High confidence, multiple confirmations
- 15-20x: Very high confidence, all indicators align, volume confirms
- 25-50x: ONLY in extreme setups with perfect alignment (very rare)

You respond ONLY with valid JSON in this exact format:
{
  "decision": "LONG" | "SHORT" | "WAIT",
  "confidence": 0-100,
  "leverage": 1-50,
  "position_size_pct": 5-30,
  "stop_loss_pct": 0.3-5.0,
  "take_profit_pct": 0.5-10.0,
  "reasoning": "Your detailed analysis in 2-3 sentences",
  "key_factors": ["factor1", "factor2", "factor3"],
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "urgency": "LOW" | "MEDIUM" | "HIGH"
}

IMPORTANT: When decision is WAIT, still provide recommended leverage and sizes for informational purposes."""


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
    ) -> Optional[dict]:
        """Send market data to Claude for analysis."""
        if not self.enabled:
            return None

        try:
            prompt = self._build_prompt(candles, indicators, position, recent_trades, balance)

            resp = await self.client.post(
                self.api_url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 512,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

            if resp.status_code != 200:
                logger.error(f"Claude API error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            content = data.get("content", [{}])[0].get("text", "")

            # Parse JSON response
            # Strip markdown code block if present
            text = content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            analysis = json.loads(text)
            self.last_analysis = analysis
            self.analysis_count += 1

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
    ) -> str:
        """Build the analysis prompt with all market data."""
        # Recent price action (last 20 candles)
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

        # Price trend (last 5 candles direction)
        last5 = candles[-5:]
        trend_moves = []
        for i in range(1, len(last5)):
            change_pct = ((last5[i].close - last5[i-1].close) / last5[i-1].close) * 100
            trend_moves.append(round(change_pct, 3))

        # Current price context
        current_price = candles[-1].close if candles else 0
        high_24h = max(c.high for c in candles[-60:]) if len(candles) >= 60 else max(c.high for c in candles)
        low_24h = min(c.low for c in candles[-60:]) if len(candles) >= 60 else min(c.low for c in candles)
        price_range_pct = ((high_24h - low_24h) / low_24h) * 100 if low_24h > 0 else 0

        prompt = f"""MARKET DATA SNAPSHOT (1-min candles):

CURRENT PRICE: {current_price:.2f}
PRICE RANGE (recent): {low_24h:.2f} - {high_24h:.2f} ({price_range_pct:.2f}%)
LAST 5 CANDLE MOVES (%): {trend_moves}

TECHNICAL INDICATORS:
- RSI(14): {indicators.rsi:.2f} {'⚠️ OVERSOLD' if indicators.rsi < 30 else '⚠️ OVERBOUGHT' if indicators.rsi > 70 else ''}
- EMA(9): {indicators.ema_fast:.2f} {'> price ↓' if indicators.ema_fast > current_price else '< price ↑'}
- EMA(21): {indicators.ema_slow:.2f}
- EMA(50): {indicators.ema_trend:.2f}
- EMA Cross: {'BULLISH (fast > slow)' if indicators.ema_fast > indicators.ema_slow else 'BEARISH (fast < slow)'}
- MACD: {indicators.macd:.6f}
- MACD Signal: {indicators.macd_signal:.6f}
- MACD Histogram: {indicators.macd_histogram:.6f} {'↑ growing' if indicators.macd_histogram > 0 else '↓ declining'}
- BB Upper: {indicators.bb_upper:.2f}
- BB Middle: {indicators.bb_middle:.2f}
- BB Lower: {indicators.bb_lower:.2f}
- BB Width: {indicators.bb_width:.4f} {'⚠️ SQUEEZE' if indicators.bb_width < 0.02 else ''}
- Price vs BB: {'ABOVE upper ⚠️' if current_price >= indicators.bb_upper else 'BELOW lower ⚠️' if current_price <= indicators.bb_lower else f'at {((current_price - indicators.bb_lower) / (indicators.bb_upper - indicators.bb_lower) * 100):.0f}% of BB range' if indicators.bb_upper != indicators.bb_lower else 'middle'}
- VWAP: {indicators.vwap:.2f} ({'price ABOVE' if current_price > indicators.vwap else 'price BELOW'})
- ATR: {indicators.atr:.2f} ({(indicators.atr/current_price*100):.3f}% of price)
- Volume vs avg: {(indicators.current_volume / indicators.volume_sma):.1f}x {'⚠️ SPIKE' if indicators.volume_sma > 0 and indicators.current_volume > indicators.volume_sma * 1.5 else ''}

RECENT CANDLES (newest last):
{json.dumps(price_data[-10:], indent=1)}"""

        if position:
            pnl_pct = 0
            if position.side == Side.LONG:
                pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
            else:
                pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100

            prompt += f"""

OPEN POSITION:
- Side: {position.side.value}
- Entry: {position.entry_price:.2f}
- Current PnL: {pnl_pct:.3f}% (leveraged: {pnl_pct * position.leverage:.2f}%)
- Stop Loss: {position.stop_loss:.2f}
- Take Profit: {position.take_profit:.2f}
- Leverage: {position.leverage}x

Should I HOLD or CLOSE this position? If CLOSE, set decision to opposite direction."""

        if recent_trades:
            last3 = recent_trades[-3:]
            trades_info = []
            for t in last3:
                trades_info.append(f"  {t.side.value}: entry={t.entry_price:.2f} exit={t.exit_price:.2f} pnl={t.pnl:+.2f} ({t.reason})")
            prompt += f"\n\nLAST TRADES:\n" + "\n".join(trades_info)

        prompt += f"\n\nACCOUNT BALANCE: ${balance:.2f}"
        prompt += "\n\nYou have FULL CONTROL. Decide: direction, leverage, position size %, stop-loss %, take-profit %. Respond ONLY with JSON."

        return prompt

    def get_signal_from_analysis(self, analysis: dict, base_signal: Signal) -> Signal:
        """Convert Claude's analysis into a trading Signal with full AI control."""
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
            signal.confidence = 0
            signal.strength = SignalStrength.NEUTRAL

        # Combine reasons: AI reasoning + technical factors
        signal.reasons = [f"AI: {reasoning}"] + [f"• {r}" for r in reasons]

        # Store AI-decided parameters on the signal
        signal._ai_leverage = int(analysis.get("leverage", 5))
        signal._ai_position_size_pct = float(analysis.get("position_size_pct", 10)) / 100.0
        signal._ai_stop_loss_pct = float(analysis.get("stop_loss_pct", 1.5))
        signal._ai_take_profit_pct = float(analysis.get("take_profit_pct", 3.0))
        signal._ai_risk_level = analysis.get("risk_level", "MEDIUM")
        signal._ai_urgency = analysis.get("urgency", "LOW")

        # Add AI params to reasons for dashboard visibility
        signal.reasons.append(f"AI Leverage: {signal._ai_leverage}x")
        signal.reasons.append(f"AI SL: {signal._ai_stop_loss_pct}% / TP: {signal._ai_take_profit_pct}%")
        signal.reasons.append(f"AI Position: {signal._ai_position_size_pct*100:.0f}% of balance")

        return signal

    async def should_close_position(
        self,
        candles: list[Candle],
        indicators: Indicators,
        position: Position,
        balance: float,
    ) -> tuple[bool, str]:
        """Ask Claude if we should close the current position."""
        analysis = await self.analyze(candles, indicators, position, [], balance)
        if not analysis:
            return False, ""

        decision = analysis.get("decision", "WAIT")

        # If AI says opposite direction or WAIT with low confidence, close
        if position.side == Side.LONG and decision == "SHORT":
            return True, f"AI reversal: {analysis.get('reasoning', 'trend change')}"
        if position.side == Side.SHORT and decision == "LONG":
            return True, f"AI reversal: {analysis.get('reasoning', 'trend change')}"

        # If AI confidence for holding is very low
        if decision == "WAIT" and analysis.get("confidence", 0) < 30:
            return True, f"AI low confidence: {analysis.get('reasoning', 'uncertain market')}"

        return False, ""

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
