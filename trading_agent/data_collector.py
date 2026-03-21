"""Data collection engine — SQLite black box recorder.

Persists every trade decision, market snapshot, equity state, and daily summary.
MFE/MAE tracked tick-by-tick during trade lifetime.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from trading_agent import config
from trading_agent.indicators import (
    adx, atr, atr_percent, bollinger_band_width, choppiness_index,
    compute_cvd, ema, ema_slope, macd_histogram, rsi, sma, volume_ratio,
)
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, EntryGateResult, RegimeState,
)
from trading_agent.risk_manager import SLTPLevels

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

_TRADE_DECISIONS_SQL = """
CREATE TABLE IF NOT EXISTS trade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    decision TEXT NOT NULL,
    trade_id TEXT,
    price REAL,
    bid REAL,
    ask REAL,
    spread_usdt REAL,
    atr_1m REAL,
    atr_5m REAL,
    atr_15m REAL,
    volatility_pct REAL,
    volume_current REAL,
    volume_avg_20 REAL,
    volume_ratio REAL,
    ema9 REAL,
    ema21 REAL,
    ema50 REAL,
    rsi_14 REAL,
    adx REAL,
    chop_index REAL,
    bb_width REAL,
    macd_hist REAL,
    ema_slope_1m REAL,
    ema_slope_15m REAL,
    ema_slope_1h REAL,
    cvd_value REAL,
    cvd_aligned INTEGER,
    oi_current REAL,
    oi_change_pct REAL,
    oi_confirmed INTEGER,
    funding_rate REAL,
    orderbook_imbalance REAL,
    regime TEXT,
    htf_alignment TEXT,
    setup_type TEXT,
    entry_quality INTEGER,
    confidence INTEGER,
    extension_atr REAL,
    trade_source TEXT,
    entry_type TEXT,
    entry_price REAL,
    sl_price REAL,
    tp_price REAL,
    planned_rr REAL,
    net_rr REAL,
    position_size_usd REAL,
    leverage REAL,
    skip_reason TEXT,
    entry_reason TEXT,
    exit_price REAL,
    exit_type TEXT,
    gross_pnl_usd REAL,
    fees_paid_usd REAL,
    net_pnl_usd REAL,
    net_pnl_pct REAL,
    slippage_bps REAL,
    hold_duration_sec INTEGER,
    max_favorable_excursion REAL,
    max_adverse_excursion REAL,
    session_hour_local INTEGER,
    day_of_week INTEGER,
    equity_before REAL,
    equity_after REAL,
    drawdown_pct REAL,
    api_latency_ms INTEGER,
    ai_response_time_ms INTEGER
)
"""

_MARKET_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    price REAL,
    atr_1m REAL,
    volume_ratio REAL,
    regime TEXT,
    adx REAL,
    chop_index REAL,
    cvd_value REAL,
    oi_current REAL,
    funding_rate REAL,
    spread_usdt REAL,
    bb_width REAL
)
"""

_EQUITY_CURVE_SQL = """
CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    equity_usdt REAL,
    unrealized_pnl REAL,
    total_equity REAL,
    peak_equity REAL,
    drawdown_pct REAL,
    drawdown_duration_min INTEGER,
    total_trades INTEGER,
    total_wins INTEGER,
    win_rate REAL,
    total_net_pnl REAL,
    position_size_tier TEXT
)
"""

