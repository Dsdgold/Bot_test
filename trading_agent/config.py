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

# --- Risk / Position (stubs for later chunks) ---
POSITION_SIZE_USD = float(os.getenv("POSITION_SIZE_USD", "100"))
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
DEFAULT_TP_PCT = float(os.getenv("DEFAULT_TP_PCT", "0.4"))
DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", "0.25"))
