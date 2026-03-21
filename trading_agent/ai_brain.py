"""
Claude AI Brain — Selective Execution Mode.
Issues Directional Licenses (strategic context) instead of execution timing.
WAIT is the default. Trading is the exception.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx

from .models import (
    Candle, DirectionalLicense, Indicators, MarketContext,
    Side, Signal, SignalStrength, Position
)

logger = logging.getLogger("ai_brain")

# Selective system prompt — WAIT is default, trading requires convergence
SYSTEM_PROMPT = """You are a Directional License engine for an institutional BTC scalper.
Your job: evaluate higher-timeframe context and regime to PERMIT or DENY trading in a direction.
You do NOT decide exact entry timing — deterministic code handles that.

RULES:
- WAIT is the DEFAULT and CORRECT output for unclear conditions.
- Trading requires ALL of: clear regime + HTF alignment + momentum + no overextension.
- Mixed signals → WAIT. Chop → WAIT. Extended price → WAIT. Late entry → WAIT.
- Reversal setups require STRICTLY HIGHER evidence: divergence + exhaustion + structure break + rejection.
- Only A and A+ setups are tradeable. B setups are rare exceptions in perfect regime. C/D → WAIT.
- You are REWARDED for saying WAIT when conditions are unclear. Bad trades destroy capital.

RESPOND WITH ONLY RAW JSON:
{"a":"L|S|W","c":0-100,"regime":"TRENDING|RANGING|DEAD_LOW_VOL|SPIKE_HIGH_VOL","setup":"CONTINUATION|PULLBACK|BREAKOUT_RETEST|REVERSAL|NONE","eq":0-100,"htf":"ALIGNED|NEUTRAL|OPPOSING","reason":"concise","rc":["HTF+","MOM+","VOL+","TREND"],"iv":[""]}

