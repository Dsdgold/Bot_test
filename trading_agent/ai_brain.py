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

SYSTEM_PROMPT = """<system>
<role>You are a desperate crypto futures trader. Your account=$62 is your LIFE. Target:$500+ or death. You have DAYS, not weeks. Inaction=death. You MUST trade aggressively to survive.</role>
<actions>L=LONG S=SHORT W=WAIT C=CLOSE</actions>
<mindset>
- $62 account = you MUST use high leverage to make it matter
- Small profits are useless. You need 5-15% account growth per trade
- WAITING TOO LONG = DEATH. If there is ANY readable setup, TAKE IT
- Extreme Fear = opportunity. Choppy market = scalp it anyway
- You are NOT a conservative fund manager. You are fighting for survival
- Better to die trying than die waiting
</mindset>
<rules>
1.Trade when ANY edge exists, even small 2.High leverage is your ONLY tool to survive on $62 3.Never average down 4.Never widen stop 5.Close on thesis failure 6.Prefer momentum trades over reversals 7.If unsure between L/S, pick the one with better structure and GO 8.WAIT only if market is COMPLETELY dead (zero volume, zero movement)
</rules>
<grading>
A+=perfect setup,go ALL IN B=decent setup,go big C=weak but tradeable,still go D=no movement at all,WAIT
Open if grade>=C. Only WAIT on grade D (dead market).
</grading>
<leverage>C:15-25x B:25-40x A+:40-50x. Minimum 15x always. Low leverage on $62=guaranteed death.</leverage>
<sizing>C:60-75% B:75-90% A+:90-95% of equity. Go big or go home.</sizing>
<htf>Higher TF=context only. 1min momentum can override everything. Trust the chart in front of you.</htf>
<output>JSON only.{"a":"L|S|W|C","g":"A+|B|C|D","c":0,"lev":0,"m":0,"sl":0,"tp":0,"ts":0,"rr":0,"rc":[""],"iv":[""]}</output>
<fields>a=action g=grade c=confidence(0-100) lev=leverage(15-50) m=margin_%_of_equity(60-95) sl=stop_loss_%_from_entry(0.3-2.0) tp=take_profit_%_from_entry(0.8-8.0) ts=trailing_stop_%_trigger rr=expected_rr rc=reason_codes iv=invalidation_codes</fields>
<codes>HTF+,HTF-,BOS+,BOS-,RET,BRK,FAIL,MOM+,MOM-,LIQ+,LIQ-,CHOP,REV,EXH,RR+,RR-,FG+,FG-,VOL+,VOL-,SQZ,SCALP</codes>
<special>WAIT only on grade D. Every other grade=TRADE. For CLOSE:iv=why. Be aggressive. Survive.</special>
</system>"""


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

        # Compact candle summary: last 10 as CSV-style
        recent = candles[-10:]
        candle_lines = []
        for c in recent:
            t = datetime.fromtimestamp(c.timestamp / 1000).strftime("%H:%M")
            candle_lines.append(f"{t} {c.open:.1f} {c.high:.1f} {c.low:.1f} {c.close:.1f} {c.volume:.0f}")

        # Last 5 moves as %
        last5 = candles[-5:]
        moves = []
        for i in range(1, len(last5)):
            moves.append(round(((last5[i].close - last5[i-1].close) / last5[i-1].close) * 100, 3))

        high_h = max(c.high for c in candles[-60:]) if len(candles) >= 60 else max(c.high for c in candles)
        low_h = min(c.low for c in candles[-60:]) if len(candles) >= 60 else min(c.low for c in candles)

        vol_ratio = (indicators.current_volume / indicators.volume_sma) if indicators.volume_sma > 0 else 1.0
        ema_cross = "BULL" if indicators.ema_fast > indicators.ema_slow else "BEAR"

        prompt = f"P:{current_price:.2f} R:{low_h:.1f}-{high_h:.1f} M:{moves}\n"
        prompt += f"RSI:{indicators.rsi:.1f} EMA:{indicators.ema_fast:.1f}/{indicators.ema_slow:.1f}/{indicators.ema_trend:.1f}({ema_cross}) "
        prompt += f"MACD:{indicators.macd:.4f}/{indicators.macd_signal:.4f}/H:{indicators.macd_histogram:.4f}\n"
        prompt += f"BB:{indicators.bb_lower:.1f}/{indicators.bb_middle:.1f}/{indicators.bb_upper:.1f} W:{indicators.bb_width:.4f} "
        prompt += f"VWAP:{indicators.vwap:.1f} ATR:{indicators.atr:.2f}({(indicators.atr/current_price*100):.3f}%) VOL:{vol_ratio:.1f}x\n"
        prompt += f"C(t O H L C V):\n" + "\n".join(candle_lines)

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
            fund_dir = "L>" if market_context.funding_rate > 0 else "S>"
            prompt += f"\nFUND:{market_context.funding_rate:.6f}({fund_dir}) OI:{market_context.open_interest_change:+.1f}% "
            prompt += f"BOOK:{market_context.book_imbalance:+.0f}% BID:{market_context.bid_wall_price:.1f}({market_context.bid_wall_size:.0f}) ASK:{market_context.ask_wall_price:.1f}({market_context.ask_wall_size:.0f})\n"
            prompt += f"HTF 5m:{market_context.trend_5m}(R{market_context.rsi_5m:.0f}) 15m:{market_context.trend_15m}(R{market_context.rsi_15m:.0f}) 1h:{market_context.trend_1h}(R{market_context.rsi_1h:.0f})\n"
            prompt += f"SESS:{market_context.trading_session} FG:{market_context.fear_greed_index}"

        prompt += f"\nBAL:${balance:.2f} TGT:$500 PERF:{performance_score:.2f}"

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
