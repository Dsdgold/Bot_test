"""
Trading Agent Configuration
"""
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
load_dotenv()


@dataclass
class BybitConfig:
    """Bybit API configuration."""
    api_key: str = os.getenv("BYBIT_API_KEY", "")
    api_secret: str = os.getenv("BYBIT_API_SECRET", "")
    base_url: str = os.getenv(
        "BYBIT_BASE_URL",
        "https://api-testnet.bybit.com" if os.getenv("BYBIT_TESTNET", "false").lower() == "true"
        else "https://api.bybit.com"
    )
    testnet: bool = os.getenv("BYBIT_TESTNET", "false").lower() == "true"


@dataclass
class TradingConfig:
    """Trading parameters."""
    # Symbol to trade (USDT perpetual futures)
    symbol: str = os.getenv("TRADING_SYMBOL", "BTCUSDT")

    # Leverage settings
    leverage: int = int(os.getenv("TRADING_LEVERAGE", "20"))
    max_leverage: int = 50

    # Position sizing
    max_position_pct: float = 0.3  # Max 30% of balance per position
    min_order_usdt: float = 5.0

    # Risk management
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "1.5"))
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
    trailing_stop_pct: float = 0.5
    max_daily_loss_pct: float = 10.0  # Stop trading after 10% daily loss
    max_open_positions: int = 1

    # Scalping timing
    candle_interval: str = "Min1"  # 1-minute candles for scalping
    analysis_interval: int = 5  # Analyze every 5 seconds
    cooldown_after_trade: int = 5  # Wait 5s after closing a trade

    # Strategy thresholds
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

    # Confidence threshold - minimum score to open a trade (0-100)
    min_confidence: float = float(os.getenv("MIN_CONFIDENCE", "20"))


@dataclass
class AIConfig:
    """Claude AI configuration."""
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("AI_MODEL", "claude-sonnet-4-20250514")
    # How often to ask Claude (every N ticks)
    analysis_every_n_ticks: int = int(os.getenv("AI_ANALYSIS_INTERVAL", "1"))
    # Use AI for position close decisions too
    ai_close_decisions: bool = os.getenv("AI_CLOSE_DECISIONS", "true").lower() == "true"


@dataclass
class AgentConfig:
    """Full agent configuration."""
    bybit: BybitConfig = field(default_factory=BybitConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    paper_trading: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
    dashboard_port: int = int(os.getenv("DASHBOARD_PORT", "8001"))


def load_config() -> AgentConfig:
    """Load configuration from environment."""
    return AgentConfig()
