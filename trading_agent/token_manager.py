"""Token optimization engine — pre-filtering, license caching, budget tracking.

Deterministic gates run BEFORE LLM to eliminate 80-90% of token spend.
Directional License cached with intelligent invalidation.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from trading_agent import config
from trading_agent.indicators import (
    adx, atr, atr_percent, bollinger_band_width, choppiness_index,
    classify_regime, ema, ema_slope, extension_from_ema, rsi,
    macd_histogram, volume_ratio,
)
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, Regime, RegimeState,
)
from trading_agent.risk_manager import check_kill_switches, check_cooldowns, CooldownState
from trading_agent.strategy import check_session_filter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pre-filter result
# ---------------------------------------------------------------------------

@dataclass
class PreFilterResult:
    """Result of deterministic pre-filtering before LLM call."""
    passed: bool
    reasons: list[str] = field(default_factory=list)
    regime: Optional[RegimeState] = None
    llm_call_needed: bool = True

    def block(self, reason: str) -> None:
        self.passed = False
        self.llm_call_needed = False
        self.reasons.append(reason)


# ---------------------------------------------------------------------------
# License cache
# ---------------------------------------------------------------------------

@dataclass
class CachedLicense:
    """Cached directional license with invalidation metadata."""
    license: DirectionalLicense
    cached_at: float
    cached_price: float
    cached_atr: float
    cached_regime: Regime
    hits: int = 0

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.cached_at) > config.LICENSE_CACHE_TTL_SEC

    def should_invalidate(self, current_price: float, current_atr: float,
                          current_regime: Regime, volume_ratio: float) -> tuple[bool, str]:
        """Check if cache should be invalidated early."""
        if self.is_expired:
            return True, "TTL expired"
        if current_regime != self.cached_regime:
            return True, f"Regime changed: {self.cached_regime.value} → {current_regime.value}"
        if current_atr > 0:
            price_move = abs(current_price - self.cached_price) / current_atr
            if price_move > config.LICENSE_CACHE_INVALIDATE_ATR_MOVE:
                return True, f"Price moved {price_move:.1f} ATR from cache"
        if volume_ratio > config.LICENSE_CACHE_INVALIDATE_VOL_SPIKE:
            return True, f"Volume spike {volume_ratio:.1f}x (threshold {config.LICENSE_CACHE_INVALIDATE_VOL_SPIKE}x)"
        return False, ""


# ---------------------------------------------------------------------------
# Token budget tracker
# ---------------------------------------------------------------------------

class TokenBudgetTracker:
    """Track daily token usage and enforce budget."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or config.DB_PATH
        self._daily_total: int = 0
        self._daily_date: str = ""
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                call_type TEXT NOT NULL,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cached INTEGER DEFAULT 0,
                model TEXT,
                cost_usd REAL DEFAULT 0
            )
        """)
        conn.commit()

    def _check_date_reset(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_total = 0

    def record_usage(self, call_type: str, input_tokens: int, output_tokens: int,
                     cached: bool = False, model: str = "") -> None:
        """Record token usage for a call."""
        total = input_tokens + output_tokens
        cost = total * 0.000003  # Approximate cost per token

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO token_usage (timestamp_utc, call_type, input_tokens,
               output_tokens, total_tokens, cached, model, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             call_type, input_tokens, output_tokens, total, int(cached), model, cost),
        )
        conn.commit()

        self._check_date_reset()
        self._daily_total += total

    @property
    def daily_total(self) -> int:
        self._check_date_reset()
        return self._daily_total

    @property
    def budget_pct(self) -> float:
        if config.DAILY_TOKEN_BUDGET <= 0:
            return 0
        return self.daily_total / config.DAILY_TOKEN_BUDGET * 100

    @property
    def budget_alert(self) -> bool:
        return self.budget_pct >= config.TOKEN_BUDGET_ALERT_PCT

    @property
    def budget_exceeded(self) -> bool:
        return self.daily_total >= config.DAILY_TOKEN_BUDGET

    @property
    def should_use_deterministic_only(self) -> bool:
        return self.budget_exceeded and config.TOKEN_BUDGET_HARD_STOP

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ---------------------------------------------------------------------------
# Compressed prompt builder
# ---------------------------------------------------------------------------

def build_compressed_prompt(
    candles_1m: Sequence[CandleData],
    candles_5m: Sequence[CandleData] | None,
    candles_15m: Sequence[CandleData] | None,
    candles_1h: Sequence[CandleData] | None,
    regime: RegimeState,
    spread: float = 0,
    funding_rate: float = 0,
    oi_change_pct: float = 0,
    last_trade_info: str = "",
) -> str:
    """Build a compressed prompt with pre-computed indicators, no raw candles."""
    if not candles_1m:
        return "No data available"

    price = candles_1m[-1].close
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    closes = [c.close for c in candles_1m]

    # 1m indicators
    ema9_val = ema(closes, 9)[-1] if len(closes) >= 9 else 0
    ema21_val = ema(closes, 21)[-1] if len(closes) >= 21 else 0
    slope_1m = ema_slope(candles_1m) * 100 if len(candles_1m) >= 24 else 0
    rsi_val = rsi(candles_1m) if len(candles_1m) >= 15 else 50
    adx_val = adx(candles_1m) if len(candles_1m) >= 15 else 0
    chop_val = choppiness_index(candles_1m) if len(candles_1m) >= 14 else 50
    atr_vals = atr(candles_1m)
    atr_val = atr_vals[-1] if atr_vals else 0
    atr_pct = atr_percent(candles_1m)
    vol_r = volume_ratio(candles_1m)
    ext = extension_from_ema(candles_1m)

    parts = [
        f"BTCUSDT | ${price:.0f} | {ts}",
        f"1m: EMA9={ema9_val:.0f} EMA21={ema21_val:.0f} slope={slope_1m:+.2f}% RSI={rsi_val:.0f} ADX={adx_val:.0f} CHOP={chop_val:.0f} ATR={atr_val:.1f}({atr_pct:.3f}%) vol={vol_r:.1f}x",
    ]

    # 15m
    if candles_15m and len(candles_15m) >= 21:
        c15 = [c.close for c in candles_15m]
        from trading_agent.indicators import trend_direction
        parts.append(
            f"15m: EMA9={ema(c15,9)[-1]:.0f} EMA21={ema(c15,21)[-1]:.0f} "
            f"slope={ema_slope(candles_15m)*100:+.2f}% RSI={rsi(candles_15m):.0f} "
            f"trend={trend_direction(candles_15m)}"
        )

    # 1h
    if candles_1h and len(candles_1h) >= 21:
        c1h = [c.close for c in candles_1h]
        from trading_agent.indicators import trend_direction
        parts.append(
            f"1h: EMA9={ema(c1h,9)[-1]:.0f} EMA21={ema(c1h,21)[-1]:.0f} "
            f"slope={ema_slope(candles_1h)*100:+.2f}% RSI={rsi(candles_1h):.0f} "
            f"trend={trend_direction(candles_1h)}"
        )

    parts.append(
        f"Regime: {regime.regime.value} | Spread: ${spread:.2f} | "
        f"Ext: {ext:.1f}ATR | OI: {oi_change_pct:+.1f}% | Funding: {funding_rate:+.4f}"
    )

    if last_trade_info:
        parts.append(last_trade_info)

    parts.append('Respond JSON only: {action, confidence, setup_type, entry_quality, htf_alignment, reason}')

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Deterministic pre-filter
# ---------------------------------------------------------------------------

def run_pre_filters(
    candles_1m: Sequence[CandleData],
    spread: float = 0,
    funding_rate: float = 0,
    latency_ms: float = 0,
    cooldown: CooldownState | None = None,
    candle_index: int = 0,
    daily_pnl: float = 0,
    weekly_pnl: float = 0,
    equity: float = 10000,
    drawdown_pct: float = 0,
) -> PreFilterResult:
    """
    Run deterministic pre-filters BEFORE LLM call.
    Gates ordered cheapest first. If ANY fails → no LLM call.
    """
    result = PreFilterResult(passed=True)

    # Gate 1: Kill switches
    kill = check_kill_switches(spread=spread, funding_rate=funding_rate,
                               latency_ms=latency_ms)
    if kill.any_active:
        result.block(f"Kill switch: {'; '.join(kill.reasons())}")
        return result

    # Gate 2: Session filter
    session_ok, session_reason = check_session_filter()
    if not session_ok:
        result.block(f"Session: {session_reason}")
        return result

    # Gate 3: Cooldown
    if cooldown:
        from trading_agent.risk_manager import check_cooldowns
        cd_ok, cd_reason = check_cooldowns(cooldown, "LONG", candle_index)
        cd_ok2, cd_reason2 = check_cooldowns(cooldown, "SHORT", candle_index)
        if not cd_ok and not cd_ok2:
            result.block(f"Cooldown: both sides blocked")
            return result

    # Gate 4: Capital protection
    from trading_agent.position_sizer import check_daily_loss_limit, check_weekly_loss_limit, check_equity_floor
    floor_ok, floor_reason = check_equity_floor(equity)
    if not floor_ok:
        result.block(f"Capital: {floor_reason}")
        return result
    daily_ok, daily_reason = check_daily_loss_limit(daily_pnl, equity)
    if not daily_ok:
        result.block(f"Capital: {daily_reason}")
        return result
    weekly_ok, weekly_reason = check_weekly_loss_limit(weekly_pnl, equity)
    if not weekly_ok:
        result.block(f"Capital: {weekly_reason}")
        return result

    # Gate 5: Regime filter
    if candles_1m and len(candles_1m) >= 21:
        regime = classify_regime(candles_1m)
        result.regime = regime
        if config.REGIME_FILTER_ENABLED and regime.regime in (Regime.RANGING, Regime.DEAD_LOW_VOL):
            result.block(f"Regime: {regime.regime.value} — {regime.details}")
            return result
    else:
        result.block("Insufficient candle data")
        return result

    # Gate 6: Extension filter
    ext = extension_from_ema(candles_1m)
    if ext > config.MAX_ENTRY_EXTENSION_ATR * 1.5:  # Only extreme pre-filter
        result.block(f"Extreme extension: {ext:.1f} ATR (pre-filter threshold {config.MAX_ENTRY_EXTENSION_ATR * 1.5:.1f})")
        return result

    # Gate 7: Volume
    vol = volume_ratio(candles_1m)
    if vol < config.MIN_VOLUME_RATIO * 0.7:  # Only very weak volume pre-filter
        result.block(f"Very weak volume: {vol:.2f}x (pre-filter threshold {config.MIN_VOLUME_RATIO * 0.7:.2f})")
        return result

    # All gates passed → LLM call needed
    result.llm_call_needed = True
    return result
