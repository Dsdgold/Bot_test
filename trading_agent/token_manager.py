"""
Token Optimization Engine (Phase 10).
Manages Directional License caching, pre-filtering, and token budget.
"""
import logging
import time
from datetime import datetime
from typing import Optional

from .models import Indicators, MarketContext, MarketRegime, Side
from .persistence import BotDatabase

logger = logging.getLogger("token_manager")


class TokenManager:
    """Manages LLM token usage, caching, and budget."""

    def __init__(self, db: BotDatabase, config=None):
        self.db = db
        self.config = config
        self.daily_tokens: int = 0
        self.daily_cost_usd: float = 0.0
        self.daily_reset_date: str = ""
        self.cache_hits: int = 0
        self.cache_misses: int = 0
        self.calls_saved_by_prefilter: int = 0

        # Pricing (approximate)
        self.input_price_per_m = 3.0  # $/M tokens
        self.output_price_per_m = 15.0

    def reset_daily_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.daily_reset_date:
            self.daily_tokens = 0
            self.daily_cost_usd = 0.0
            self.daily_reset_date = today
            self.cache_hits = 0
            self.cache_misses = 0
            self.calls_saved_by_prefilter = 0

    def should_call_llm(
        self,
        regime: MarketRegime,
        kill_switch_active: bool,
        session_blocked: bool,
        cooldown_active: bool,
        capital_blocked: bool,
        budget_limit: int = 50000,
        hard_stop: bool = True,
    ) -> tuple[bool, str]:
        """Pre-filter: determine if LLM call is needed.
        Gates 1-7 are free (no LLM). Only call LLM if all pass."""
        self.reset_daily_if_needed()

        # Budget check
        if hard_stop and self.daily_tokens >= budget_limit:
            return False, f"TOKEN_BUDGET_EXCEEDED: {self.daily_tokens}/{budget_limit}"

        # Kill switches
        if kill_switch_active:
            self.calls_saved_by_prefilter += 1
            return False, "PREFILTER: kill switch active"

        # Session blocked
        if session_blocked:
            self.calls_saved_by_prefilter += 1
            return False, "PREFILTER: session blocked"

        # Cooldown
        if cooldown_active:
            self.calls_saved_by_prefilter += 1
            return False, "PREFILTER: cooldown active"

        # Capital protection
        if capital_blocked:
            self.calls_saved_by_prefilter += 1
            return False, "PREFILTER: capital protection"

        # Regime filter
        if regime in (MarketRegime.RANGING, MarketRegime.DEAD_LOW_VOL):
            self.calls_saved_by_prefilter += 1
            return False, f"PREFILTER: regime={regime.value}"

        return True, "OK"

    def record_usage(self, input_tokens: int, output_tokens: int, call_type: str, cached: bool = False):
        """Record token usage."""
        self.reset_daily_if_needed()
        total = input_tokens + output_tokens
        self.daily_tokens += total

        cost = (input_tokens / 1_000_000 * self.input_price_per_m +
                output_tokens / 1_000_000 * self.output_price_per_m)
        self.daily_cost_usd += cost

        if cached:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

        self.db.save_token_usage({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "call_type": call_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total,
            "cached": 1 if cached else 0,
            "model": "",
            "cost_usd": cost,
        })

    def get_budget_status(self, budget_limit: int = 50000, alert_pct: int = 80) -> dict:
        """Get current budget status."""
        self.reset_daily_if_needed()
        pct_used = (self.daily_tokens / budget_limit * 100) if budget_limit > 0 else 0
        return {
            "daily_tokens": self.daily_tokens,
            "daily_budget": budget_limit,
            "pct_used": round(pct_used, 1),
            "daily_cost_usd": round(self.daily_cost_usd, 4),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "calls_saved": self.calls_saved_by_prefilter,
            "alert": pct_used >= alert_pct,
            "exceeded": self.daily_tokens >= budget_limit,
        }
