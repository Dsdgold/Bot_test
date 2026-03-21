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
DEAD_VOL_ATR_PCT_MIN = float(os.getenv("DEAD_VOL_ATR_PCT_MIN", "0.10"))
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
POST_LOSS_COOLDOWN_SEC = int(os.getenv("POST_LOSS_COOLDOWN_SEC", "600"))
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

# --- Risk / Position ---
POSITION_SIZE_USD = float(os.getenv("POSITION_SIZE_USD", "100"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.4"))
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.25"))