_DAILY_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS daily_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    trades_taken INTEGER DEFAULT 0,
    trades_skipped INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    gross_pnl REAL DEFAULT 0,
    total_fees REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    best_trade_pnl REAL DEFAULT 0,
    worst_trade_pnl REAL DEFAULT 0,
    avg_hold_time_sec REAL DEFAULT 0,
    dominant_regime TEXT,
    equity_start REAL,
    equity_end REAL,
    equity_growth_pct REAL DEFAULT 0,
    skip_reasons_json TEXT
)
"""

ALL_SCHEMAS = [
    _TRADE_DECISIONS_SQL,
    _MARKET_SNAPSHOTS_SQL,
    _EQUITY_CURVE_SQL,
    _DAILY_SESSIONS_SQL,
]


# ---------------------------------------------------------------------------
# MFE/MAE tracker
# ---------------------------------------------------------------------------

class MFEMAETracker:
    """Track Max Favorable and Adverse Excursion during a trade."""

    def __init__(self, entry_price: float, direction: str):
        self.entry_price = entry_price
        self.direction = direction  # "LONG" or "SHORT"
        self.mfe: float = 0.0  # Max unrealized profit
        self.mae: float = 0.0  # Max unrealized loss (stored as positive)
        self._active = True

    def update(self, current_price: float) -> None:
        """Update MFE/MAE with a new price tick."""
        if not self._active:
            return

        if self.direction == "LONG":
            unrealized = current_price - self.entry_price
        else:
            unrealized = self.entry_price - current_price

        if unrealized > self.mfe:
            self.mfe = unrealized
        if unrealized < 0 and abs(unrealized) > self.mae:
            self.mae = abs(unrealized)

    def close(self) -> tuple[float, float]:
        """Close tracking and return (MFE, MAE)."""
        self._active = False
        return self.mfe, self.mae


# ---------------------------------------------------------------------------
# DataCollector
# ---------------------------------------------------------------------------

class DataCollector:
    """Central data collection engine backed by SQLite."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or config.DB_PATH
        self._conn: Optional[sqlite3.Connection] = None
        self._active_tracker: Optional[MFEMAETracker] = None
        self._active_trade_id: Optional[str] = None
        self._active_entry_time: Optional[float] = None
        self._peak_equity: float = 0.0
        self._drawdown_start: Optional[float] = None
        self._last_market_snapshot: float = 0.0
        self._last_equity_snapshot: float = 0.0
        self._total_trades: int = 0
        self._total_wins: int = 0
        self._total_net_pnl: float = 0.0
        self._initialize()

    def _initialize(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        for schema in ALL_SCHEMAS:
            conn.execute(schema)
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _local_hour() -> int:
        now = datetime.now(timezone.utc)
        if ZoneInfo:
            try:
                return now.astimezone(ZoneInfo(config.TIMEZONE)).hour
            except Exception:
                pass
        return now.hour

    @staticmethod
    def _day_of_week() -> int:
        return datetime.now(timezone.utc).weekday()

    # ------------------------------------------------------------------
    # Indicator snapshot helpers
    # ------------------------------------------------------------------

    def _compute_indicators(
        self,
        candles_1m: Sequence[CandleData],
        candles_5m: Sequence[CandleData] | None = None,
        candles_15m: Sequence[CandleData] | None = None,
        candles_1h: Sequence[CandleData] | None = None,
    ) -> dict:
        """Compute all indicator values for a snapshot."""
        d: dict = {}
        closes = [c.close for c in candles_1m] if candles_1m else []

        if candles_1m and len(candles_1m) >= 2:
            atr_vals = atr(candles_1m, 14)
            d["atr_1m"] = atr_vals[-1] if atr_vals else 0
            d["volatility_pct"] = atr_percent(candles_1m)
            d["volume_current"] = candles_1m[-1].volume
            vol_avg = sma([c.volume for c in candles_1m], 20)
            d["volume_avg_20"] = vol_avg[-1] if vol_avg else 0
            d["volume_ratio"] = volume_ratio(candles_1m)
            d["adx"] = adx(candles_1m)
            d["chop_index"] = choppiness_index(candles_1m)
            d["bb_width"] = bollinger_band_width(candles_1m)
            d["rsi_14"] = rsi(candles_1m)
            d["macd_hist"] = macd_histogram(candles_1m)
            d["ema_slope_1m"] = ema_slope(candles_1m)

            cvd_vals = compute_cvd(candles_1m)
            d["cvd_value"] = cvd_vals[-1] if cvd_vals else 0

            if closes and len(closes) >= 50:
                ema_vals = ema(closes, 50)
                d["ema50"] = ema_vals[-1]
            else:
                d["ema50"] = 0
            if closes and len(closes) >= 21:
                d["ema21"] = ema(closes, 21)[-1]
            else:
                d["ema21"] = 0
            if closes and len(closes) >= 9:
                d["ema9"] = ema(closes, 9)[-1]
            else:
                d["ema9"] = 0
        else:
            for k in ("atr_1m", "volatility_pct", "volume_current", "volume_avg_20",
                       "volume_ratio", "adx", "chop_index", "bb_width", "rsi_14",
                       "macd_hist", "ema_slope_1m", "cvd_value", "ema9", "ema21", "ema50"):
                d[k] = 0

        d["atr_5m"] = atr(candles_5m, 14)[-1] if candles_5m and len(candles_5m) >= 2 else 0
        d["atr_15m"] = atr(candles_15m, 14)[-1] if candles_15m and len(candles_15m) >= 2 else 0
        d["ema_slope_15m"] = ema_slope(candles_15m) if candles_15m and len(candles_15m) >= 24 else 0
        d["ema_slope_1h"] = ema_slope(candles_1h) if candles_1h and len(candles_1h) >= 24 else 0

        return d

    # ------------------------------------------------------------------
    # Trade decisions
    # ------------------------------------------------------------------

    def save_trade_decision(
        self,
        decision: str,
        license: DirectionalLicense,
        gate_result: EntryGateResult,
        regime: RegimeState,
        candles_1m: Sequence[CandleData],
        candles_5m: Sequence[CandleData] | None = None,
        candles_15m: Sequence[CandleData] | None = None,
        candles_1h: Sequence[CandleData] | None = None,
        sl_tp: SLTPLevels | None = None,
        spread: float = 0.0,
        funding_rate: float = 0.0,
        oi_current: float | None = None,
        oi_previous: float | None = None,
        equity: float = 0.0,
        latency_ms: int = 0,
        ai_response_ms: int = 0,
        trade_id: str | None = None,
        exit_price: float | None = None,
        exit_type: str | None = None,
        gross_pnl: float | None = None,
        fees_paid: float | None = None,
        net_pnl: float | None = None,
        slippage_bps: float | None = None,
        hold_duration_sec: int | None = None,
        mfe: float | None = None,
        mae: float | None = None,
    ) -> int:
        """Persist a complete trade decision snapshot."""
        if not config.ENABLE_TRADE_SNAPSHOTS:
            return 0

        ind = self._compute_indicators(candles_1m, candles_5m, candles_15m, candles_1h)
        price = candles_1m[-1].close if candles_1m else 0

        oi_change_pct = 0.0
        if oi_current is not None and oi_previous is not None and oi_previous > 0:
            oi_change_pct = (oi_current - oi_previous) / oi_previous * 100

        # Drawdown
        if equity > self._peak_equity:
            self._peak_equity = equity
        dd_pct = ((self._peak_equity - equity) / self._peak_equity * 100) if self._peak_equity > 0 else 0

        net_pnl_pct = 0.0
        if net_pnl is not None and sl_tp and sl_tp.entry_price > 0:
            pos_val = config.POSITION_SIZE_USD
            net_pnl_pct = (net_pnl / pos_val * 100) if pos_val > 0 else 0

        planned_rr = 0.0
        if sl_tp:
            risk = abs(sl_tp.entry_price - sl_tp.stop_loss)
            reward = abs(sl_tp.take_profit - sl_tp.entry_price)
            planned_rr = reward / risk if risk > 0 else 0

        equity_after = equity
        if net_pnl is not None:
            equity_after = equity + net_pnl

        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO trade_decisions (
                timestamp_utc, decision, trade_id, price, bid, ask, spread_usdt,
                atr_1m, atr_5m, atr_15m, volatility_pct,
                volume_current, volume_avg_20, volume_ratio,
                ema9, ema21, ema50, rsi_14, adx, chop_index, bb_width, macd_hist,
                ema_slope_1m, ema_slope_15m, ema_slope_1h,
                cvd_value, cvd_aligned, oi_current, oi_change_pct, oi_confirmed,
                funding_rate, orderbook_imbalance,
                regime, htf_alignment, setup_type, entry_quality, confidence,
                extension_atr, trade_source, entry_type,
                entry_price, sl_price, tp_price, planned_rr, net_rr,
                position_size_usd, leverage,
                skip_reason, entry_reason,
                exit_price, exit_type, gross_pnl_usd, fees_paid_usd,
                net_pnl_usd, net_pnl_pct, slippage_bps, hold_duration_sec,
                max_favorable_excursion, max_adverse_excursion,
                session_hour_local, day_of_week,
                equity_before, equity_after, drawdown_pct,
                api_latency_ms, ai_response_time_ms
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?
            )""",
            (
                self._now_iso(), decision, trade_id, price,
                price - spread / 2 if spread else None,
                price + spread / 2 if spread else None,
                spread,
                ind["atr_1m"], ind["atr_5m"], ind["atr_15m"], ind["volatility_pct"],
                ind["volume_current"], ind["volume_avg_20"], ind["volume_ratio"],
                ind["ema9"], ind["ema21"], ind["ema50"], ind["rsi_14"],
                ind["adx"], ind["chop_index"], ind["bb_width"], ind["macd_hist"],
                ind["ema_slope_1m"], ind["ema_slope_15m"], ind["ema_slope_1h"],
                ind["cvd_value"], int(gate_result.cvd_ok),
                oi_current, oi_change_pct, int(gate_result.oi_ok),
                funding_rate, None,  # orderbook_imbalance — needs L2 data
                regime.regime.value if regime else "UNKNOWN", license.htf_alignment.value,
                license.setup_type.value, license.entry_quality, license.confidence,
                ind.get("extension_atr_raw", 0) or 0,
                "AI", sl_tp.entry_type if sl_tp else None,
                sl_tp.entry_price if sl_tp else None,
                sl_tp.stop_loss if sl_tp else None,
                sl_tp.take_profit if sl_tp else None,
                planned_rr, sl_tp.net_rr if sl_tp else None,
                config.POSITION_SIZE_USD, config.MAX_LEVERAGE,
                "; ".join(gate_result.reasons) if not gate_result.passed else None,
                license.reason if gate_result.passed else None,
                exit_price, exit_type, gross_pnl, fees_paid,
                net_pnl, net_pnl_pct, slippage_bps, hold_duration_sec,
                mfe, mae,
                self._local_hour(), self._day_of_week(),
                equity, equity_after, dd_pct,
                latency_ms, ai_response_ms,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def new_trade_id(self) -> str:
        """Generate a new trade UUID."""
        tid = uuid.uuid4().hex[:16]
        self._active_trade_id = tid
        self._active_entry_time = time.time()
        return tid

    # ------------------------------------------------------------------
    # MFE/MAE
    # ------------------------------------------------------------------

    def start_mfe_mae(self, entry_price: float, direction: str) -> None:
        """Start MFE/MAE tracking for a new position."""
        if config.ENABLE_MFE_MAE_TRACKING:
            self._active_tracker = MFEMAETracker(entry_price, direction)

    def update_mfe_mae(self, current_price: float) -> None:
        """Update MFE/MAE with new tick."""
        if self._active_tracker:
            self._active_tracker.update(current_price)

    def close_mfe_mae(self) -> tuple[float, float]:
        """Close MFE/MAE tracking, return (mfe, mae)."""
        if self._active_tracker:
            result = self._active_tracker.close()
            self._active_tracker = None
            return result
        return 0.0, 0.0

    def get_hold_duration(self) -> int:
        """Get current trade hold duration in seconds."""
        if self._active_entry_time:
            return int(time.time() - self._active_entry_time)
        return 0

    # ------------------------------------------------------------------
    # Market snapshots
    # ------------------------------------------------------------------

    def save_market_snapshot(
        self,
        candles_1m: Sequence[CandleData],
        regime: RegimeState,
        spread: float = 0.0,
        funding_rate: float = 0.0,
        oi_current: float | None = None,
        force: bool = False,
    ) -> bool:
        """Save periodic market snapshot. Returns True if saved."""
        if not config.ENABLE_MARKET_SNAPSHOTS:
            return False

        now = time.time()
        if not force and (now - self._last_market_snapshot) < config.MARKET_SNAPSHOT_INTERVAL_SEC:
            return False

        self._last_market_snapshot = now
        ind = self._compute_indicators(candles_1m)
        price = candles_1m[-1].close if candles_1m else 0

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO market_snapshots (
                timestamp_utc, price, atr_1m, volume_ratio, regime,
                adx, chop_index, cvd_value, oi_current,
                funding_rate, spread_usdt, bb_width
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._now_iso(), price, ind["atr_1m"], ind["volume_ratio"],
                regime.regime.value if regime else "UNKNOWN", ind["adx"], ind["chop_index"],
                ind["cvd_value"], oi_current, funding_rate, spread, ind["bb_width"],
            ),
        )
        conn.commit()
        return True

    # ------------------------------------------------------------------
    # Equity curve
    # ------------------------------------------------------------------

    def save_equity_snapshot(
        self,
        equity: float,
        unrealized_pnl: float = 0.0,
        force: bool = False,
    ) -> bool:
        """Save equity curve point. Returns True if saved."""
        if not config.ENABLE_EQUITY_TRACKING:
            return False

        now = time.time()
        if not force and (now - self._last_equity_snapshot) < config.EQUITY_SNAPSHOT_INTERVAL_SEC:
            return False

        self._last_equity_snapshot = now
        total_equity = equity + unrealized_pnl

        if total_equity > self._peak_equity:
            self._peak_equity = total_equity

        dd_pct = 0.0
        dd_duration = 0
        if self._peak_equity > 0:
            dd_pct = (self._peak_equity - total_equity) / self._peak_equity * 100
            if dd_pct > 0 and self._drawdown_start is None:
                self._drawdown_start = now
            elif dd_pct <= 0:
                self._drawdown_start = None

        if self._drawdown_start:
            dd_duration = int((now - self._drawdown_start) / 60)

        win_rate = (self._total_wins / self._total_trades * 100) if self._total_trades > 0 else 0

        tier = "STANDARD"
        if equity > 10000:
            tier = "HIGH"
        elif equity < 500:
            tier = "LOW"

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO equity_curve (
                timestamp_utc, equity_usdt, unrealized_pnl, total_equity,
                peak_equity, drawdown_pct, drawdown_duration_min,
                total_trades, total_wins, win_rate, total_net_pnl,
                position_size_tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._now_iso(), equity, unrealized_pnl, total_equity,
                self._peak_equity, dd_pct, dd_duration,
                self._total_trades, self._total_wins, win_rate,
                self._total_net_pnl, tier,
            ),
        )
        conn.commit()
        return True

    def record_trade_stats(self, is_win: bool, net_pnl: float) -> None:
        """Update running trade statistics."""
        self._total_trades += 1
        if is_win:
            self._total_wins += 1
        self._total_net_pnl += net_pnl

    # ------------------------------------------------------------------
    # Daily session summary
    # ------------------------------------------------------------------

    def compute_daily_summary(self, date_str: str | None = None) -> dict | None:
        """Compute and persist daily session summary from trade_decisions."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        conn = self._get_conn()

        # Get all decisions for the day
        rows = conn.execute(
            """SELECT decision, net_pnl_usd, fees_paid_usd, gross_pnl_usd,
                      hold_duration_sec, regime, skip_reason, equity_before, equity_after
               FROM trade_decisions WHERE timestamp_utc LIKE ?""",
            (f"{date_str}%",),
        ).fetchall()

        if not rows:
            return None

        trades_taken = 0
        trades_skipped = 0
        wins = 0
        losses = 0
        gross_pnl = 0.0
        total_fees = 0.0
        net_pnl = 0.0
        pnl_list = []
        hold_times = []
        regime_counts: dict[str, int] = {}
        skip_reasons: dict[str, int] = {}
        equity_vals = []
        drawdowns = []

        for row in rows:
            decision, r_net_pnl, r_fees, r_gross, r_hold, r_regime, r_skip, r_eq_before, r_eq_after = row

            if decision.startswith("ENTRY"):
                trades_taken += 1
            elif decision == "SKIP":
                trades_skipped += 1
                if r_skip:
                    for reason in r_skip.split(";"):
                        reason = reason.strip().split(":")[0] if ":" in reason else reason.strip()
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

            if decision.startswith("EXIT") and r_net_pnl is not None:
                pnl_list.append(r_net_pnl)
                net_pnl += r_net_pnl
                if r_gross is not None:
                    gross_pnl += r_gross
                if r_fees is not None:
                    total_fees += r_fees
                if r_net_pnl > 0:
                    wins += 1
                else:
                    losses += 1
                if r_hold is not None:
                    hold_times.append(r_hold)

            if r_regime:
                regime_counts[r_regime] = regime_counts.get(r_regime, 0) + 1
            if r_eq_before is not None:
                equity_vals.append(r_eq_before)
            if r_eq_after is not None:
                equity_vals.append(r_eq_after)

        total_closed = wins + losses
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
        dominant_regime = max(regime_counts, key=regime_counts.get) if regime_counts else None
        best_pnl = max(pnl_list) if pnl_list else 0
        worst_pnl = min(pnl_list) if pnl_list else 0
        eq_start = equity_vals[0] if equity_vals else 0
        eq_end = equity_vals[-1] if equity_vals else 0
        eq_growth = ((eq_end - eq_start) / eq_start * 100) if eq_start > 0 else 0

        conn.execute(
            """INSERT OR REPLACE INTO daily_sessions (
                date, trades_taken, trades_skipped, wins, losses, win_rate,
                gross_pnl, total_fees, net_pnl, max_drawdown_pct,
                best_trade_pnl, worst_trade_pnl, avg_hold_time_sec,
                dominant_regime, equity_start, equity_end, equity_growth_pct,
                skip_reasons_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date_str, trades_taken, trades_skipped, wins, losses, win_rate,
                gross_pnl, total_fees, net_pnl, 0,  # max_drawdown computed elsewhere
                best_pnl, worst_pnl, avg_hold,
                dominant_regime, eq_start, eq_end, eq_growth,
                json.dumps(skip_reasons) if skip_reasons else None,
            ),
        )
        conn.commit()

        return {
            "date": date_str, "trades_taken": trades_taken,
            "trades_skipped": trades_skipped, "wins": wins, "losses": losses,
            "win_rate": win_rate, "net_pnl": net_pnl,
        }

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_table_count(self, table: str) -> int:
        """Get row count for a table."""
        conn = self._get_conn()
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0] if row else 0

    def get_tables(self) -> list[str]:
        """List all tables in the database."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return [r[0] for r in rows]

    def get_columns(self, table: str) -> list[str]:
        """Get column names for a table."""
        conn = self._get_conn()
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]
