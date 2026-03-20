"""
Data models for the trading agent.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStrength(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp / 1000)


@dataclass
class Ticker:
    symbol: str
    last_price: float
    bid: float
    ask: float
    volume_24h: float
    change_24h: float
    high_24h: float
    low_24h: float
    timestamp: int = 0


@dataclass
class Indicators:
    """Technical indicator values."""
    rsi: float = 50.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_trend: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_width: float = 0.0
    vwap: float = 0.0
    atr: float = 0.0
    volume_sma: float = 0.0
    current_volume: float = 0.0
    price: float = 0.0


@dataclass
class Signal:
    """Trading signal from strategy analysis."""
    side: Optional[Side] = None
    strength: SignalStrength = SignalStrength.NEUTRAL
    confidence: float = 0.0  # 0-100
    reasons: list = field(default_factory=list)
    indicators: Optional[Indicators] = None
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def should_trade(self) -> bool:
        return self.side is not None and self.confidence > 0


@dataclass
class Position:
    """Open trading position."""
    symbol: str
    side: Side
    entry_price: float
    quantity: float
    leverage: int
    stop_loss: float
    take_profit: float
    trailing_stop: Optional[float] = None
    open_time: datetime = field(default_factory=datetime.now)
    unrealized_pnl: float = 0.0
    order_id: str = ""

    @property
    def value_usdt(self) -> float:
        return self.quantity * self.entry_price


@dataclass
class Trade:
    """Completed trade record."""
    symbol: str
    side: Side
    entry_price: float
    exit_price: float
    quantity: float
    leverage: int
    pnl: float
    pnl_pct: float
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: datetime = field(default_factory=datetime.now)
    reason: str = ""
    signal_confidence: float = 0.0


@dataclass
class MarketContext:
    """Extended market data for AI analysis."""
    # Funding rate
    funding_rate: float = 0.0
    next_funding_time: int = 0
    # Open interest
    open_interest: float = 0.0
    open_interest_change: float = 0.0
    # Order book
    bid_wall_price: float = 0.0
    bid_wall_size: float = 0.0
    ask_wall_price: float = 0.0
    ask_wall_size: float = 0.0
    bid_total: float = 0.0
    ask_total: float = 0.0
    book_imbalance: float = 0.0  # >0 = more bids, <0 = more asks
    # Multi-timeframe trends
    trend_5m: str = "NEUTRAL"  # UP/DOWN/NEUTRAL
    trend_15m: str = "NEUTRAL"
    trend_1h: str = "NEUTRAL"
    rsi_5m: float = 50.0
    rsi_15m: float = 50.0
    rsi_1h: float = 50.0
    # Session info
    trading_session: str = "OFF_HOURS"  # ASIA/EUROPE/US/OFF_HOURS
    session_volume_ratio: float = 1.0  # current vs average


@dataclass
class AccountState:
    """Current account state."""
    balance: float = 0.0
    available: float = 0.0
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_trades: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    total_trades: int = 0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.win_trades / self.total_trades) * 100
