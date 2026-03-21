"""
Data Collection Engine (Phase 7).
Records trade decisions, market snapshots, equity curve, and daily sessions.
"""
import logging
import time
from datetime import datetime
from typing import Optional

from .models import Indicators, MarketContext, Position, Side, Trade
from .persistence import BotDatabase

logger = logging.getLogger("data_collector")


class DataCollector:
    """Collects and persists all trading data for analysis."""

    def __init__(self, db: BotDatabase, config=None):
        self.db = db
        self.config = config
        self.last_market_snapshot: float = 0
        self.last_equity_snapshot: float = 0
        self.daily_skips: int = 0
        self.daily_skip_reasons: dict = {}
        self.session_date: str = ""

    def record_entry_decision(
        self,
        decision: str,
        trade_id: str,
        indicators: Indicators,
        market_context: Optional[MarketContext],
        signal_data: dict,
        balance: float,
        drawdown_pct: float = 0,
    ):
        """Record a full snapshot at entry or skip decision."""
        now = datetime.utcnow()
        try:
            local_hour = datetime.now().hour
        except Exception:
            local_hour = now.hour

        data = {
            "timestamp_utc": now.isoformat(),
            "decision": decision,
            "trade_id": trade_id,
            "price": indicators.price,
            "spread_usdt": getattr(market_context, "spread_usdt", 0) if market_context else 0,
            "atr_1m": indicators.atr,
            "volatility_pct": indicators.atr_pct,
            "volume_current": indicators.current_volume,
            "volume_avg_20": indicators.volume_sma,
            "volume_ratio": indicators.volume_ratio,
            "ema9": indicators.ema_fast,
            "ema21": indicators.ema_slow,
            "ema50": indicators.ema_trend,
            "rsi_14": indicators.rsi,
            "adx": indicators.adx,
            "chop_index": indicators.chop_index,
            "bb_width": indicators.bb_width,
            "macd_hist": indicators.macd_histogram,
            "ema_slope_1m": indicators.ema_slope_1m,
            "cvd_value": indicators.cvd_value,
            "cvd_aligned": 1 if signal_data.get("cvd_aligned", False) else 0,
            "oi_current": getattr(market_context, "open_interest", 0) if market_context else 0,
            "oi_change_pct": getattr(market_context, "open_interest_change", 0) if market_context else 0,
            "oi_confirmed": 1 if signal_data.get("oi_confirmed", False) else 0,
            "funding_rate": getattr(market_context, "funding_rate", 0) if market_context else 0,
            "orderbook_imbalance": getattr(market_context, "book_imbalance", 0) if market_context else 0,
            "regime": signal_data.get("regime", "UNKNOWN"),
            "htf_alignment": signal_data.get("htf_alignment", "NEUTRAL"),
            "setup_type": signal_data.get("setup_type", "NONE"),
            "entry_quality": signal_data.get("entry_quality", 0),
            "confidence": signal_data.get("confidence", 0),
            "extension_atr": signal_data.get("extension_atr", 0),
            "trade_source": signal_data.get("trade_source", "AI"),
            "entry_type": signal_data.get("entry_type", "TAKER"),
            "entry_price": signal_data.get("entry_price", indicators.price),
            "sl_price": signal_data.get("sl_price", 0),
            "tp_price": signal_data.get("tp_price", 0),
            "net_rr": signal_data.get("net_rr", 0),
            "position_size_usd": signal_data.get("position_size_usd", 0),
            "leverage": signal_data.get("leverage", 0),
            "skip_reason": signal_data.get("skip_reason", ""),
            "entry_reason": signal_data.get("entry_reason", ""),
            "session_hour_local": local_hour,
            "day_of_week": now.weekday(),
            "equity_before": balance,
            "drawdown_pct": drawdown_pct,
        }

        self.db.save_trade_decision(data)

        if decision == "SKIP":
            self.daily_skips += 1
            reason = signal_data.get("skip_reason", "unknown")
            self.daily_skip_reasons[reason] = self.daily_skip_reasons.get(reason, 0) + 1

    def record_exit(self, trade_id: str, trade: Trade, position: Position):
        """Record exit data for an existing trade decision."""
        exit_data = {
            "exit_price": trade.exit_price,
            "exit_type": trade.exit_type or trade.reason,
            "gross_pnl_usd": trade.gross_pnl,
            "fees_paid_usd": trade.fees_paid_usd,
            "net_pnl_usd": trade.pnl,
            "net_pnl_pct": trade.pnl_pct,
            "hold_duration_sec": trade.hold_duration_sec,
            "max_favorable_excursion": position.max_favorable_excursion,
            "max_adverse_excursion": position.max_adverse_excursion,
            "equity_after": 0,  # filled by agent
        }
        self.db.update_trade_exit(trade_id, exit_data)

    def maybe_save_market_snapshot(
        self,
        indicators: Indicators,
        market_context: Optional[MarketContext],
        interval_sec: int = 300,
    ):
        """Save periodic market state snapshot."""
        now = time.time()
        if now - self.last_market_snapshot < interval_sec:
            return

        self.last_market_snapshot = now
        self.db.save_market_snapshot({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "price": indicators.price,
            "atr_1m": indicators.atr,
            "volume_ratio": indicators.volume_ratio,
            "regime": str(getattr(indicators, "_regime", "UNKNOWN")),
            "adx": indicators.adx,
            "chop_index": indicators.chop_index,
            "cvd_value": indicators.cvd_value,
            "oi_current": getattr(market_context, "open_interest", 0) if market_context else 0,
            "funding_rate": getattr(market_context, "funding_rate", 0) if market_context else 0,
            "spread_usdt": getattr(market_context, "spread_usdt", 0) if market_context else 0,
            "bb_width": indicators.bb_width,
        })

    def maybe_save_equity_snapshot(
        self,
        balance: float,
        unrealized_pnl: float,
        peak_equity: float,
        drawdown_pct: float,
        total_trades: int,
        total_wins: int,
        total_net_pnl: float,
        tier: str,
        interval_sec: int = 900,
        force: bool = False,
    ):
        """Save periodic equity curve snapshot."""
        now = time.time()
        if not force and now - self.last_equity_snapshot < interval_sec:
            return

        self.last_equity_snapshot = now
        win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
        self.db.save_equity_snapshot({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "equity_usdt": balance,
            "unrealized_pnl": unrealized_pnl,
            "total_equity": balance + unrealized_pnl,
            "peak_equity": peak_equity,
            "drawdown_pct": drawdown_pct,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "win_rate": win_rate,
            "total_net_pnl": total_net_pnl,
            "position_size_tier": tier,
        })

    def compute_daily_session(
        self,
        date_str: str,
        trades: list,
        equity_start: float,
        equity_end: float,
    ):
        """Compute and save daily session summary."""
        if not trades:
            return

        wins = sum(1 for t in trades if t.pnl > 0)
        losses = len(trades) - wins
        gross_pnl = sum(t.pnl for t in trades)

        self.db.save_daily_session({
            "date": date_str,
            "trades_taken": len(trades),
            "trades_skipped": self.daily_skips,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / len(trades) * 100) if trades else 0,
            "gross_pnl": gross_pnl,
            "net_pnl": gross_pnl,
            "best_trade_pnl": max(t.pnl for t in trades) if trades else 0,
            "worst_trade_pnl": min(t.pnl for t in trades) if trades else 0,
            "equity_start": equity_start,
            "equity_end": equity_end,
            "equity_growth_pct": ((equity_end - equity_start) / equity_start * 100) if equity_start > 0 else 0,
            "skip_reasons": self.daily_skip_reasons,
        })
