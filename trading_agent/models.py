"""Data models for the trading bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


class Regime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    DEAD_LOW_VOL = "DEAD_LOW_VOL"
    SPIKE_HIGH_VOL = "SPIKE_HIGH_VOL"


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
class DirectionalLicense:
    """Permission from AI to trade a specific direction, valid for N minutes."""
    action: Action
    confidence: int
    regime: Regime
    setup_type: SetupType
    entry_quality: int
    htf_alignment: HTFAlignment
    reason: str
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    valid_minutes: int = 15

    @property
    def is_expired(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self.issued_at).total_seconds()
        return elapsed > self.valid_minutes * 60

    @property
    def is_trade(self) -> bool:
        return self.action in (Action.LONG, Action.SHORT)

    @property
    def is_reversal(self) -> bool:
        return self.setup_type == SetupType.REVERSAL


@dataclass
class RegimeState:
    """Deterministic regime classification result."""
    regime: Regime
    adx: float
    chop: float
    atr_pct: float
    bb_width: float
    ema_slope: float
    details: str = ""

    @property
    def allows_trading(self) -> bool:
        return self.regime == Regime.TRENDING

    @property
    def allows_breakout(self) -> bool:
        return self.regime in (Regime.TRENDING, Regime.SPIKE_HIGH_VOL)


@dataclass
class EntryGateResult:
    """Result of all entry sub-gates."""
    passed: bool
    htf_ok: bool = False
    trend_ok: bool = False
    extension_ok: bool = False
    candle_ok: bool = False
    volume_ok: bool = False
    reversal_ok: bool = True  # True by default (only checked for reversals)
    regime_ok: bool = False
    cvd_ok: bool = True  # True by default (skipped if disabled)
    oi_ok: bool = True   # True by default (skipped if data unavailable)
    session_ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def add_block(self, reason: str) -> None:
        self.reasons.append(reason)
        self.passed = False


@dataclass
class CandleData:
    """OHLCV candle data."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> float:
        return self.high - self.low


@dataclass
class TradeRecord:
    """Record of a completed trade for persistence."""
    id: Optional[int] = None
    symbol: str = "BTCUSDT"
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    confidence: int = 0
    entry_quality: int = 0
    regime: str = ""
    setup_type: str = ""
    htf_alignment: str = ""
    source: str = ""  # "ai" or "fallback"
    reason: str = ""
