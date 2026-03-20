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

    # Leverage settings (defaults — AI decides actual leverage per trade)
    leverage: int = int(os.getenv("TRADING_LEVERAGE", "20"))
    max_leverage: int = 50  # AI can go up to 50x if confident

    # Position sizing (defaults — AI decides actual sizing per trade)
    max_position_pct: float = 0.90  # AI can use up to 90% if confident
    min_order_usdt: float = 1.0

    # Risk management (defaults — AI overrides)
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "1.2"))
    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "3.0"))
    trailing_stop_pct: float = 1.0
    max_daily_loss_pct: float = 25.0  # Stop trading after 25% daily loss
    max_open_positions: int = 1

    # Timing
    candle_interval: str = "Min1"
    analysis_interval: int = 30  # Every 30 seconds — give market time to move
    cooldown_after_trade: int = 60  # Wait 60s between trades to avoid overtrading

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
    min_confidence: float = float(os.getenv("MIN_CONFIDENCE", "40"))

    # Minimum hold time in seconds — prevent closing trades too early
    min_hold_time: int = int(os.getenv("MIN_HOLD_TIME", "120"))


@dataclass
class AIConfig:
    """Claude AI configuration."""
    api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("AI_MODEL", "claude-haiku-4-5-20251001")
    # How often to ask Claude (every N ticks) — higher = less frequent close checks
    analysis_every_n_ticks: int = int(os.getenv("AI_ANALYSIS_INTERVAL", "4"))
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
