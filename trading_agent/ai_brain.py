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

SYSTEM_PROMPT = """Aggressive crypto scalper. $60→$500. TRADE when trend is clear. WAIT only if TFs disagree.
2+TFs same dir=TRADE. All aligned=min grade B. Follow trend: DOWN=SHORT, UP=LONG.
Lev: C:15-25x B:25-40x A+:40-50x. SL:0.8-1.5% TP:1.5-4.0%. Size: C:50-70% B:70-85% A+:85-90%.
Hold winners to TP. Close on reversal/invalidation only. Grade D=W(WAIT).
Output RAW JSON only: {"a":"L|S|W|C","g":"A+|B|C|D","c":0-100,"lev":0,"m":0,"sl":0,"tp":0,"ts":0,"rr":0,"rc":[""],"iv":[""]}"""


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
                    "max_tokens": 100,
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

            raw = json.loads(text)
            self.last_analysis = raw
            self.analysis_count += 1

            # Map compact fields to internal names
            action_map = {"L": "LONG", "S": "SHORT", "W": "WAIT", "C": "CLOSE"}
            action_raw = str(raw.get("a", "W")).upper().strip()
            decision = action_map.get(action_raw, action_raw)  # Accept both L and LONG

            tp_val = raw.get("tp", 3.0)
            if isinstance(tp_val, list):
                tp_pct = float(tp_val[0]) if tp_val else 3.0
            else:
                tp_pct = float(tp_val or 3.0)

            analysis = {
                "decision": decision,
                "confidence": float(raw.get("c", 50)),
                "leverage": int(float(raw.get("lev", 20))),
                "position_size_pct": float(raw.get("m", 50)),
                "stop_loss_pct": float(raw.get("sl", 1.5)),
                "take_profit_pct": tp_pct,
                "grade": str(raw.get("g", "B")),
                "rr": float(raw.get("rr", 0)),
                "trailing_stop_pct": float(raw.get("ts", 0)),
                "reason_codes": raw.get("rc", []),
                "invalidation_codes": raw.get("iv", []),
                "reasoning": ", ".join(raw.get("rc", [])) or "AI decision",
                "key_factors": raw.get("rc", []),
                "risk_level": "HIGH" if str(raw.get("g", "B")) in ("A+", "A") else "MEDIUM" if str(raw.get("g", "B")) == "B" else "LOW",
            }

            logger.info(
                f"Claude AI: {analysis['decision']} | "
                f"Grade: {analysis['grade']} | "
                f"Conf: {analysis['confidence']}% | "
                f"Lev: {analysis['leverage']}x | "
                f"RR: {analysis['rr']:.1f} | "
                f"RC: {analysis['reason_codes']}"
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
        """Build ultra-compact market data prompt to minimize token usage."""
        current_price = candles[-1].close if candles else 0

        # Compact candle summary: last 5 only
        recent = candles[-5:]
        candle_lines = []
        for c in recent:
            t = datetime.fromtimestamp(c.timestamp / 1000).strftime("%H:%M")
            candle_lines.append(f"{t} {c.close:.1f} {c.volume:.0f}")

        # Last 5 moves as %
        last5 = candles[-5:]
        moves = []
        for i in range(1, len(last5)):
            moves.append(round(((last5[i].close - last5[i-1].close) / last5[i-1].close) * 100, 3))

        high_h = max(c.high for c in candles[-60:]) if len(candles) >= 60 else max(c.high for c in candles)
        low_h = min(c.low for c in candles[-60:]) if len(candles) >= 60 else min(c.low for c in candles)

        vol_ratio = (indicators.current_volume / indicators.volume_sma) if indicators.volume_sma > 0 else 1.0
        ema_cross = "BULL" if indicators.ema_fast > indicators.ema_slow else "BEAR"

        prompt = f"P:{current_price:.0f} R:{low_h:.0f}-{high_h:.0f} M:{moves}\n"
        prompt += f"RSI:{indicators.rsi:.0f} EMA:{ema_cross} MACD_H:{indicators.macd_histogram:.4f} ATR:{(indicators.atr/current_price*100):.2f}% VOL:{vol_ratio:.1f}x\n"
        prompt += " ".join(candle_lines)

        if position:
            if position.side == Side.LONG:
                pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
            else:
                pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100
            prompt += f"\nPOS:{position.side.value} @{position.entry_price:.2f} {position.leverage}x PnL:{pnl_pct:.3f}%({pnl_pct*position.leverage:.1f}%lev) SL:{position.stop_loss:.2f} TP:{position.take_profit:.2f}"

        if recent_trades:
            last3 = recent_trades[-3:]
            t_info = " ".join([f"{t.side.value[0]}:{t.pnl:+.2f}" for t in last3])
            wins = sum(1 for t in recent_trades if t.pnl > 0)
            prompt += f"\nTRADES:{t_info} W:{wins}/{len(recent_trades)}"

        if market_context:
            prompt += f"\nOI:{market_context.open_interest_change:+.1f}% BOOK:{market_context.book_imbalance:+.0f}%"
            prompt += f"\n5m:{market_context.trend_5m} 15m:{market_context.trend_15m} 1h:{market_context.trend_1h} FG:{market_context.fear_greed_index}"

        prompt += f"\n${balance:.0f}→$500 JSON:"

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
            signal.strength = SignalStrength.STRONG_BUY if confidence >= 65 else SignalStrength.BUY
        elif decision == "SHORT":
            signal.side = Side.SHORT
            signal.confidence = confidence
            signal.strength = SignalStrength.STRONG_SELL if confidence >= 65 else SignalStrength.SELL
        else:
            signal.side = None
            signal.confidence = confidence
            signal.strength = SignalStrength.NEUTRAL

        grade = analysis.get("grade", "B")
        rr = analysis.get("rr", 0)
        signal.reasons = [f"AI[{grade}] RR:{rr:.1f}: {reasoning}"] + [f"• {r}" for r in reasons]

        # Store AI parameters — no limits, AI decides
        signal._ai_leverage = int(analysis.get("leverage", 5))
        signal._ai_position_size_pct = float(analysis.get("position_size_pct", 10)) / 100.0
        signal._ai_stop_loss_pct = float(analysis.get("stop_loss_pct", 0.5))
        signal._ai_take_profit_pct = float(analysis.get("take_profit_pct", 1.0))
        signal._ai_trailing_stop_pct = float(analysis.get("trailing_stop_pct", 0))
        signal._ai_risk_level = analysis.get("risk_level", "MEDIUM")
        signal._ai_grade = grade

        signal.reasons.append(f"Lev:{signal._ai_leverage}x SL:{signal._ai_stop_loss_pct}% TP:{signal._ai_take_profit_pct}%")
        signal.reasons.append(f"Size:{signal._ai_position_size_pct*100:.0f}% Grade:{grade}")

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

        decision = analysis.get("decision", "WAIT").upper().strip()
        iv_codes = analysis.get("invalidation_codes", [])
        rc_codes = analysis.get("reason_codes", [])
        reason_str = ",".join(iv_codes or rc_codes or ["AI"])

        # If AI explicitly says CLOSE — respect it immediately
        if decision == "CLOSE":
            return True, f"AI close: {reason_str}"

        # If AI says opposite direction, close
        if position.side == Side.LONG and decision == "SHORT":
            return True, f"AI reversal->SHORT: {reason_str}"
        if position.side == Side.SHORT and decision == "LONG":
            return True, f"AI reversal->LONG: {reason_str}"

        # WAIT = hold current position, let SL/TP/trailing handle it
        return False, ""

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
