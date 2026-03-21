"""
Data models for the trading agent — Selective Execution Mode.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List


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


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    DEAD_LOW_VOL = "DEAD_LOW_VOL"
    SPIKE_HIGH_VOL = "SPIKE_HIGH_VOL"
    UNKNOWN = "UNKNOWN"


class SetupType(str, Enum):
    CONTINUATION = "CONTINUATION"
    PULLBACK = "PULLBACK"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    REVERSAL = "REVERSAL"
    NONE = "NONE"


class HTFAlignment(str, Enum):
    ALIGNED = "ALIGNED"
    NEUTRAL = "NEUTRAL"
    OPPOSING = "OPPOSING"


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

    @property
    def spread(self) -> float:
        return self.ask - self.bid


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
    # New selective execution indicators
    adx: float = 0.0
    chop_index: float = 50.0
    ema_slope_1m: float = 0.0
    ema_slope_15m: float = 0.0
    ema_slope_1h: float = 0.0
    atr_pct: float = 0.0  # ATR as % of price
    cvd_value: float = 0.0
    cvd_aligned: bool = False
    oi_current: float = 0.0
    oi_change_pct: float = 0.0
    oi_confirmed: bool = False
    extension_atr: float = 0.0  # Distance from mean in ATR units
    volume_ratio: float = 1.0


@dataclass
class RegimeState:
    """Current market regime classification."""
    regime: MarketRegime = MarketRegime.UNKNOWN
    adx: float = 0.0
    chop_index: float = 50.0
    atr_pct: float = 0.0
    bb_width: float = 0.0
    ema_slope: float = 0.0
    trading_allowed: bool = False
    reason: str = ""


@dataclass
class EntryQuality:
    """Composite entry quality assessment."""
    score: int = 0
    htf_alignment: HTFAlignment = HTFAlignment.NEUTRAL
    setup_type: SetupType = SetupType.NONE
    trend_confirmed: bool = False
    extension_ok: bool = False
    volume_confirmed: bool = False
    cvd_aligned: bool = False
    oi_confirmed: bool = False
    candle_confirmed: bool = False
    reasons: List[str] = field(default_factory=list)
    skip_reasons: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.skip_reasons) == 0 and self.score > 0


@dataclass
class DirectionalLicense:
    """LLM-issued permission to trade a direction."""
    direction: Optional[Side] = None
    confidence: int = 0
    regime: str = "UNKNOWN"
    setup_type: str = "NONE"
    entry_quality: int = 0
    htf_alignment: str = "NEUTRAL"
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    valid_until: Optional[datetime] = None
    price_at_issue: float = 0.0

    @property
    def is_valid(self) -> bool:
        if self.valid_until and datetime.now() > self.valid_until:
            return False
        return self.direction is not None


@dataclass
class Signal:
    """Trading signal from strategy analysis."""
    side: Optional[Side] = None
    strength: SignalStrength = SignalStrength.NEUTRAL
    confidence: float = 0.0
    reasons: list = field(default_factory=list)
    indicators: Optional[Indicators] = None
    timestamp: datetime = field(default_factory=datetime.now)
    # Selective execution fields
    regime: MarketRegime = MarketRegime.UNKNOWN
    entry_quality: int = 0
    setup_type: SetupType = SetupType.NONE
    htf_alignment: HTFAlignment = HTFAlignment.NEUTRAL
    extension_atr: float = 0.0
    cvd_aligned: bool = False
    oi_confirmed: bool = False
    volume_ratio: float = 1.0
    net_rr: float = 0.0
    skip_reason: str = ""
    entry_reason: str = ""
    trade_source: str = "AI"

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
    original_quantity: float = 0.0
    partial_closed: bool = False
    # MFE/MAE tracking
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    # Context at entry
    trade_id: str = ""
    entry_quality: int = 0
    regime: str = "UNKNOWN"
    setup_type: str = "NONE"
    htf_alignment: str = "NEUTRAL"
    confidence: float = 0.0
    trade_source: str = "AI"
    entry_type: str = "TAKER"

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
    # Extended fields
    trade_id: str = ""
    trade_source: str = "AI"
    regime: str = "UNKNOWN"
    entry_quality: int = 0
    setup_type: str = "NONE"
    htf_alignment: str = "NEUTRAL"
    net_rr: float = 0.0
    entry_type: str = "TAKER"
    exit_type: str = ""
    fees_paid_usd: float = 0.0
    gross_pnl: float = 0.0
    net_pnl_usd: float = 0.0
    slippage_bps: float = 0.0
    hold_duration_sec: int = 0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0


@dataclass
class MarketContext:
    """Extended market data for AI analysis."""
    funding_rate: float = 0.0
    next_funding_time: int = 0
    open_interest: float = 0.0
    open_interest_change: float = 0.0
    bid_wall_price: float = 0.0
    bid_wall_size: float = 0.0
    ask_wall_price: float = 0.0
    ask_wall_size: float = 0.0
    bid_total: float = 0.0
    ask_total: float = 0.0
    book_imbalance: float = 0.0
    trend_5m: str = "NEUTRAL"
    trend_15m: str = "NEUTRAL"
    trend_1h: str = "NEUTRAL"
    rsi_5m: float = 50.0
    rsi_15m: float = 50.0
    rsi_1h: float = 50.0
    trading_session: str = "OFF_HOURS"
    session_volume_ratio: float = 1.0
    fear_greed_index: int = 50
    fear_greed_label: str = "Neutral"
    # New fields
    spread_usdt: float = 0.0
    api_latency_ms: int = 0
    regime: MarketRegime = MarketRegime.UNKNOWN
    cvd_value: float = 0.0
    ema_slope_5m: float = 0.0
    ema_slope_15m: float = 0.0
    ema_slope_1h: float = 0.0


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
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    weekly_pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.win_trades / self.total_trades) * 100
