"""
Trading Agent Configuration — Selective Execution Mode
All parameters loaded from environment / .env file.
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()


def _bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).lower() == "true"


def _float(key: str, default: str = "0") -> float:
    return float(os.getenv(key, default))


def _int(key: str, default: str = "0") -> int:
    return int(float(os.getenv(key, default)))


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _list_int(key: str, default: str = "") -> List[int]:
    raw = os.getenv(key, default).strip()
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


@dataclass
class BybitConfig:
    """Bybit API configuration."""
    api_key: str = _str("BYBIT_API_KEY")
    api_secret: str = _str("BYBIT_API_SECRET")
    base_url: str = os.getenv(
        "BYBIT_BASE_URL",
        "https://api-testnet.bybit.com" if _bool("BYBIT_TESTNET") else "https://api.bybit.com"
    )
    testnet: bool = _bool("BYBIT_TESTNET")


@dataclass
class TradingConfig:
    """Trading parameters — selective execution mode."""
    # Symbol
    symbol: str = _str("TRADING_SYMBOL", "BTCUSDT")

    # Leverage (capped by MAX_LEVERAGE)
    leverage: int = _int("TRADING_LEVERAGE", "20")
    max_leverage: int = _int("MAX_LEVERAGE", "10")

    # Position sizing
    max_position_pct: float = _float("MAX_POSITION_PCT", "0.90")
    min_order_usdt: float = _float("MIN_ORDER_USDT", "1.0")

    # Risk management defaults (overridden by dynamic SL/TP)
    stop_loss_pct: float = _float("STOP_LOSS_PCT", "0.8")
    take_profit_pct: float = _float("TAKE_PROFIT_PCT", "4.0")
    trailing_stop_pct: float = _float("TRAILING_STOP_PCT", "1.0")
    max_daily_loss_pct: float = _float("DAILY_MAX_LOSS_PCT", "3.0")
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", "1")

    # Timing
    candle_interval: str = "Min1"
    analysis_interval: int = _int("ANALYSIS_INTERVAL", "60")
    cooldown_after_trade: int = _int("COOLDOWN_AFTER_TRADE", "120")

    # Strategy indicator params
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    rsi_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    bb_period: int = 20
    bb_std: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_spike_multiplier: float = 1.5

    # Confidence threshold
    min_confidence: float = _float("MIN_CONFIDENCE", "60")

    # Minimum hold time
    min_hold_time: int = _int("MIN_HOLD_TIME", "120")

    # Progressive profit locking
    progressive_stop_enabled: bool = _bool("PROGRESSIVE_STOP_ENABLED", "true")
    profit_step_net_usd: float = _float("PROFIT_STEP_NET_USD", "2.0")
    lock_step_net_usd: float = _float("LOCK_STEP_NET_USD", "0.75")
    taker_fee_rate: float = _float("TAKER_FEE_RATE", "0.00055")
    slippage_buffer_usd: float = _float("SLIPPAGE_BUFFER_USD", "0.40")
    min_stop_improvement_usd: float = _float("MIN_STOP_IMPROVEMENT_USD", "0.25")
    disable_legacy_profit_protection: bool = _bool("DISABLE_LEGACY_PROFIT_PROTECTION", "true")

    # ═══════════════════════════════════════
    # SELECTIVE EXECUTION MODE
    # ═══════════════════════════════════════
    strict_wait_mode: bool = _bool("STRICT_WAIT_MODE", "true")
    enable_fallback_override: bool = _bool("ENABLE_FALLBACK_OVERRIDE", "false")

    # Regime & chop filters
    regime_filter_enabled: bool = _bool("REGIME_FILTER_ENABLED", "true")
    use_adx_filter: bool = _bool("USE_ADX_FILTER", "true")
    adx_min: float = _float("ADX_MIN", "18")
    use_chop_filter: bool = _bool("USE_CHOP_FILTER", "true")
    chop_max: float = _float("CHOP_MAX", "61.8")
    dead_vol_atr_pct_min: float = _float("DEAD_VOL_ATR_PCT_MIN", "0.10")
    spike_candle_atr_max: float = _float("SPIKE_CANDLE_ATR_MAX", "1.5")

    # Entry quality gates
    trade_quality_min: int = _int("TRADE_QUALITY_MIN", "70")
    reversal_quality_min: int = _int("REVERSAL_QUALITY_MIN", "80")
    max_entry_extension_atr: float = _float("MAX_ENTRY_EXTENSION_ATR", "0.8")
    candle_close_confirmation: bool = _bool("CANDLE_CLOSE_CONFIRMATION", "true")
    min_volume_ratio: float = _float("MIN_VOLUME_RATIO", "1.15")

    # Microstructure
    require_oi_confirmation: bool = _bool("REQUIRE_OI_CONFIRMATION", "true")
    require_cvd_alignment: bool = _bool("REQUIRE_CVD_ALIGNMENT", "true")

    # Execution & fees
    prefer_post_only_entries: bool = _bool("PREFER_POST_ONLY_ENTRIES", "true")
    max_allowed_slippage_bps: float = _float("MAX_ALLOWED_SLIPPAGE_BPS", "5")

    # Dynamic SL/TP
    use_dynamic_sl_tp: bool = _bool("USE_DYNAMIC_SL_TP", "true")
    atr_stop_mult: float = _float("ATR_STOP_MULT", "1.0")
    target_rr_a_plus: float = _float("TARGET_RR_A_PLUS", "1.6")
    target_rr_a: float = _float("TARGET_RR_A", "1.35")
    target_rr_b: float = _float("TARGET_RR_B", "1.2")
    min_net_rr: float = _float("MIN_NET_RR", "1.15")
    min_sl_pct: float = _float("MIN_SL_PCT", "0.35")
    max_sl_pct: float = _float("MAX_SL_PCT", "0.90")
    min_tp_pct: float = _float("MIN_TP_PCT", "0.45")
    max_tp_pct: float = _float("MAX_TP_PCT", "1.50")

    # Session filter
    session_filter_enabled: bool = _bool("SESSION_FILTER_ENABLED", "true")
    timezone: str = _str("TIMEZONE", "Europe/Warsaw")
    blocked_hours_local: List[int] = field(default_factory=lambda: _list_int("BLOCKED_HOURS_LOCAL"))
    min_sample_per_hour: int = _int("MIN_SAMPLE_PER_HOUR", "10")

    # Cooldowns & kill switches
    post_loss_cooldown_sec: int = _int("POST_LOSS_COOLDOWN_SEC", "600")
    reentry_cooldown_candles: int = _int("REENTRY_COOLDOWN_CANDLES", "3")
    same_side_loss_pause_count: int = _int("SAME_SIDE_LOSS_PAUSE_COUNT", "2")
    same_side_loss_pause_sec: int = _int("SAME_SIDE_LOSS_PAUSE_SEC", "1800")
    freeze_on_extreme_funding: bool = _bool("FREEZE_ON_EXTREME_FUNDING", "true")
    max_funding_rate_abs: float = _float("MAX_FUNDING_RATE_ABS", "0.001")
    max_spread_tolerance_usdt: float = _float("MAX_SPREAD_TOLERANCE_USDT", "2.5")
    latency_kill_switch_ms: int = _int("LATENCY_KILL_SWITCH_MS", "500")

    # Capital protection
    weekly_max_loss_pct: float = _float("WEEKLY_MAX_LOSS_PCT", "7.0")
    equity_floor_usdt: float = _float("EQUITY_FLOOR_USDT", "500")

    # Position sizing
    position_sizing_mode: str = _str("POSITION_SIZING_MODE", "fixed_fractional")
    base_risk_per_trade_pct: float = _float("BASE_RISK_PER_TRADE_PCT", "1.0")
    max_risk_per_trade_pct: float = _float("MAX_RISK_PER_TRADE_PCT", "2.0")
    quality_size_mult_a_plus: float = _float("QUALITY_SIZE_MULTIPLIER_A_PLUS", "1.5")
    quality_size_mult_a: float = _float("QUALITY_SIZE_MULTIPLIER_A", "1.2")
    quality_size_mult_b: float = _float("QUALITY_SIZE_MULTIPLIER_B", "1.0")
    streak_loss_reduction_1: float = _float("STREAK_LOSS_REDUCTION_1", "0.75")
    streak_loss_reduction_2: float = _float("STREAK_LOSS_REDUCTION_2", "0.50")
    streak_loss_reduction_3: float = _float("STREAK_LOSS_REDUCTION_3", "0.25")

    # Growth tiers
    tier_1_equity: float = _float("TIER_1_EQUITY", "1000")
    tier_1_risk_pct: float = _float("TIER_1_RISK_PCT", "1.0")
    tier_2_equity: float = _float("TIER_2_EQUITY", "2500")
    tier_2_risk_pct: float = _float("TIER_2_RISK_PCT", "1.25")
    tier_3_equity: float = _float("TIER_3_EQUITY", "5000")
    tier_3_risk_pct: float = _float("TIER_3_RISK_PCT", "1.5")
    tier_4_equity: float = _float("TIER_4_EQUITY", "10000")
    tier_4_risk_pct: float = _float("TIER_4_RISK_PCT", "1.75")

    # Drawdown position scaling
    dd_tier_1_pct: float = _float("DD_TIER_1_PCT", "3.0")
    dd_tier_1_mult: float = _float("DD_TIER_1_MULT", "0.70")
    dd_tier_2_pct: float = _float("DD_TIER_2_PCT", "5.0")
    dd_tier_2_mult: float = _float("DD_TIER_2_MULT", "0.40")
    dd_tier_3_pct: float = _float("DD_TIER_3_PCT", "8.0")
    dd_tier_3_mult: float = _float("DD_TIER_3_MULT", "0.20")
    dd_halt_pct: float = _float("DD_HALT_PCT", "10.0")


@dataclass
class AIConfig:
    """Claude AI configuration."""
    api_key: str = _str("ANTHROPIC_API_KEY")
    model: str = _str("AI_MODEL", "claude-sonnet-4-20250514")
    analysis_every_n_ticks: int = _int("AI_ANALYSIS_INTERVAL", "4")
    ai_close_decisions: bool = _bool("AI_CLOSE_DECISIONS", "true")

    # Token optimization
    license_cache_ttl_sec: int = _int("LICENSE_CACHE_TTL_SEC", "180")
    license_cache_invalidate_atr_move: float = _float("LICENSE_CACHE_INVALIDATE_ATR_MOVE", "1.0")
    license_cache_invalidate_vol_spike: float = _float("LICENSE_CACHE_INVALIDATE_VOL_SPIKE", "3.0")
    llm_max_tokens_license: int = _int("LLM_MAX_TOKENS_LICENSE", "200")
    llm_compressed_prompt: bool = _bool("LLM_COMPRESSED_PROMPT", "true")
    daily_token_budget: int = _int("DAILY_TOKEN_BUDGET", "50000")
    token_budget_alert_pct: int = _int("TOKEN_BUDGET_ALERT_PCT", "80")
    token_budget_hard_stop: bool = _bool("TOKEN_BUDGET_HARD_STOP", "true")
    deterministic_fallback_on_budget: bool = _bool("DETERMINISTIC_FALLBACK_ON_BUDGET_EXCEEDED", "true")

    # Learning journal
    learning_journal_enabled: bool = _bool("LEARNING_JOURNAL_ENABLED", "true")
    journal_post_trade: bool = _bool("JOURNAL_POST_TRADE", "true")
    journal_skip_review_interval_hours: int = _int("JOURNAL_SKIP_REVIEW_INTERVAL_HOURS", "4")
    journal_on_regime_shift: bool = _bool("JOURNAL_ON_REGIME_SHIFT", "true")
    journal_on_edge_decay: bool = _bool("JOURNAL_ON_EDGE_DECAY", "true")
    journal_max_tokens_post_trade: int = _int("JOURNAL_MAX_TOKENS_POST_TRADE", "150")
    journal_max_tokens_skip_review: int = _int("JOURNAL_MAX_TOKENS_SKIP_REVIEW", "300")

    # Self-optimization
    autonomous_tuning_enabled: bool = _bool("AUTONOMOUS_TUNING_ENABLED", "true")
    tuning_cycle_hours: int = _int("TUNING_CYCLE_HOURS", "24")
    tuning_lookback_trades: int = _int("TUNING_LOOKBACK_TRADES", "200")
    tuning_min_sample: int = _int("TUNING_MIN_SAMPLE", "30")
    tuning_min_confidence_pct: int = _int("TUNING_MIN_CONFIDENCE_PCT", "70")
    probation_trades: int = _int("PROBATION_TRADES", "30")
    max_concurrent_probations: int = _int("MAX_CONCURRENT_PROBATIONS", "2")
    tuning_freeze_dd_pct: float = _float("TUNING_FREEZE_DD_PCT", "5.0")
    rollback_threshold_pct: float = _float("ROLLBACK_THRESHOLD_PCT", "15")
    rollback_max_dd_pct: float = _float("ROLLBACK_MAX_DD_PCT", "5.0")
    rollback_wr_drop_pct: float = _float("ROLLBACK_WR_DROP_PCT", "10")
    lockout_cycles: int = _int("LOCKOUT_CYCLES", "5")
    meta_learning_interval_hours: int = _int("META_LEARNING_INTERVAL_HOURS", "72")
    meta_learning_max_tokens: int = _int("META_LEARNING_MAX_TOKENS", "500")


@dataclass
class DataConfig:
    """Data collection configuration."""
    enable_trade_snapshots: bool = _bool("ENABLE_TRADE_SNAPSHOTS", "true")
    enable_market_snapshots: bool = _bool("ENABLE_MARKET_SNAPSHOTS", "true")
    market_snapshot_interval_sec: int = _int("MARKET_SNAPSHOT_INTERVAL_SEC", "300")
    enable_equity_tracking: bool = _bool("ENABLE_EQUITY_TRACKING", "true")
    equity_snapshot_interval_sec: int = _int("EQUITY_SNAPSHOT_INTERVAL_SEC", "900")
    enable_mfe_mae_tracking: bool = _bool("ENABLE_MFE_MAE_TRACKING", "true")
    db_path: str = _str("DB_PATH", "bot_data.db")

    # Auto export
    auto_export_enabled: bool = _bool("AUTO_EXPORT_ENABLED", "true")
    auto_export_interval_hours: int = _int("AUTO_EXPORT_INTERVAL_HOURS", "24")
    auto_export_format: str = _str("AUTO_EXPORT_FORMAT", "csv")
    auto_export_path: str = _str("AUTO_EXPORT_PATH", "./exports/")
    auto_export_retain_days: int = _int("AUTO_EXPORT_RETAIN_DAYS", "90")


@dataclass
class DashboardConfig:
    """Dashboard configuration."""
    enabled: bool = _bool("ENABLE_DASHBOARD", "true")
    refresh_sec: int = _int("DASHBOARD_REFRESH_SEC", "10")
    mode: str = _str("DASHBOARD_MODE", "web")
    host: str = _str("DASHBOARD_HOST", "127.0.0.1")
    port: int = _int("DASHBOARD_PORT", "8080")
    password: str = _str("DASHBOARD_PASSWORD")
    chart_timeframe: str = _str("DASHBOARD_CHART_TIMEFRAME", "15m")
    chart_hours: int = _int("DASHBOARD_CHART_HOURS", "4")


@dataclass
class AgentConfig:
    """Full agent configuration."""
    bybit: BybitConfig = field(default_factory=BybitConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    data: DataConfig = field(default_factory=DataConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    log_level: str = _str("LOG_LEVEL", "INFO")
    paper_trading: bool = _bool("PAPER_TRADING", "true")
    dashboard_port: int = _int("DASHBOARD_PORT", "8001")


def load_config() -> AgentConfig:
    """Load configuration from environment."""
    return AgentConfig()
