"""Trading bot configuration — all values from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()


# --- API Keys (never hardcoded) ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")

# --- Symbol ---
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
CATEGORY = "linear"

# --- Core selectivity ---
STRICT_WAIT_MODE = os.getenv("STRICT_WAIT_MODE", "true").lower() == "true"
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "60"))
ENABLE_FALLBACK_OVERRIDE = os.getenv("ENABLE_FALLBACK_OVERRIDE", "false").lower() == "true"

# --- Regime filter ---
REGIME_FILTER_ENABLED = os.getenv("REGIME_FILTER_ENABLED", "true").lower() == "true"
USE_ADX_FILTER = os.getenv("USE_ADX_FILTER", "true").lower() == "true"
ADX_MIN = float(os.getenv("ADX_MIN", "18"))
USE_CHOP_FILTER = os.getenv("USE_CHOP_FILTER", "true").lower() == "true"
CHOP_MAX = float(os.getenv("CHOP_MAX", "61.8"))
DEAD_VOL_ATR_PCT_MIN = float(os.getenv("DEAD_VOL_ATR_PCT_MIN", "0.05"))
SPIKE_CANDLE_ATR_MAX = float(os.getenv("SPIKE_CANDLE_ATR_MAX", "1.5"))

# --- Entry quality gates ---
TRADE_QUALITY_MIN = int(os.getenv("TRADE_QUALITY_MIN", "70"))
REVERSAL_QUALITY_MIN = int(os.getenv("REVERSAL_QUALITY_MIN", "80"))
MAX_ENTRY_EXTENSION_ATR = float(os.getenv("MAX_ENTRY_EXTENSION_ATR", "0.8"))
CANDLE_CLOSE_CONFIRMATION = os.getenv("CANDLE_CLOSE_CONFIRMATION", "true").lower() == "true"
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "1.15"))

# --- Directional License ---
LICENSE_VALIDITY_MINUTES = int(os.getenv("LICENSE_VALIDITY_MINUTES", "15"))

# --- Trend confirmation ---
EMA_SLOPE_MIN = float(os.getenv("EMA_SLOPE_MIN", "0.0001"))
TREND_SCORE_MIN = float(os.getenv("TREND_SCORE_MIN", "0.5"))

# --- Timeframes ---
TIMEFRAME_1M = "1"
TIMEFRAME_5M = "5"
TIMEFRAME_15M = "15"
TIMEFRAME_1H = "60"

# --- Fallback override strict thresholds ---
FALLBACK_MIN_CONFIDENCE = int(os.getenv("FALLBACK_MIN_CONFIDENCE", "80"))
FALLBACK_MIN_QUALITY = int(os.getenv("FALLBACK_MIN_QUALITY", "85"))

# --- Microstructure / order flow ---
REQUIRE_OI_CONFIRMATION = os.getenv("REQUIRE_OI_CONFIRMATION", "true").lower() == "true"
REQUIRE_CVD_ALIGNMENT = os.getenv("REQUIRE_CVD_ALIGNMENT", "true").lower() == "true"

# --- Execution ---
PREFER_POST_ONLY_ENTRIES = os.getenv("PREFER_POST_ONLY_ENTRIES", "true").lower() == "true"
MAX_ALLOWED_SLIPPAGE_BPS = float(os.getenv("MAX_ALLOWED_SLIPPAGE_BPS", "5"))
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.00055"))
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", "0.0002"))
MAX_SPREAD_TOLERANCE_USDT = float(os.getenv("MAX_SPREAD_TOLERANCE_USDT", "2.5"))

# --- Dynamic SL/TP ---
USE_DYNAMIC_SL_TP = os.getenv("USE_DYNAMIC_SL_TP", "true").lower() == "true"
ATR_STOP_MULT = float(os.getenv("ATR_STOP_MULT", "1.0"))
TARGET_RR_A_PLUS = float(os.getenv("TARGET_RR_A_PLUS", "1.6"))
TARGET_RR_A = float(os.getenv("TARGET_RR_A", "1.35"))
TARGET_RR_B = float(os.getenv("TARGET_RR_B", "1.2"))
MIN_NET_RR = float(os.getenv("MIN_NET_RR", "1.15"))
MIN_SL_PCT = float(os.getenv("MIN_SL_PCT", "0.35"))
MAX_SL_PCT = float(os.getenv("MAX_SL_PCT", "0.90"))
MIN_TP_PCT = float(os.getenv("MIN_TP_PCT", "0.45"))
MAX_TP_PCT = float(os.getenv("MAX_TP_PCT", "1.50"))

# --- Session filter ---
SESSION_FILTER_ENABLED = os.getenv("SESSION_FILTER_ENABLED", "true").lower() == "true"
TIMEZONE = os.getenv("TIMEZONE", "Europe/Warsaw")
BLOCKED_HOURS_LOCAL = [
    int(h.strip()) for h in os.getenv("BLOCKED_HOURS_LOCAL", "").split(",")
    if h.strip().isdigit()
]
MIN_SAMPLE_PER_HOUR = int(os.getenv("MIN_SAMPLE_PER_HOUR", "10"))

# --- Cooldowns ---
POST_LOSS_COOLDOWN_SEC = int(os.getenv("POST_LOSS_COOLDOWN_SEC", "300"))
REENTRY_COOLDOWN_CANDLES = int(os.getenv("REENTRY_COOLDOWN_CANDLES", "3"))
SAME_SIDE_LOSS_PAUSE_COUNT = int(os.getenv("SAME_SIDE_LOSS_PAUSE_COUNT", "2"))
SAME_SIDE_LOSS_PAUSE_SEC = int(os.getenv("SAME_SIDE_LOSS_PAUSE_SEC", "1800"))

# --- Kill switches ---
FREEZE_ON_EXTREME_FUNDING = os.getenv("FREEZE_ON_EXTREME_FUNDING", "true").lower() == "true"
MAX_FUNDING_RATE_ABS = float(os.getenv("MAX_FUNDING_RATE_ABS", "0.001"))
LATENCY_KILL_SWITCH_MS = int(os.getenv("LATENCY_KILL_SWITCH_MS", "500"))

# --- Data collection ---
ENABLE_TRADE_SNAPSHOTS = os.getenv("ENABLE_TRADE_SNAPSHOTS", "true").lower() == "true"
ENABLE_MARKET_SNAPSHOTS = os.getenv("ENABLE_MARKET_SNAPSHOTS", "true").lower() == "true"
MARKET_SNAPSHOT_INTERVAL_SEC = int(os.getenv("MARKET_SNAPSHOT_INTERVAL_SEC", "300"))
ENABLE_EQUITY_TRACKING = os.getenv("ENABLE_EQUITY_TRACKING", "true").lower() == "true"
EQUITY_SNAPSHOT_INTERVAL_SEC = int(os.getenv("EQUITY_SNAPSHOT_INTERVAL_SEC", "900"))
ENABLE_MFE_MAE_TRACKING = os.getenv("ENABLE_MFE_MAE_TRACKING", "true").lower() == "true"
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

# --- Analytics engine ---
AUTO_ANALYTICS_ENABLED = os.getenv("AUTO_ANALYTICS_ENABLED", "true").lower() == "true"
ANALYTICS_RUN_INTERVAL_HOURS = int(os.getenv("ANALYTICS_RUN_INTERVAL_HOURS", "24"))
MIN_SAMPLE_FOR_RECOMMENDATIONS = int(os.getenv("MIN_SAMPLE_FOR_RECOMMENDATIONS", "50"))
EDGE_DECAY_WINDOW_TRADES = int(os.getenv("EDGE_DECAY_WINDOW_TRADES", "30"))
EDGE_DECAY_ALERT_THRESHOLD = float(os.getenv("EDGE_DECAY_ALERT_THRESHOLD", "0.50"))
FEE_EROSION_ALERT_THRESHOLD = float(os.getenv("FEE_EROSION_ALERT_THRESHOLD", "0.40"))
SKIP_RATE_ALERT_THRESHOLD = float(os.getenv("SKIP_RATE_ALERT_THRESHOLD", "0.95"))
SKIP_RATE_ALERT_HOURS = int(os.getenv("SKIP_RATE_ALERT_HOURS", "4"))

# --- Position sizing ---
POSITION_SIZING_MODE = os.getenv("POSITION_SIZING_MODE", "fixed_fractional")
BASE_RISK_PER_TRADE_PCT = float(os.getenv("BASE_RISK_PER_TRADE_PCT", "1.0"))
MAX_RISK_PER_TRADE_PCT = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "2.0"))
QUALITY_SIZE_MULTIPLIER_A_PLUS = float(os.getenv("QUALITY_SIZE_MULTIPLIER_A_PLUS", "1.5"))
QUALITY_SIZE_MULTIPLIER_A = float(os.getenv("QUALITY_SIZE_MULTIPLIER_A", "1.2"))
QUALITY_SIZE_MULTIPLIER_B = float(os.getenv("QUALITY_SIZE_MULTIPLIER_B", "1.0"))
STREAK_LOSS_REDUCTION_1 = float(os.getenv("STREAK_LOSS_REDUCTION_1", "0.75"))
STREAK_LOSS_REDUCTION_2 = float(os.getenv("STREAK_LOSS_REDUCTION_2", "0.50"))
STREAK_LOSS_REDUCTION_3 = float(os.getenv("STREAK_LOSS_REDUCTION_3", "0.25"))

# --- Growth tiers ---
TIER_1_EQUITY = float(os.getenv("TIER_1_EQUITY", "1000"))
TIER_1_RISK_PCT = float(os.getenv("TIER_1_RISK_PCT", "1.0"))
TIER_2_EQUITY = float(os.getenv("TIER_2_EQUITY", "2500"))
TIER_2_RISK_PCT = float(os.getenv("TIER_2_RISK_PCT", "1.25"))
TIER_3_EQUITY = float(os.getenv("TIER_3_EQUITY", "5000"))
TIER_3_RISK_PCT = float(os.getenv("TIER_3_RISK_PCT", "1.5"))
TIER_4_EQUITY = float(os.getenv("TIER_4_EQUITY", "10000"))
TIER_4_RISK_PCT = float(os.getenv("TIER_4_RISK_PCT", "1.75"))

# --- Drawdown tiers ---
DD_TIER_1_PCT = float(os.getenv("DD_TIER_1_PCT", "3.0"))
DD_TIER_1_MULT = float(os.getenv("DD_TIER_1_MULT", "0.70"))
DD_TIER_2_PCT = float(os.getenv("DD_TIER_2_PCT", "5.0"))
DD_TIER_2_MULT = float(os.getenv("DD_TIER_2_MULT", "0.40"))
DD_TIER_3_PCT = float(os.getenv("DD_TIER_3_PCT", "8.0"))
DD_TIER_3_MULT = float(os.getenv("DD_TIER_3_MULT", "0.20"))
DD_HALT_PCT = float(os.getenv("DD_HALT_PCT", "10.0"))

# --- Capital protection ---
DAILY_MAX_LOSS_PCT = float(os.getenv("DAILY_MAX_LOSS_PCT", "3.0"))
WEEKLY_MAX_LOSS_PCT = float(os.getenv("WEEKLY_MAX_LOSS_PCT", "7.0"))
EQUITY_FLOOR_USDT = float(os.getenv("EQUITY_FLOOR_USDT", "500"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "1"))

# --- Token optimization ---
LICENSE_CACHE_TTL_SEC = int(os.getenv("LICENSE_CACHE_TTL_SEC", "180"))
LICENSE_CACHE_INVALIDATE_ATR_MOVE = float(os.getenv("LICENSE_CACHE_INVALIDATE_ATR_MOVE", "1.0"))
LICENSE_CACHE_INVALIDATE_VOL_SPIKE = float(os.getenv("LICENSE_CACHE_INVALIDATE_VOL_SPIKE", "3.0"))
LLM_MAX_TOKENS_LICENSE = int(os.getenv("LLM_MAX_TOKENS_LICENSE", "200"))
LLM_COMPRESSED_PROMPT = os.getenv("LLM_COMPRESSED_PROMPT", "true").lower() == "true"
DAILY_TOKEN_BUDGET = int(os.getenv("DAILY_TOKEN_BUDGET", "50000"))
TOKEN_BUDGET_ALERT_PCT = int(os.getenv("TOKEN_BUDGET_ALERT_PCT", "80"))
TOKEN_BUDGET_HARD_STOP = os.getenv("TOKEN_BUDGET_HARD_STOP", "true").lower() == "true"
DETERMINISTIC_FALLBACK_ON_BUDGET_EXCEEDED = os.getenv("DETERMINISTIC_FALLBACK_ON_BUDGET_EXCEEDED", "true").lower() == "true"

# --- Learning journal ---
LEARNING_JOURNAL_ENABLED = os.getenv("LEARNING_JOURNAL_ENABLED", "true").lower() == "true"
JOURNAL_POST_TRADE = os.getenv("JOURNAL_POST_TRADE", "true").lower() == "true"
JOURNAL_SKIP_REVIEW_INTERVAL_HOURS = int(os.getenv("JOURNAL_SKIP_REVIEW_INTERVAL_HOURS", "2"))
JOURNAL_ON_REGIME_SHIFT = os.getenv("JOURNAL_ON_REGIME_SHIFT", "true").lower() == "true"
JOURNAL_ON_EDGE_DECAY = os.getenv("JOURNAL_ON_EDGE_DECAY", "true").lower() == "true"
JOURNAL_MAX_TOKENS_POST_TRADE = int(os.getenv("JOURNAL_MAX_TOKENS_POST_TRADE", "150"))
JOURNAL_MAX_TOKENS_SKIP_REVIEW = int(os.getenv("JOURNAL_MAX_TOKENS_SKIP_REVIEW", "300"))

# --- Self-optimization ---
AUTONOMOUS_TUNING_ENABLED = os.getenv("AUTONOMOUS_TUNING_ENABLED", "true").lower() == "true"
TUNING_CYCLE_HOURS = int(os.getenv("TUNING_CYCLE_HOURS", "6"))
TUNING_LOOKBACK_TRADES = int(os.getenv("TUNING_LOOKBACK_TRADES", "200"))
TUNING_MIN_SAMPLE = int(os.getenv("TUNING_MIN_SAMPLE", "10"))
TUNING_MIN_CONFIDENCE_PCT = int(os.getenv("TUNING_MIN_CONFIDENCE_PCT", "70"))
PROBATION_TRADES = int(os.getenv("PROBATION_TRADES", "15"))
MAX_CONCURRENT_PROBATIONS = int(os.getenv("MAX_CONCURRENT_PROBATIONS", "2"))
TUNING_FREEZE_DD_PCT = float(os.getenv("TUNING_FREEZE_DD_PCT", "5.0"))
ROLLBACK_THRESHOLD_PCT = float(os.getenv("ROLLBACK_THRESHOLD_PCT", "15"))
ROLLBACK_MAX_DD_PCT = float(os.getenv("ROLLBACK_MAX_DD_PCT", "5.0"))
ROLLBACK_WR_DROP_PCT = float(os.getenv("ROLLBACK_WR_DROP_PCT", "10"))
LOCKOUT_CYCLES = int(os.getenv("LOCKOUT_CYCLES", "5"))
META_LEARNING_INTERVAL_HOURS = int(os.getenv("META_LEARNING_INTERVAL_HOURS", "12"))
META_LEARNING_MAX_TOKENS = int(os.getenv("META_LEARNING_MAX_TOKENS", "500"))

# --- Dashboard ---
ENABLE_DASHBOARD = os.getenv("ENABLE_DASHBOARD", "true").lower() == "true"
DASHBOARD_REFRESH_SEC = int(os.getenv("DASHBOARD_REFRESH_SEC", "10"))
DASHBOARD_MODE = os.getenv("DASHBOARD_MODE", "web")
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
DASHBOARD_CHART_TIMEFRAME = os.getenv("DASHBOARD_CHART_TIMEFRAME", "15m")
DASHBOARD_CHART_HOURS = int(os.getenv("DASHBOARD_CHART_HOURS", "4"))

# --- Auto export ---
AUTO_EXPORT_ENABLED = os.getenv("AUTO_EXPORT_ENABLED", "true").lower() == "true"
AUTO_EXPORT_INTERVAL_HOURS = int(os.getenv("AUTO_EXPORT_INTERVAL_HOURS", "24"))
AUTO_EXPORT_FORMAT = os.getenv("AUTO_EXPORT_FORMAT", "csv")
AUTO_EXPORT_PATH = os.getenv("AUTO_EXPORT_PATH", "./exports/")
AUTO_EXPORT_RETAIN_DAYS = int(os.getenv("AUTO_EXPORT_RETAIN_DAYS", "90"))

# --- Risk / Position ---
POSITION_SIZE_USD = float(os.getenv("POSITION_SIZE_USD", "100"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.4"))
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.25"))