rc/iv codes: HTF+,HTF-,MOM+,MOM-,VOL+,VOL-,CHOP,TREND,BOS+,BOS-,RR+,RR-,REV,SQZ,FG+,FG-,OI+,OI-,CVD+,CVD-,EXT"""


class ClaudeAIBrain:
    """Uses Claude API for Directional License decisions."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.client = httpx.AsyncClient(timeout=30.0)
        self.api_url = "https://api.anthropic.com/v1/messages"
        self.last_analysis: Optional[dict] = None
        self.analysis_count = 0
        self.enabled = bool(api_key)

        # Directional License cache
        self._cached_license: Optional[DirectionalLicense] = None
        self._cache_timestamp: float = 0
        self._cache_price: float = 0
        self._cache_ttl: int = 180  # seconds

        # Token tracking
        self.daily_tokens_used: int = 0
        self.daily_token_reset: str = ""
        self.token_calls: list = []

        if not self.enabled:
            logger.warning("Claude AI Brain DISABLED - no ANTHROPIC_API_KEY")
        else:
            logger.info(f"Claude AI Brain enabled (model: {model}, selective mode)")

    def set_cache_ttl(self, ttl: int):
        self._cache_ttl = ttl

    def get_cached_license(
        self, current_price: float, atr_value: float, volume_ratio: float,
        invalidate_atr_move: float = 1.0, invalidate_vol_spike: float = 3.0,
    ) -> Optional[DirectionalLicense]:
        """Return cached Directional License if still valid."""
        if not self._cached_license:
            return None

        elapsed = time.time() - self._cache_timestamp
        if elapsed > self._cache_ttl:
            return None

        # Invalidate if price moved more than N ATR from license price
        if atr_value > 0 and self._cache_price > 0:
            price_move_atr = abs(current_price - self._cache_price) / atr_value
            if price_move_atr > invalidate_atr_move:
                logger.info(f"License cache invalidated: price moved {price_move_atr:.1f} ATR")
                return None

        # Invalidate on volume spike
        if volume_ratio > invalidate_vol_spike:
            logger.info(f"License cache invalidated: volume spike {volume_ratio:.1f}x")
            return None

        remaining = self._cache_ttl - elapsed
        logger.info(f"License CACHED: {self._cached_license.direction} conf={self._cached_license.confidence} ({remaining:.0f}s left)")
        return self._cached_license

    async def get_directional_license(
        self,
        candles: list[Candle],
        indicators: Indicators,
        market_context: Optional[MarketContext],
        recent_trades: list = None,
        balance: float = 0,
        max_tokens: int = 200,
    ) -> Optional[DirectionalLicense]:
        """Issue a Directional License — strategic direction permission.
        This replaces the old analyze() for entry decisions."""
        if not self.enabled:
            return None

        # Check token budget
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_token_reset != today:
            self.daily_tokens_used = 0
            self.daily_token_reset = today

        try:
            prompt = self._build_compressed_prompt(
                candles, indicators, market_context, recent_trades, balance
            )

            start_time = time.time()
            resp = await self.client.post(
                self.api_url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            api_time = int((time.time() - start_time) * 1000)

            if resp.status_code != 200:
                logger.error(f"Claude API error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            content = data.get("content", [{}])[0].get("text", "")

            # Track tokens
            usage = data.get("usage", {})
            tokens_used = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            self.daily_tokens_used += tokens_used
            self.token_calls.append({
                "timestamp": datetime.now().isoformat(),
                "call_type": "directional_license",
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": tokens_used,
                "api_latency_ms": api_time,
            })

            # Parse JSON
            raw = self._parse_json_response(content)
            if not raw:
                return None

            self.last_analysis = raw
            self.analysis_count += 1

            # Build DirectionalLicense
            action_map = {"L": "LONG", "S": "SHORT", "W": "WAIT"}
            action_raw = str(raw.get("a", "W")).upper().strip()
            decision = action_map.get(action_raw, action_raw)

            license = DirectionalLicense(
                confidence=int(float(raw.get("c", 0))),
                regime=str(raw.get("regime", "UNKNOWN")),
                setup_type=str(raw.get("setup", "NONE")),
                entry_quality=int(float(raw.get("eq", 0))),
                htf_alignment=str(raw.get("htf", "NEUTRAL")),
                reason=str(raw.get("reason", "")),
                timestamp=datetime.now(),
                valid_until=datetime.now() + timedelta(seconds=self._cache_ttl),
                price_at_issue=candles[-1].close if candles else 0,
            )

            if decision == "LONG":
                license.direction = Side.LONG
            elif decision == "SHORT":
                license.direction = Side.SHORT
            else:
                license.direction = None

            # Cache the license
            self._cached_license = license
            self._cache_timestamp = time.time()
            self._cache_price = candles[-1].close if candles else 0

            logger.info(
                f"Claude AI: {decision} | Conf:{license.confidence}% | "
                f"Regime:{license.regime} | Setup:{license.setup_type} | "
                f"Quality:{license.entry_quality} | HTF:{license.htf_alignment} | "
                f"RC:{raw.get('rc', [])} | {api_time}ms"
            )

            return license

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response: {e}")
            return None
        except Exception as e:
            logger.error(f"Claude AI license failed: {e}")
            return None

    # Legacy analyze() kept for backward compatibility with close decisions
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
        """Legacy analyze — used for close decisions."""
        if not self.enabled:
            return None

        try:
            prompt = self._build_compressed_prompt(
                candles, indicators, market_context, recent_trades, balance, position
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
                    "max_tokens": 200,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

            if resp.status_code != 200:
                logger.error(f"Claude API error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            content = data.get("content", [{}])[0].get("text", "")
            raw = self._parse_json_response(content)
            if not raw:
                return None

            self.last_analysis = raw
            self.analysis_count += 1

            action_map = {"L": "LONG", "S": "SHORT", "W": "WAIT", "C": "CLOSE"}
            action_raw = str(raw.get("a", "W")).upper().strip()
            decision = action_map.get(action_raw, action_raw)

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
                "grade": str(raw.get("g", raw.get("setup", "B"))),
                "rr": float(raw.get("rr", 0)),
                "trailing_stop_pct": float(raw.get("ts", 0)),
                "reason_codes": raw.get("rc", []),
                "invalidation_codes": raw.get("iv", []),
                "reasoning": str(raw.get("reason", ", ".join(raw.get("rc", [])))),
                "key_factors": raw.get("rc", []),
                "risk_level": "HIGH" if decision in ("LONG", "SHORT") else "LOW",
                "regime": str(raw.get("regime", "UNKNOWN")),
                "setup_type": str(raw.get("setup", "NONE")),
                "entry_quality": int(float(raw.get("eq", 0))),
                "htf_alignment": str(raw.get("htf", "NEUTRAL")),
            }

            return analysis

        except Exception as e:
            logger.error(f"Claude AI analysis failed: {e}")
            return None

    def _build_compressed_prompt(
        self,
        candles: list[Candle],
        indicators: Indicators,
        market_context: Optional[MarketContext] = None,
        recent_trades: list = None,
        balance: float = 0,
        position: Optional[Position] = None,
    ) -> str:
        """Build ultra-compressed prompt — pre-computed values only, no raw candles."""
        price = candles[-1].close if candles else 0

        # Last 5 moves as %
        last5 = candles[-5:]
        moves = []
        for i in range(1, len(last5)):
            moves.append(round(((last5[i].close - last5[i - 1].close) / last5[i - 1].close) * 100, 3))

        high_h = max(c.high for c in candles[-60:]) if len(candles) >= 60 else max(c.high for c in candles)
        low_h = min(c.low for c in candles[-60:]) if len(candles) >= 60 else min(c.low for c in candles)

        ema_cross = "BULL" if indicators.ema_fast > indicators.ema_slow else "BEAR"

        prompt = f"BTCUSDT ${price:.0f} | {datetime.utcnow().strftime('%H:%M')}Z\n"
        prompt += (
            f"1m: RSI:{indicators.rsi:.0f} EMA:{ema_cross} "
            f"MACD_H:{indicators.macd_histogram:.4f} "
            f"ATR:{indicators.atr_pct:.2f}% VOL:{indicators.volume_ratio:.1f}x "
            f"ADX:{indicators.adx:.1f} CHOP:{indicators.chop_index:.1f} "
            f"Ext:{indicators.extension_atr:.1f}ATR "
            f"CVD:{indicators.cvd_value:+.0f}\n"
        )
        prompt += f"Range:{low_h:.0f}-{high_h:.0f} Moves:{moves}\n"

        if market_context:
            prompt += (
                f"5m:{market_context.trend_5m} 15m:{market_context.trend_15m} "
                f"1h:{market_context.trend_1h} "
                f"OI:{market_context.open_interest_change:+.1f}% "
                f"BOOK:{market_context.book_imbalance:+.0f}% "
                f"FG:{market_context.fear_greed_index} "
                f"Fund:{market_context.funding_rate:.5f}\n"
            )

        if position:
            if position.side == Side.LONG:
                pnl_pct = ((price - position.entry_price) / position.entry_price) * 100
            else:
                pnl_pct = ((position.entry_price - price) / position.entry_price) * 100
            prompt += (
                f"POS:{position.side.value} @{position.entry_price:.2f} "
                f"{position.leverage}x PnL:{pnl_pct:.3f}% "
                f"SL:{position.stop_loss:.2f} TP:{position.take_profit:.2f}\n"
            )

        if recent_trades:
            last3 = recent_trades[-3:]
            t_info = " ".join([f"{t.side.value[0]}:{t.pnl:+.2f}" for t in last3])
            wins = sum(1 for t in recent_trades if t.pnl > 0)
            prompt += f"Trades:{t_info} W:{wins}/{len(recent_trades)}\n"

        prompt += f"${balance:.0f} JSON:"
        return prompt

    def _parse_json_response(self, content: str) -> Optional[dict]:
        """Parse JSON from Claude response, handling markdown blocks."""
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # Extract first JSON object
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

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.error(f"JSON parse failed: {text[:100]}")
            return None

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
        signal.trade_source = "AI"

        # Store AI parameters
        signal._ai_leverage = int(analysis.get("leverage", 5))
        signal._ai_position_size_pct = float(analysis.get("position_size_pct", 10)) / 100.0
        signal._ai_stop_loss_pct = float(analysis.get("stop_loss_pct", 0.5))
        signal._ai_take_profit_pct = float(analysis.get("take_profit_pct", 1.0))
        signal._ai_trailing_stop_pct = float(analysis.get("trailing_stop_pct", 0))
        signal._ai_risk_level = analysis.get("risk_level", "MEDIUM")
        signal._ai_grade = grade

        # Selective execution fields
        signal.regime = analysis.get("regime", "UNKNOWN")
        signal.entry_quality = analysis.get("entry_quality", 0)
        signal.setup_type = analysis.get("setup_type", "NONE")
        signal.htf_alignment = analysis.get("htf_alignment", "NEUTRAL")

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

        if decision == "CLOSE":
            return True, f"AI close: {reason_str}"
        if position.side == Side.LONG and decision == "SHORT":
            return True, f"AI reversal->SHORT: {reason_str}"
        if position.side == Side.SHORT and decision == "LONG":
            return True, f"AI reversal->LONG: {reason_str}"

        return False, ""

    async def generate_journal_entry(
        self,
        context: str,
        max_tokens: int = 150,
    ) -> Optional[dict]:
        """Generate a learning journal entry via cheap LLM call."""
        if not self.enabled:
            return None

        try:
            resp = await self.client.post(
                self.api_url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "system": "You analyze trading outcomes. Respond JSON only: {\"observation\":\"...\",\"conclusion\":\"...\",\"suggested_action\":\"...\",\"confidence_in_conclusion\":0-100}. Max 3 sentences total.",
                    "messages": [{"role": "user", "content": context}],
                },
            )

            if resp.status_code != 200:
                return None

            data = resp.json()
            content = data.get("content", [{}])[0].get("text", "")

            # Track tokens
            usage = data.get("usage", {})
            tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            self.daily_tokens_used += tokens

            return self._parse_json_response(content)

        except Exception as e:
            logger.error(f"Journal entry generation failed: {e}")
            return None

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
