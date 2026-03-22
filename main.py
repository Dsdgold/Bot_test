#!/usr/bin/env python3
"""
BTCUSDT Perpetual Futures Scalping Bot — Main Entry Point.

Integrates: agent → learning journal → self-optimizer → position sizer → order execution.

Usage:
  python main.py              # Run bot (testnet by default)
  python main.py --dashboard  # Run bot + dashboard
  python main.py --dry-run    # Simulate without placing orders
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trading_agent import config
from trading_agent.agent import TradingAgent
from trading_agent.learning_journal import LearningJournal
from trading_agent.log_sanitizer import setup_sanitized_logging
from trading_agent.models import Action, CandleData, Regime
from trading_agent.position_sizer import calculate_position_size
from trading_agent.self_optimizer import SelfOptimizer

logger = logging.getLogger(__name__)

LOOP_INTERVAL_SEC = 60  # 60-second cycle — balanced between cost and responsiveness

# Candle cache to reduce API calls (5m/15m/1h don't change every 30s)
_candle_cache: dict[str, tuple[float, list]] = {}  # interval → (timestamp, candles)
CANDLE_CACHE_TTL = {"5": 60, "15": 180, "60": 600, "1": 0}  # seconds per interval


# ---------------------------------------------------------------------------
# Shared state for dashboard (module-level, thread-safe reads)
# ---------------------------------------------------------------------------
_shared_positions: list[dict] = []  # Updated by bot loop, read by dashboard
_shared_equity: dict = {}  # Updated by bot loop, read by dashboard


def get_shared_positions() -> list[dict]:
    """Return a snapshot of bot's internal positions for the dashboard."""
    return list(_shared_positions)


def get_shared_equity() -> dict:
    """Return bot's equity tracking for the dashboard."""
    return dict(_shared_equity)


def _sync_shared_positions(active_positions: dict, current_price: float = 0) -> None:
    """Update the shared state with current active positions."""
    global _shared_positions
    snapshot = []
    for tid, pos in active_positions.items():
        unrealized = ((current_price - pos.entry_price) if pos.direction == "LONG"
                      else (pos.entry_price - current_price)) * pos.qty_btc if current_price else 0
        snapshot.append({
            "trade_id": tid[:8],
            "side": "Buy" if pos.direction == "LONG" else "Sell",
            "direction": pos.direction,
            "size": pos.qty_btc,
            "entry_price": pos.entry_price,
            "sl_price": pos.sl_price,
            "tp_price": pos.tp_price,
            "unrealised_pnl": round(unrealized, 4),
            "opened_at": pos.opened_at,
        })
    _shared_positions = snapshot


# ---------------------------------------------------------------------------
# Multi-position tracking
# ---------------------------------------------------------------------------

@dataclass
class ActivePosition:
    """Tracks a single virtual position within a potentially aggregated exchange position."""
    trade_id: str
    direction: str
    entry_price: float
    sl_price: float
    tp_price: float
    qty_btc: float
    license: Any = None
    gate_result: Any = None
    regime: Any = None
    sl_tp: Any = None
    opened_at: float = field(default_factory=time.time)
    exchange_sl_set: bool = False  # Track if exchange SL/TP was successfully set


# ---------------------------------------------------------------------------
# Bybit session singleton
# ---------------------------------------------------------------------------

_bybit_session = None


def _get_session():
    """Get or create a persistent Bybit HTTP session."""
    global _bybit_session
    if _bybit_session is None:
        from pybit.unified_trading import HTTP
        _bybit_session = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )
    return _bybit_session


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

def validate_api_keys() -> bool:
    """Check that required API keys are configured."""
    missing = []
    if not config.BYBIT_API_KEY:
        missing.append("BYBIT_API_KEY")
    if not config.BYBIT_API_SECRET:
        missing.append("BYBIT_API_SECRET")
    if not config.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")

    if missing:
        print("=" * 60)
        print(" ONBOARDING — API keys not configured!")
        print("=" * 60)
        print(f"\nMissing: {', '.join(missing)}")
        print("\nSteps:")
        print("  1. Copy .env.example to .env")
        print("  2. Fill in your API keys in .env")
        print("  3. Restart the bot")
        print(f"\n  BYBIT_TESTNET is {'ON' if config.BYBIT_TESTNET else 'OFF'}")
        print("=" * 60)
        return False
    return True


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def fetch_candles(symbol: str, interval: str, limit: int) -> list[CandleData]:
    """Fetch candles from Bybit API with rate limit retry and caching."""
    # Check cache for higher timeframes
    cache_ttl = CANDLE_CACHE_TTL.get(interval, 0)
    if cache_ttl > 0 and interval in _candle_cache:
        cached_time, cached_candles = _candle_cache[interval]
        if (time.time() - cached_time) < cache_ttl and cached_candles:
            return cached_candles

    for attempt in range(3):
        try:
            session = _get_session()
            result = session.get_kline(
                category=config.CATEGORY,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )

            candles = []
            for item in reversed(result["result"]["list"]):
                candles.append(CandleData(
                    timestamp=datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                ))
            # Cache higher timeframes
            if cache_ttl > 0:
                _candle_cache[interval] = (time.time(), candles)
            return candles

        except ImportError:
            logger.warning("pybit not installed — run: pip install pybit")
            return []
        except Exception as e:
            err_str = str(e).lower()
            if "rate limit" in err_str or "10006" in str(e) or "bapi-limit" in err_str:
                wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                logger.warning(f"Rate limit ({interval}) — waiting {wait}s (attempt {attempt+1}/3)")
                await asyncio.sleep(wait)
                continue
            logger.error(f"Failed to fetch candles ({interval}): {e}")
            return []
    return []


async def fetch_market_data() -> dict:
    """Fetch spread, funding rate, OI from Bybit with graceful degradation."""
    data = {"spread": 0.0, "funding_rate": 0.0, "oi_current": None, "oi_previous": None, "latency_ms": 0}
    try:
        session = _get_session()
        start = time.time()

        # Orderbook (spread) — critical, retry once
        ob = None
        for attempt in range(2):
            try:
                ob = session.get_orderbook(category=config.CATEGORY, symbol=config.SYMBOL, limit=1)
                break
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(1)
                else:
                    logger.warning(f"Orderbook fetch failed: {e}")

        latency = (time.time() - start) * 1000
        data["latency_ms"] = latency

        if ob:
            bids = ob["result"]["b"]
            asks = ob["result"]["a"]
            if bids and asks:
                data["spread"] = float(asks[0][0]) - float(bids[0][0])
                data["best_bid"] = float(bids[0][0])
                data["best_ask"] = float(asks[0][0])

        # Funding rate — non-critical, single try
        try:
            tickers = session.get_tickers(category=config.CATEGORY, symbol=config.SYMBOL)
            data["funding_rate"] = float(tickers["result"]["list"][0].get("fundingRate", 0))
        except Exception:
            pass

        # Open Interest — non-critical, single try
        try:
            oi = session.get_open_interest(
                category=config.CATEGORY, symbol=config.SYMBOL,
                intervalTime="5min", limit=2,
            )
            oi_list = oi["result"]["list"]
            if len(oi_list) >= 2:
                data["oi_current"] = float(oi_list[0]["openInterest"])
                data["oi_previous"] = float(oi_list[1]["openInterest"])
        except Exception:
            pass

    except ImportError:
        pass
    except Exception as e:
        logger.error(f"Market data fetch error: {e}")

    return data


async def fetch_account_equity() -> float:
    """Fetch current equity from Bybit."""
    try:
        session = _get_session()
        result = session.get_wallet_balance(accountType="UNIFIED")
        acct_list = result.get("result", {}).get("list", [])
        if acct_list:
            total_eq = acct_list[0].get("totalEquity", "0")
            return float(total_eq)
    except Exception as e:
        logger.warning(f"Failed to fetch equity: {e}")
    return 0.0


# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------

LIMIT_ORDER_TIMEOUT_SEC = 5  # Max wait for limit fill before switching to market (fast scalping)


def _check_order_filled(session, order_id: str) -> str:
    """Check limit order status. Returns 'Filled', 'Open', 'Cancelled', or 'Unknown'."""
    try:
        # Check order history (filled/cancelled orders)
        result = session.get_order_history(
            category=config.CATEGORY, symbol=config.SYMBOL, orderId=order_id
        )
        orders = result.get("result", {}).get("list", [])
        if orders:
            return orders[0].get("orderStatus", "Unknown")

        # Check open orders (still active)
        result = session.get_open_orders(
            category=config.CATEGORY, symbol=config.SYMBOL, orderId=order_id
        )
        orders = result.get("result", {}).get("list", [])
        if orders:
            return orders[0].get("orderStatus", "Open")

        return "Unknown"
    except Exception:
        return "Unknown"


def _has_open_position(session) -> bool:
    """Check if there's already an open position on the exchange."""
    try:
        result = session.get_positions(
            category=config.CATEGORY, symbol=config.SYMBOL
        )
        positions = result.get("result", {}).get("list", [])
        for pos in positions:
            size = float(pos.get("size", "0"))
            if size > 0:
                return True
        return False
    except Exception:
        return False


def _get_exchange_position_size(session) -> tuple[float, str]:
    """Get current exchange position size and side. Returns (size, side) or (0, '')."""
    try:
        result = session.get_positions(
            category=config.CATEGORY, symbol=config.SYMBOL
        )
        positions = result.get("result", {}).get("list", [])
        for pos in positions:
            size = float(pos.get("size", "0"))
            if size > 0:
                return size, pos.get("side", "")
        return 0.0, ""
    except Exception:
        return 0.0, ""


def _sync_positions_with_exchange(
    active_positions: dict[str, ActivePosition],
    price: float,
    agent,
    journal,
    dry_run: bool = False,
) -> tuple[list[str], float]:
    """Sync local positions with exchange. Detect SL/TP hit by exchange.
    Returns (closed_ids, total_net_pnl_from_closures)."""
    if dry_run or not active_positions:
        return [], 0.0

    try:
        session = _get_session()
        exch_size, exch_side = _get_exchange_position_size(session)
    except Exception:
        return [], 0.0

    total_local_qty = sum(p.qty_btc for p in active_positions.values())
    closed_ids = []
    total_pnl = 0.0

    # If exchange has NO position but we have local positions → exchange closed them (SL/TP hit)
    if exch_size == 0 and total_local_qty > 0:
        logger.warning(
            f"EXCHANGE SYNC: No exchange position but {len(active_positions)} local position(s) "
            f"(total {total_local_qty:.3f} BTC) — exchange SL/TP likely hit"
        )
        for tid, pos in list(active_positions.items()):
            # Estimate PnL — we don't know exact exit price, use current price
            if pos.direction == "LONG":
                gross_pnl = (price - pos.entry_price) * pos.qty_btc
            else:
                gross_pnl = (pos.entry_price - price) * pos.qty_btc

            # Check if price hit SL or TP to estimate exit
            hit_sl = (price <= pos.sl_price) if pos.direction == "LONG" else (price >= pos.sl_price)
            hit_tp = (price >= pos.tp_price) if pos.direction == "LONG" else (price <= pos.tp_price)

            if hit_sl:
                exit_price = pos.sl_price
                exit_type = "SL_EXCHANGE"
            elif hit_tp:
                exit_price = pos.tp_price
                exit_type = "TP_EXCHANGE"
            else:
                exit_price = price
                exit_type = "EXCHANGE_CLOSE"

            if pos.direction == "LONG":
                gross_pnl = (exit_price - pos.entry_price) * pos.qty_btc
            else:
                gross_pnl = (pos.entry_price - exit_price) * pos.qty_btc

            entry_notional = pos.entry_price * pos.qty_btc
            exit_notional = exit_price * pos.qty_btc
            fees = entry_notional * config.MAKER_FEE_RATE + exit_notional * config.TAKER_FEE_RATE
            net_pnl = gross_pnl - fees

            logger.info(
                f"EXCHANGE CLOSED [{tid[:8]}]: {exit_type} | {pos.direction} @ {pos.entry_price:.2f} → "
                f"${exit_price:.2f} | PnL=${net_pnl:+.2f}"
            )
            total_pnl += net_pnl
            closed_ids.append(tid)

    return closed_ids, total_pnl


def _cancel_order(session, order_id: str) -> bool:
    """Cancel an open limit order."""
    try:
        result = session.cancel_order(
            category=config.CATEGORY, symbol=config.SYMBOL, orderId=order_id
        )
        return result.get("retCode") == 0
    except Exception as e:
        logger.warning(f"Cancel order error: {e}")
        return False


def place_order(direction: str, size_usd: float, price: float) -> dict | None:
    """
    Place an order on Bybit. Tries limit (PostOnly) with fill timeout,
    falls back to market if limit doesn't fill within LIMIT_ORDER_TIMEOUT_SEC.
    direction: 'LONG' or 'SHORT'
    Returns order result dict or None on failure.
    """
    try:
        session = _get_session()
        side = "Buy" if direction == "LONG" else "Sell"
        qty = round(size_usd / price, 3)  # BTC contracts

        if qty <= 0:
            logger.warning(f"Position size too small: ${size_usd:.2f}")
            return None

        # Try limit order first (PostOnly = maker fees only, no taker)
        if config.PREFER_POST_ONLY_ENTRIES:
            # Offset price closer to market for faster fills
            # 0.9999 = $6.8 offset was too tight, 0.9997 = ~$20 gives better fill chance
            if direction == "LONG":
                limit_price = round(price * 0.9997, 2)  # ~$20 below market
            else:
                limit_price = round(price * 1.0003, 2)  # ~$20 above market

            try:
                result = session.place_order(
                    category=config.CATEGORY,
                    symbol=config.SYMBOL,
                    side=side,
                    orderType="Limit",
                    qty=str(qty),
                    price=str(limit_price),
                    timeInForce="PostOnly",
                )

                if result.get("retCode") == 0:
                    order_id = result["result"]["orderId"]
                    logger.info(f"LIMIT ORDER: {side} {qty} BTC @ ${limit_price:.2f} (PostOnly) | id={order_id}")

                    # Wait for fill with timeout
                    status = "New"
                    start = time.time()
                    while (time.time() - start) < LIMIT_ORDER_TIMEOUT_SEC:
                        time.sleep(2)
                        status = _check_order_filled(session, order_id)
                        if status == "Filled":
                            logger.info(f"LIMIT FILLED: {side} {qty} BTC @ ${limit_price:.2f} (maker fee)")
                            result["result"]["_filled_as"] = "MAKER"
                            return result["result"]
                        elif status == "Cancelled":
                            logger.info("Limit order was cancelled by exchange — falling back to market")
                            break

                    # Timeout — cancel and fall back to market
                    if status not in ("Filled", "Cancelled"):
                        logger.warning(
                            f"Limit order not filled in {LIMIT_ORDER_TIMEOUT_SEC}s — "
                            f"cancelling and using market"
                        )
                    cancel_ok = _cancel_order(session, order_id)
                    time.sleep(0.5)

                    # CRITICAL: Check if position exists before market fallback
                    # Cancel may fail if order was filled between last check and cancel
                    if not cancel_ok and _has_open_position(session):
                        logger.info(
                            "Limit order was filled (cancel failed + position exists) — "
                            "skipping market fallback"
                        )
                        result["result"]["_filled_as"] = "MAKER"
                        return result["result"]
                else:
                    logger.warning(f"Limit order rejected: {result.get('retMsg')} — falling back to market")
            except Exception as e:
                logger.warning(f"Limit order failed: {e} — falling back to market")

        # Fallback: market order (guaranteed fill)
        result = session.place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="GTC",
        )

        if result.get("retCode") == 0:
            order_id = result["result"]["orderId"]
            logger.info(f"MARKET ORDER: {side} {qty} BTC @ market | order_id={order_id}")
            result["result"]["_filled_as"] = "TAKER"
            return result["result"]
        else:
            logger.error(f"ORDER FAILED: {result.get('retMsg', 'Unknown error')}")
            return None

    except Exception as e:
        logger.error(f"Order execution error: {e}")
        return None


def set_stop_loss_take_profit(sl_price: float, tp_price: float, direction: str) -> bool:
    """Set SL/TP on the active position via trading stop."""
    try:
        session = _get_session()
        result = session.set_trading_stop(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            stopLoss=str(round(sl_price, 2)),
            takeProfit=str(round(tp_price, 2)),
            positionIdx=0,
        )
        if result.get("retCode") == 0:
            logger.info(f"SL/TP SET: SL=${sl_price:.2f} TP=${tp_price:.2f}")
            return True
        else:
            logger.error(f"SL/TP FAILED: {result.get('retMsg')}")
            return False
    except Exception as e:
        logger.error(f"SL/TP error: {e}")
        return False


def close_all_positions() -> bool:
    """Close all open positions with market order."""
    try:
        session = _get_session()
        positions = session.get_positions(category=config.CATEGORY, symbol=config.SYMBOL)
        pos_list = positions.get("result", {}).get("list", [])

        for pos in pos_list:
            size = float(pos.get("size", 0))
            if size <= 0:
                continue
            side = "Sell" if pos["side"] == "Buy" else "Buy"
            session.place_order(
                category=config.CATEGORY,
                symbol=config.SYMBOL,
                side=side,
                orderType="Market",
                qty=str(size),
                timeInForce="GTC",
                reduceOnly=True,
            )
            logger.info(f"CLOSED: {pos['side']} {size} BTC")
        return True
    except Exception as e:
        logger.error(f"Close positions error: {e}")
        return False


def close_partial_position(direction: str, qty_btc: float) -> bool:
    """Close a partial position (reduceOnly) for multi-position management."""
    try:
        session = _get_session()
        # Check if position still exists on exchange before trying to close
        pos_info = session.get_positions(
            category=config.CATEGORY, symbol=config.SYMBOL
        )
        pos_list = pos_info.get("result", {}).get("list", [])
        has_position = any(
            float(p.get("size", 0)) > 0 for p in pos_list
        )
        if not has_position:
            logger.info("Position already closed by exchange (SL/TP hit)")
            return True

        side = "Sell" if direction == "LONG" else "Buy"
        result = session.place_order(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            side=side,
            orderType="Market",
            qty=str(qty_btc),
            timeInForce="GTC",
            reduceOnly=True,
        )
        if result.get("retCode") == 0:
            logger.info(f"PARTIAL CLOSE: {side} {qty_btc} BTC (reduceOnly)")
            return True
        else:
            logger.error(f"PARTIAL CLOSE FAILED: {result.get('retMsg')}")
            return False
    except Exception as e:
        logger.error(f"Partial close error: {e}")
        return False


def update_exchange_sl(positions: dict[str, ActivePosition]) -> bool:
    """Update exchange SL to the widest stop among active positions (safety net).
    Returns True if SL was successfully set on exchange."""
    if not positions:
        return False
    first = next(iter(positions.values()))
    direction = first.direction
    if direction == "LONG":
        widest_sl = min(p.sl_price for p in positions.values())
    else:
        widest_sl = max(p.sl_price for p in positions.values())
    try:
        session = _get_session()
        session.set_trading_stop(
            category=config.CATEGORY,
            symbol=config.SYMBOL,
            stopLoss=str(round(widest_sl, 2)),
            takeProfit="0",  # we manage TP internally
            positionIdx=0,
        )
        logger.info(f"Exchange safety SL updated: ${widest_sl:.2f}")
        # Mark all positions as having exchange SL set
        for p in positions.values():
            p.exchange_sl_set = True
        return True
    except Exception as e:
        err_str = str(e)
        if "34040" in err_str or "not modified" in err_str.lower():
            logger.debug(f"Exchange SL unchanged (same value): ${widest_sl:.2f}")
            for p in positions.values():
                p.exchange_sl_set = True
            return True
        elif "10001" in err_str or "zero position" in err_str.lower():
            # Limit order not filled yet — will retry in monitoring loop
            logger.warning(f"Exchange SL deferred: position not yet filled (limit order pending)")
            return False
        else:
            logger.error(f"Exchange SL update error: {e}")
            return False


# ---------------------------------------------------------------------------
# Skip tracking for journal
# ---------------------------------------------------------------------------

class SkipTracker:
    """Track skip reasons for periodic journal review."""

    def __init__(self):
        self.total_decisions = 0
        self.skip_count = 0
        self.reasons: dict[str, int] = {}

    def record_skip(self, reasons: list[str]):
        self.total_decisions += 1
        self.skip_count += 1
        for r in reasons:
            key = r.split(":")[0].strip() if ":" in r else r[:40]
            self.reasons[key] = self.reasons.get(key, 0) + 1

    def record_trade(self):
        self.total_decisions += 1

    def get_top_reasons(self, n: int = 5) -> dict[str, int]:
        return dict(sorted(self.reasons.items(), key=lambda x: -x[1])[:n])

    def reset(self):
        self.total_decisions = 0
        self.skip_count = 0
        self.reasons.clear()


class PaperTradeTracker:
    """Track 'what if I had traded?' simulations for every signal."""

    MAX_PENDING = 50     # Max open paper trades in memory
    MAX_COMPLETED = 200  # Max completed paper trades in memory

    def __init__(self):
        self.pending: list[dict] = []  # Open paper trades
        self.completed: list[dict] = []
        self.total_paper_pnl = 0.0
        self.paper_wins = 0
        self.paper_losses = 0

    def open_paper_trade(self, direction: str, entry_price: float,
                         sl_price: float, tp_price: float,
                         confidence: int, quality: int, regime: str,
                         was_blocked: bool, block_reason: str = ""):
        """Record a hypothetical trade entry."""
        self.pending.append({
            "direction": direction,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "confidence": confidence,
            "quality": quality,
            "regime": regime,
            "was_blocked": was_blocked,
            "block_reason": block_reason,
            "open_time": time.time(),
            "mfe": 0.0,
            "mae": 0.0,
        })

    def update_prices(self, current_price: float) -> list[dict]:
        """Update all pending paper trades with current price. Returns closed trades."""
        closed = []
        still_open = []

        for pt in self.pending:
            # Update MFE/MAE
            if pt["direction"] == "LONG":
                unrealized = current_price - pt["entry_price"]
            else:
                unrealized = pt["entry_price"] - current_price

            if unrealized > pt["mfe"]:
                pt["mfe"] = unrealized
            if unrealized < 0 and abs(unrealized) > pt["mae"]:
                pt["mae"] = abs(unrealized)

            # Check SL/TP hit
            hit_tp = False
            hit_sl = False
            if pt["direction"] == "LONG":
                hit_tp = current_price >= pt["tp_price"]
                hit_sl = current_price <= pt["sl_price"]
            else:
                hit_tp = current_price <= pt["tp_price"]
                hit_sl = current_price >= pt["sl_price"]

            # Timeout after 10 min (scalping)
            timed_out = (time.time() - pt["open_time"]) > 600

            if hit_tp or hit_sl or timed_out:
                if hit_tp:
                    pt["exit_type"] = "TP"
                    pt["pnl"] = abs(pt["tp_price"] - pt["entry_price"])
                elif hit_sl:
                    pt["exit_type"] = "SL"
                    pt["pnl"] = -abs(pt["sl_price"] - pt["entry_price"])
                else:
                    pt["exit_type"] = "TIMEOUT"
                    pt["pnl"] = unrealized

                pt["hold_sec"] = int(time.time() - pt["open_time"])
                pt["exit_price"] = current_price
                self.total_paper_pnl += pt["pnl"]
                if pt["pnl"] > 0:
                    self.paper_wins += 1
                else:
                    self.paper_losses += 1
                closed.append(pt)
                self.completed.append(pt)
            else:
                still_open.append(pt)

        self.pending = still_open[-self.MAX_PENDING:]  # Cap memory
        # Cap completed list
        if len(self.completed) > self.MAX_COMPLETED:
            self.completed = self.completed[-self.MAX_COMPLETED:]
        return closed

    def get_summary(self) -> str:
        total = self.paper_wins + self.paper_losses
        wr = (self.paper_wins / total * 100) if total > 0 else 0
        return (
            f"Paper trades: {total} ({self.paper_wins}W/{self.paper_losses}L) "
            f"WR={wr:.0f}% PnL=${self.total_paper_pnl:.2f}"
        )


# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------

async def run_bot(dry_run: bool = False):
    """Main bot loop with full integration."""
    setup_sanitized_logging()

    if not validate_api_keys():
        return

    # Initialize all components
    agent = TradingAgent()
    journal = LearningJournal()
    optimizer = SelfOptimizer()
    skip_tracker = SkipTracker()
    paper_tracker = PaperTradeTracker()

    # Strategy evolution — dynamic prompt management
    from trading_agent.strategy_evolution import StrategyEvolution, DEFAULT_STRATEGY
    from trading_agent.ai_brain import set_system_prompt
    strategy_evo = StrategyEvolution()
    # Always use aggressive default strategy on startup
    # Strategy evolution tends to make AI too conservative (adding volume/ADX rules)
    set_system_prompt(DEFAULT_STRATEGY)
    logger.info(f"Strategy loaded: v{strategy_evo.current_version} (forced aggressive default)")

    # Set leverage on Bybit
    # Fetch initial equity from Bybit FIRST (needed for dynamic leverage)
    initial_equity = await fetch_account_equity()
    if initial_equity > 0:
        agent.equity = initial_equity

    # Set leverage based on equity tier
    if not dry_run:
        from trading_agent.position_sizer import get_max_leverage_for_equity
        effective_lev = get_max_leverage_for_equity(agent.equity)
        try:
            session = _get_session()
            session.set_leverage(
                category=config.CATEGORY,
                symbol=config.SYMBOL,
                buyLeverage=str(effective_lev),
                sellLeverage=str(effective_lev),
            )
            logger.info(
                f"Leverage set to {effective_lev}x on Bybit "
                f"(equity=${agent.equity:.2f}, tier={'MICRO' if agent.equity < config.MICRO_EQUITY_THRESHOLD else 'STANDARD'})"
            )
        except Exception as e:
            if "11043" in str(e):
                logger.info(f"Leverage already set to {effective_lev}x (or position open)")
            else:
                logger.warning(f"Could not set leverage: {e}")
        logger.info(f"Account equity: ${initial_equity:.2f}")
    else:
        agent.equity = config.POSITION_SIZE_USD * 10
        logger.warning(f"Could not fetch equity, using default: ${agent.equity:.2f}")

    peak_equity = agent.equity
    daily_pnl = 0.0
    weekly_pnl = 0.0
    consecutive_losses = 0
    last_regime = ""
    last_regime_time = time.time()
    last_regime_trades = 0
    last_tuning_cycle = time.time()
    last_meta_learning = time.time()
    last_trade_time = time.time()
    relax_level = 0  # 0=normal, 1=relaxed, 2=very relaxed, 3=ultra relaxed
    current_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current_week = datetime.now(timezone.utc).isocalendar()[1]

    # Active positions (multi-position tracking)
    active_positions: dict[str, ActivePosition] = {}  # trade_id -> ActivePosition

    # ── Crash recovery: detect orphaned exchange positions on startup ──
    if not dry_run:
        try:
            session = _get_session()
            exch_size, exch_side = _get_exchange_position_size(session)
            if exch_size > 0:
                # We have a position on exchange but no local tracking
                pos_info = session.get_positions(
                    category=config.CATEGORY, symbol=config.SYMBOL
                )
                pos_data = pos_info.get("result", {}).get("list", [])
                for pd in pos_data:
                    psize = float(pd.get("size", "0"))
                    if psize <= 0:
                        continue
                    direction = "LONG" if pd.get("side") == "Buy" else "SHORT"
                    entry_p = float(pd.get("avgPrice", "0") or pd.get("entryPrice", "0"))
                    # Set conservative SL/TP for orphaned position
                    if direction == "LONG":
                        sl_p = entry_p * (1 - config.MAX_SL_PCT / 100)
                        tp_p = entry_p * (1 + config.MIN_TP_PCT / 100)
                    else:
                        sl_p = entry_p * (1 + config.MAX_SL_PCT / 100)
                        tp_p = entry_p * (1 - config.MIN_TP_PCT / 100)

                    trade_id = str(uuid.uuid4())
                    recovered_pos = ActivePosition(
                        trade_id=trade_id,
                        direction=direction,
                        entry_price=entry_p,
                        sl_price=sl_p,
                        tp_price=tp_p,
                        qty_btc=psize,
                    )
                    active_positions[trade_id] = recovered_pos
                    logger.warning(
                        f"CRASH RECOVERY: Found orphaned {direction} position "
                        f"{psize} BTC @ ${entry_p:.2f} — tracking as [{trade_id[:8]}] "
                        f"SL=${sl_p:.2f} TP=${tp_p:.2f}"
                    )
                if active_positions:
                    sl_set = False
                    for attempt in range(3):
                        if update_exchange_sl(active_positions):
                            sl_set = True
                            break
                        logger.warning(f"Crash recovery SL attempt {attempt + 1}/3 failed")
                        await asyncio.sleep(2 ** attempt)
                    if not sl_set:
                        logger.error(
                            "CRITICAL: Crash recovery SL failed — closing orphaned positions"
                        )
                        close_all_positions()
                        active_positions.clear()
        except Exception as e:
            logger.warning(f"Crash recovery check failed: {e}")

    logger.info("=" * 60)
    logger.info(f" BTCUSDT Scalping Bot — {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f" Testnet: {config.BYBIT_TESTNET}")
    logger.info(f" Equity: ${agent.equity:.2f}")
    logger.info(f" MIN_CONFIDENCE: {config.MIN_CONFIDENCE}")
    logger.info(f" TRADE_QUALITY_MIN: {config.TRADE_QUALITY_MIN}")
    logger.info(f" REGIME_FILTER: {config.REGIME_FILTER_ENABLED}")
    logger.info(f" FALLBACK_OVERRIDE: {config.ENABLE_FALLBACK_OVERRIDE}")
    logger.info(f" AUTONOMOUS_TUNING: {config.AUTONOMOUS_TUNING_ENABLED}")
    logger.info(f" LEARNING_JOURNAL: {config.LEARNING_JOURNAL_ENABLED}")
    logger.info(f" MAX_OPEN_POSITIONS: {config.MAX_OPEN_POSITIONS}")
    logger.info("=" * 60)

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        logger.info("Shutdown signal received")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cycle = 0
    while running:
        cycle += 1
        cycle_start = time.time()

        try:
            # ── Day/week reset ──
            now_utc = datetime.now(timezone.utc)
            today = now_utc.strftime("%Y-%m-%d")
            week = now_utc.isocalendar()[1]
            if today != current_day:
                logger.info(f"Day rollover: {current_day} → {today} | PnL: ${daily_pnl:+.2f}")
                daily_pnl = 0.0
                current_day = today
            if week != current_week:
                logger.info(f"Week rollover | PnL: ${weekly_pnl:+.2f}")
                weekly_pnl = 0.0
                current_week = week

            # ── Fetch data (with rate limit spacing) ──
            candles_1m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_1M, 100)
            await asyncio.sleep(2.0)
            candles_5m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_5M, 50)
            await asyncio.sleep(2.0)
            candles_15m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_15M, 50)
            await asyncio.sleep(2.0)
            candles_1h = await fetch_candles(config.SYMBOL, config.TIMEFRAME_1H, 30)

            if not candles_1m:
                logger.warning(f"Cycle {cycle}: No candle data — skipping")
                await asyncio.sleep(LOOP_INTERVAL_SEC)
                continue

            market = await fetch_market_data()
            price = candles_1m[-1].close

            # ── Update paper trades with current price ──
            paper_closed = paper_tracker.update_prices(price)
            for pt in paper_closed:
                won = "WIN" if pt["pnl"] > 0 else "LOSS"
                blocked = " (was BLOCKED)" if pt["was_blocked"] else ""
                rr = abs(pt['mfe'] / pt['mae']) if pt['mae'] > 0 else 0
                journal._insert(
                    entry_type="PAPER_TRADE",
                    trigger=f"PAPER {pt['direction']} {pt['exit_type']}: ${pt['pnl']:+.1f}{blocked}",
                    observation=(
                        f"WHAT-IF {pt['direction']}{blocked}: entry=${pt['entry_price']:.0f} "
                        f"exit=${pt['exit_price']:.0f} → {won} ${pt['pnl']:+.1f} "
                        f"in {pt['hold_sec']}s | MFE=${pt['mfe']:.1f} MAE=${pt['mae']:.1f} R:R={rr:.1f} | "
                        f"conf={pt['confidence']} quality={pt['quality']} regime={pt['regime']}"
                    ),
                    conclusion=(
                        f"{'Would have won' if pt['pnl'] > 0 else 'Saved money by skipping'}"
                        f"{' — filter was correct' if pt['pnl'] <= 0 and pt['was_blocked'] else ''}"
                        f"{' — MISSED OPPORTUNITY' if pt['pnl'] > 0 and pt['was_blocked'] else ''}"
                    ),
                    confidence=80,
                    suggested_action=f"Block reason: {pt['block_reason']}" if pt['was_blocked'] else "",
                )
                logger.info(
                    f"PAPER {won}: {pt['direction']} ${pt['pnl']:+.1f} "
                    f"({pt['exit_type']} in {pt['hold_sec']}s){blocked} | "
                    f"{paper_tracker.get_summary()}"
                )

            # ── Update MFE/MAE for active positions ──
            if active_positions:
                agent.update_price_tick(price)

            # ── Exchange sync: detect SL/TP hit by exchange ──
            if active_positions and not dry_run:
                sync_closed, sync_pnl = _sync_positions_with_exchange(
                    active_positions, price, agent, journal, dry_run
                )
                if sync_closed:
                    for tid in sync_closed:
                        pos = active_positions.pop(tid)
                        # Record in agent for stats
                        if pos.license and pos.gate_result:
                            exit_type = "SL" if sync_pnl < 0 else "TP"
                            agent.close_trade(
                                pos.license, pos.gate_result, pos.regime,
                                candles_1m, price, exit_type,
                                sync_pnl, 0, sync_pnl,  # fees already included
                                sl_tp=pos.sl_tp,
                            )
                    daily_pnl += sync_pnl
                    weekly_pnl += sync_pnl
                    if sync_pnl < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    last_trade_time = time.time()
                    relax_level = 0
                    _sync_shared_positions(active_positions, price)
                    logger.info(
                        f"EXCHANGE SYNC complete: {len(sync_closed)} position(s) closed, "
                        f"PnL=${sync_pnl:+.2f}, Equity=${agent.equity:.2f}"
                    )

            # ── Trailing Stop Loss: lock in profits progressively ──
            # When unrealized profit reaches a threshold, move SL to lock partial profit.
            # E.g. $1.50 profit → SL at +$0.50, $3.00 profit → SL at +$1.00, etc.
            TRAIL_TIERS = [
                (0.30, 0.10),  # $0.30 profit → lock $0.10
                (0.50, 0.20),  # $0.50 profit → lock $0.20
                (0.80, 0.40),  # $0.80 profit → lock $0.40
                (1.20, 0.70),  # $1.20 profit → lock $0.70
                (2.00, 1.30),  # $2.00 profit → lock $1.30
                (3.00, 2.20),  # $3.00 profit → lock $2.20
            ]
            trail_updated = False
            for tid, pos in list(active_positions.items()):
                if pos.direction == "LONG":
                    unrealized_usd = (price - pos.entry_price) * pos.qty_btc
                else:
                    unrealized_usd = (pos.entry_price - price) * pos.qty_btc

                if unrealized_usd <= 0:
                    continue  # Only trail when in profit

                # Find the best matching tier
                best_lock = None
                for threshold, lock_amount in reversed(TRAIL_TIERS):
                    if unrealized_usd >= threshold:
                        best_lock = lock_amount
                        break

                if best_lock is None:
                    continue

                # Calculate new SL price that locks in best_lock profit
                lock_per_btc = best_lock / pos.qty_btc
                if pos.direction == "LONG":
                    new_sl = pos.entry_price + lock_per_btc
                    # Only move SL up, never down
                    if new_sl > pos.sl_price:
                        old_sl = pos.sl_price
                        pos.sl_price = round(new_sl, 2)
                        trail_updated = True
                        logger.info(
                            f"TRAILING SL [{tid[:8]}]: {pos.direction} uPnL=${unrealized_usd:+.2f} → "
                            f"SL moved {old_sl:.2f} → {pos.sl_price:.2f} (locking ${best_lock:.2f})"
                        )
                else:
                    new_sl = pos.entry_price - lock_per_btc
                    # Only move SL down (tighter), never up
                    if new_sl < pos.sl_price:
                        old_sl = pos.sl_price
                        pos.sl_price = round(new_sl, 2)
                        trail_updated = True
                        logger.info(
                            f"TRAILING SL [{tid[:8]}]: {pos.direction} uPnL=${unrealized_usd:+.2f} → "
                            f"SL moved {old_sl:.2f} → {pos.sl_price:.2f} (locking ${best_lock:.2f})"
                        )

            # Update exchange SL if trailing moved any stops
            if trail_updated and active_positions and not dry_run:
                update_exchange_sl(active_positions)

            # ── Check each active position for SL/TP hit or timeout ──
            MAX_HOLD_SEC = 900  # 15 min — give trades time to reach TP
            closed_ids: list[str] = []
            for tid, pos in list(active_positions.items()):
                hit_sl = (price <= pos.sl_price) if pos.direction == "LONG" else (price >= pos.sl_price)
                hit_tp = (price >= pos.tp_price) if pos.direction == "LONG" else (price <= pos.tp_price)
                hold_time = time.time() - pos.opened_at
                hit_timeout = hold_time >= MAX_HOLD_SEC

                if not hit_sl and not hit_tp and not hit_timeout:
                    continue

                # Position hit SL, TP, or timeout — close it
                if hit_sl:
                    exit_price = pos.sl_price
                    exit_type = "SL"
                elif hit_tp:
                    exit_price = pos.tp_price
                    exit_type = "TP"
                else:
                    # Smart timeout: check if we're near breakeven or profitable
                    if pos.direction == "LONG":
                        unrealized = (price - pos.entry_price) * pos.qty_btc
                    else:
                        unrealized = (pos.entry_price - price) * pos.qty_btc
                    est_fees = (pos.entry_price + price) * pos.qty_btc * config.TAKER_FEE_RATE
                    net_if_close = unrealized - est_fees

                    # If deeply underwater (> $0.30 loss), extend timeout by 5 min
                    # to give one more chance before closing at full loss
                    if net_if_close < -0.30 and hold_time < MAX_HOLD_SEC + 300:
                        continue  # Skip close, give 5 more minutes

                    exit_price = price
                    exit_type = "TIMEOUT"
                    logger.info(
                        f"TIMEOUT [{tid[:8]}]: {pos.direction} held {int(hold_time)}s "
                        f"net≈${net_if_close:+.2f} — force closing at market"
                    )

                if pos.direction == "LONG":
                    gross_pnl = (exit_price - pos.entry_price) * pos.qty_btc
                else:
                    gross_pnl = (pos.entry_price - exit_price) * pos.qty_btc

                # Fees based on position notional value (not PnL!)
                entry_notional = pos.entry_price * pos.qty_btc
                exit_notional = exit_price * pos.qty_btc
                entry_fee = entry_notional * config.MAKER_FEE_RATE  # Entry as maker
                exit_fee = exit_notional * config.TAKER_FEE_RATE    # Exit as taker (SL/TP = market)
                fees = entry_fee + exit_fee
                net_pnl = gross_pnl - fees
                is_win = net_pnl > 0

                # Close partial position on exchange with verification
                if not dry_run:
                    close_ok = False
                    for close_attempt in range(3):
                        try:
                            if close_partial_position(pos.direction, pos.qty_btc):
                                close_ok = True
                                break
                        except Exception:
                            pass
                        if close_attempt < 2:
                            await asyncio.sleep(1)
                    if not close_ok:
                        logger.warning(
                            f"Position [{tid[:8]}] close failed after 3 attempts — "
                            "exchange SL should protect"
                        )

                # Record close
                if pos.license and pos.gate_result:
                    agent.close_trade(
                        pos.license, pos.gate_result, pos.regime,
                        candles_1m, exit_price, exit_type,
                        gross_pnl, fees, net_pnl,
                        sl_tp=pos.sl_tp,
                    )

                # Update state
                agent.equity += net_pnl
                daily_pnl += net_pnl
                weekly_pnl += net_pnl
                if agent.equity > peak_equity:
                    peak_equity = agent.equity

                if is_win:
                    consecutive_losses = 0
                else:
                    consecutive_losses += 1

                optimizer.increment_trade_counter()

                # ── Learning Journal: LLM-powered post-trade reflection ──
                mfe, mae = agent.data_collector.close_mfe_mae()
                hold_duration = agent.data_collector.get_hold_duration()
                await journal.llm_post_trade_reflection(
                    trade_id=tid,
                    is_win=is_win,
                    net_pnl=net_pnl,
                    entry_quality=pos.license.entry_quality if pos.license else 0,
                    confidence=pos.license.confidence if pos.license else 0,
                    regime=pos.license.regime.value if pos.license else "UNKNOWN",
                    setup_type=pos.license.setup_type.value if pos.license else "NONE",
                    mfe=mfe,
                    mae=mae,
                    hold_sec=hold_duration,
                )

                # Deeper loss reflection
                if not is_win:
                    sl_dist = abs(pos.entry_price - pos.sl_price) if pos.sl_price else 0
                    mae_exceeded = mae > sl_dist * 1.1 if sl_dist > 0 else True
                    journal.record_post_loss_reflection(
                        trade_id=tid,
                        net_pnl=net_pnl,
                        recent_losses=consecutive_losses,
                        regime=pos.license.regime.value if pos.license else "UNKNOWN",
                        mae_exceeded_sl=mae_exceeded,
                    )

                logger.info(
                    f"TRADE CLOSED: {exit_type} | {pos.direction} @ {pos.entry_price:.2f} | "
                    f"PnL=${net_pnl:+.2f} | Equity=${agent.equity:.2f} | "
                    f"Positions remaining: {len(active_positions) - len(closed_ids) - 1}"
                )
                closed_ids.append(tid)

            # Remove closed positions
            for tid in closed_ids:
                del active_positions[tid]
            if closed_ids:
                last_trade_time = time.time()  # Reset relax timer on close too
                relax_level = 0
                _sync_shared_positions(active_positions, price)  # Update after closes (may be empty now)

            # Update exchange safety SL if positions remain
            if closed_ids and active_positions and not dry_run:
                update_exchange_sl(active_positions)

            # ── Sync shared state for dashboard ──
            dd_pct_now = ((peak_equity - agent.equity) / peak_equity * 100) if peak_equity > 0 else 0
            _dc = agent.data_collector
            _tw = getattr(_dc, '_total_wins', 0)
            _tt = getattr(_dc, '_total_trades', 0)
            _shared_equity.update({
                "equity": round(agent.equity, 2),
                "peak_equity": round(peak_equity, 2),
                "dd_pct": round(dd_pct_now, 2),
                "daily_pnl": round(daily_pnl, 2),
                "total_trades": _tt,
                "wins": _tw,
                "losses": _tt - _tw,
                "win_rate": round(_tw / _tt * 100, 1) if _tt > 0 else 0,
                "consecutive_losses": consecutive_losses,
            })
            if active_positions:
                _sync_shared_positions(active_positions, price)

            # ── Hard equity floor & daily loss enforcement ──
            if active_positions and not dry_run:
                if agent.equity <= config.EQUITY_FLOOR_USDT:
                    logger.error(
                        f"EQUITY FLOOR BREACH: ${agent.equity:.2f} <= "
                        f"${config.EQUITY_FLOOR_USDT} — closing ALL positions"
                    )
                    close_all_positions()
                    active_positions.clear()
                    running = False
                    continue
                daily_loss_pct = abs(daily_pnl / peak_equity * 100) if daily_pnl < 0 and peak_equity > 0 else 0
                if daily_loss_pct >= config.DAILY_MAX_LOSS_PCT:
                    logger.error(
                        f"DAILY LOSS LIMIT: {daily_loss_pct:.1f}% >= "
                        f"{config.DAILY_MAX_LOSS_PCT}% — closing ALL positions"
                    )
                    close_all_positions()
                    active_positions.clear()
                    running = False
                    continue

            if active_positions and len(active_positions) >= config.MAX_OPEN_POSITIONS:
                # Retry exchange SL if limit order wasn't filled when we first tried
                any_unprotected = any(not p.exchange_sl_set for p in active_positions.values())
                if any_unprotected:
                    sl_ok = update_exchange_sl(active_positions)
                    if sl_ok:
                        logger.info("Exchange SL set after limit order fill")

                for tid, pos in active_positions.items():
                    unrealized = ((price - pos.entry_price) if pos.direction == "LONG"
                                  else (pos.entry_price - price)) * pos.qty_btc
                    sl_status = "" if pos.exchange_sl_set else " [!NO EXCHANGE SL!]"
                    logger.info(
                        f"Position [{tid[:8]}]: {pos.direction} @ {pos.entry_price:.2f} | "
                        f"SL={pos.sl_price:.2f} TP={pos.tp_price:.2f} | "
                        f"uPnL=${unrealized:+.2f} — monitoring...{sl_status}"
                    )
                if len(active_positions) >= config.MAX_OPEN_POSITIONS:
                    await asyncio.sleep(max(0, LOOP_INTERVAL_SEC - (time.time() - cycle_start)))
                    continue

            # ── Auto-relax: loosen filters if no trades for too long ──
            # SAFETY: Never re-enable FALLBACK_OVERRIDE or lower critical thresholds
            # Only relax non-critical filters (candle confirm, CVD, OI)
            mins_since_trade = (time.time() - last_trade_time) / 60
            new_relax = 0
            if mins_since_trade > 10:
                new_relax = 2  # relaxed (non-critical only)
            elif mins_since_trade > 5:
                new_relax = 1  # slightly relaxed

            if new_relax != relax_level:
                relax_level = new_relax
                if relax_level == 1:
                    config.CANDLE_CLOSE_CONFIRMATION = False
                    logger.info("AUTO-RELAX L1 (5min no trade): disabled candle confirm")
                elif relax_level == 2:
                    config.CANDLE_CLOSE_CONFIRMATION = False
                    config.REQUIRE_CVD_ALIGNMENT = False
                    config.REQUIRE_OI_CONFIRMATION = False
                    logger.info("AUTO-RELAX L2 (10min): disabled candle/CVD/OI filters")
                elif relax_level == 0:
                    # Reset to defaults after a trade
                    config.CANDLE_CLOSE_CONFIRMATION = True
                    config.REQUIRE_CVD_ALIGNMENT = True
                    config.REQUIRE_OI_CONFIRMATION = True
                    logger.info("FILTERS RESET to normal after trade")

            # ── Get journal insights for AI memory ──
            journal_entries = journal.get_recent_entries(limit=10)

            # ── Evaluate market (with journal context) ──
            license_result, gate_result, sl_tp, regime_state, vol_ratio = await agent.evaluate_market(
                candles_1m=candles_1m,
                candles_5m=candles_5m,
                candles_15m=candles_15m,
                candles_1h=candles_1h,
                spread=market["spread"],
                funding_rate=market["funding_rate"],
                latency_ms=market["latency_ms"],
                oi_current=market["oi_current"],
                oi_previous=market["oi_previous"],
                journal_insights=journal_entries,
            )

            action = license_result.action.value
            regime_val = license_result.regime.value

            # ── Regime shift detection → journal ──
            if regime_val != last_regime and last_regime:
                duration_min = int((time.time() - last_regime_time) / 60)
                journal.record_regime_shift(
                    last_regime, regime_val,
                    duration_min=duration_min,
                    trades_during=last_regime_trades,
                )
                last_regime_trades = 0
                last_regime_time = time.time()
            last_regime = regime_val

            # ── Skip review → journal ──
            if journal.should_run_skip_review():
                journal.record_skip_review(
                    skip_count=skip_tracker.skip_count,
                    top_reasons=skip_tracker.get_top_reasons(),
                    total_decisions=skip_tracker.total_decisions,
                )
                skip_tracker.reset()

            # ── Self-optimizer: periodic tuning cycle ──
            tuning_interval = config.TUNING_CYCLE_HOURS * 3600
            if (time.time() - last_tuning_cycle) >= tuning_interval:
                if config.AUTONOMOUS_TUNING_ENABLED:
                    dd_pct = ((peak_equity - agent.equity) / peak_equity * 100) if peak_equity > 0 else 0

                    # Check probations first
                    rollbacks = optimizer.check_probations(
                        current_dd=dd_pct,
                        current_expectancy=daily_pnl,
                    )
                    if rollbacks:
                        for rb in rollbacks:
                            journal.record_rollback(
                                rb["param"], rb["reverted_to"], rb["reverted_to"], rb["reason"]
                            )

                    # Run tuning analysis on paper + real trades
                    paper_total = paper_tracker.paper_wins + paper_tracker.paper_losses
                    paper_wr = (paper_tracker.paper_wins / paper_total * 100) if paper_total > 0 else 0

                    # Compute avg net_rr from recent completed paper trades
                    recent_papers = paper_tracker.completed[-30:]
                    avg_net_rr = 0.0
                    if recent_papers:
                        rr_vals = []
                        for pt in recent_papers:
                            risk = abs(pt["entry_price"] - pt["sl_price"])
                            reward = abs(pt["entry_price"] - pt["tp_price"])
                            if risk > 0:
                                rr_vals.append(reward / risk)
                        avg_net_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0

                    tuning_changes = optimizer.run_tuning_cycle(
                        paper_trades=paper_tracker.completed,
                        real_trade_count=optimizer._trade_counter,
                        daily_pnl=daily_pnl,
                        win_rate=paper_wr,
                        drawdown_pct=dd_pct,
                        avg_net_rr=avg_net_rr,
                    )

                    if tuning_changes:
                        for tc in tuning_changes:
                            journal._insert(
                                entry_type="PARAMETER_TUNING",
                                trigger=f"Auto-tuning: {tc['param']}",
                                observation=f"{tc['param']}: {tc['old']} → {tc['new']}",
                                conclusion=tc["reason"],
                                confidence=75,
                            )
                        logger.info(f"Tuning cycle: {len(tuning_changes)} changes, {len(rollbacks)} rollbacks")
                    else:
                        logger.info(f"Tuning cycle complete. Rollbacks: {len(rollbacks)}")
                last_tuning_cycle = time.time()

            # ── Meta-learner: cross-cycle pattern recognition ──
            meta_interval = config.META_LEARNING_INTERVAL_HOURS * 3600
            if (time.time() - last_meta_learning) >= meta_interval:
                try:
                    all_entries = journal.get_recent_entries(limit=30)
                    dd_pct = ((peak_equity - agent.equity) / peak_equity * 100) if peak_equity > 0 else 0
                    perf_summary = (
                        f"Equity: ${agent.equity:.2f} | Peak: ${peak_equity:.2f} | "
                        f"DD: {dd_pct:.1f}% | Daily PnL: ${daily_pnl:+.2f} | "
                        f"Consecutive losses: {consecutive_losses}"
                    )
                    await journal.llm_meta_learning(all_entries, perf_summary)
                    logger.info("Meta-learning cycle complete")
                except Exception as e:
                    logger.warning(f"Meta-learning error: {e}")
                last_meta_learning = time.time()

            # ── Strategy evolution: rewrite AI prompt based on results ──
            if strategy_evo.should_evolve():
                try:
                    all_entries = journal.get_recent_entries(limit=25)
                    # Gather trade stats from DB
                    from trading_agent.data_collector import DataCollector
                    dc = agent.data_collector
                    conn = dc._get_conn() if hasattr(dc, '_get_conn') else None
                    trade_stats = {"total_trades": 0, "wins": 0, "losses": 0,
                                   "win_rate": 0, "total_pnl": 0,
                                   "avg_win": 0, "avg_loss": 0,
                                   "best_trade": 0, "worst_trade": 0}
                    try:
                        import sqlite3 as _sql
                        _c = _sql.connect(config.DB_PATH)
                        row = _c.execute(
                            """SELECT COUNT(*) as total,
                               SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN net_pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                               SUM(net_pnl_usd) as total_pnl,
                               AVG(CASE WHEN net_pnl_usd > 0 THEN net_pnl_usd END) as avg_win,
                               AVG(CASE WHEN net_pnl_usd <= 0 THEN net_pnl_usd END) as avg_loss,
                               MAX(net_pnl_usd) as best,
                               MIN(net_pnl_usd) as worst
                               FROM trade_decisions WHERE decision LIKE 'EXIT_%'"""
                        ).fetchone()
                        _c.close()
                        total = row[0] or 0
                        wins = row[1] or 0
                        trade_stats = {
                            "total_trades": total, "wins": wins,
                            "losses": row[2] or 0,
                            "win_rate": (wins / total * 100) if total > 0 else 0,
                            "total_pnl": row[3] or 0,
                            "avg_win": row[4] or 0, "avg_loss": row[5] or 0,
                            "best_trade": row[6] or 0, "worst_trade": row[7] or 0,
                        }
                    except Exception:
                        pass

                    dd_pct = ((peak_equity - agent.equity) / peak_equity * 100) if peak_equity > 0 else 0
                    perf_summary = (
                        f"Equity: ${agent.equity:.2f} | Peak: ${peak_equity:.2f} | "
                        f"DD: {dd_pct:.1f}% | Daily PnL: ${daily_pnl:+.2f}"
                    )
                    new_version = await strategy_evo.evolve(
                        all_entries, perf_summary, trade_stats
                    )
                    if new_version:
                        set_system_prompt(strategy_evo.current_prompt)
                        journal._insert(
                            entry_type="STRATEGY_EVOLUTION",
                            trigger=f"Periodic strategy evolution (every {config.STRATEGY_EVOLUTION_HOURS}h)",
                            observation=f"Strategy evolved to v{new_version}",
                            conclusion=f"WR: {trade_stats['win_rate']:.1f}%, PnL: ${trade_stats['total_pnl']:.2f}",
                            confidence=75,
                        )
                        logger.info(f"Strategy evolution complete: v{new_version}")
                except Exception as e:
                    logger.warning(f"Strategy evolution error: {e}")

            # ── Record cycle observation to journal (every cycle) ──
            if True:
                journal.record_cycle_observation(
                    cycle=cycle,
                    price=price,
                    regime=regime_val,
                    confidence=license_result.confidence,
                    entry_quality=license_result.entry_quality,
                    action=action,
                    ai_reason=license_result.reason if hasattr(license_result, 'reason') else "",
                    gate_passed=gate_result.passed,
                    gate_reasons=gate_result.reasons if hasattr(gate_result, 'reasons') else [],
                    adx=regime_state.adx if regime_state else 0,
                    chop=regime_state.chop if regime_state else 0,
                    volume_ratio=vol_ratio,
                    num_open_positions=len(active_positions),
                )

            # ── Decision: TRADE or WAIT ──
            if gate_result.passed and license_result.is_trade:
                direction = "LONG" if license_result.action == Action.LONG else "SHORT"

                # Position sizing
                drawdown_pct = ((peak_equity - agent.equity) / peak_equity * 100) if peak_equity > 0 else 0
                entry_price = sl_tp.entry_price if sl_tp else price
                sl_price_calc = (sl_tp.sl_price if sl_tp and hasattr(sl_tp, 'sl_price') else
                                entry_price * (0.997 if direction == "LONG" else 1.003))

                # Block opposite direction if positions already open
                if active_positions:
                    existing_dir = next(iter(active_positions.values())).direction
                    if direction != existing_dir:
                        logger.info(
                            f"DIRECTION CONFLICT: {direction} vs open {existing_dir} — skipping"
                        )
                        skip_tracker.record_skip([f"Direction conflict: {direction} vs {existing_dir}"])
                        continue

                tp_pct_val = sl_tp.tp_pct if sl_tp and hasattr(sl_tp, 'tp_pct') else 0.0
                pos_result = calculate_position_size(
                    equity=agent.equity,
                    entry_price=entry_price,
                    sl_price=sl_price_calc,
                    entry_quality=license_result.entry_quality,
                    consecutive_losses=consecutive_losses,
                    drawdown_pct=drawdown_pct,
                    daily_pnl=daily_pnl,
                    weekly_pnl=weekly_pnl,
                    current_open_positions=len(active_positions),
                    tp_pct=tp_pct_val,
                )

                if pos_result.halted:
                    logger.warning(f"POSITION SIZING HALT: {pos_result.halt_reason}")
                    skip_tracker.record_skip([f"PosSizer: {pos_result.halt_reason}"])
                elif dry_run:
                    logger.info(
                        f"[DRY RUN] Would {action} @ ${price:.1f} | "
                        f"Size: ${pos_result.size_usd:.2f} | "
                        f"Tier: {pos_result.tier} | Lev: {pos_result.leverage:.1f}x | "
                        f"Quality mult: {pos_result.quality_mult}x"
                    )
                    skip_tracker.record_trade()
                    last_regime_trades += 1
                    last_trade_time = time.time()
                    relax_level = 0
                else:
                    # ── LIVE EXECUTION ──
                    tp_price_calc = (sl_tp.tp_price if sl_tp and hasattr(sl_tp, 'tp_price') else
                                     price * (1.005 if direction == "LONG" else 0.995))
                    logger.info(
                        f"EXECUTING: {action} @ ${price:.1f} | "
                        f"Size: ${pos_result.size_usd:.2f} ({pos_result.leverage:.1f}x) | "
                        f"SL: ${sl_price_calc:.2f} | TP: ${tp_price_calc:.2f} | "
                        f"Est.net: ${pos_result.estimated_net_profit:.2f}"
                    )

                    order = place_order(direction, pos_result.size_usd, price)
                    if order:
                        trade_id = agent.data_collector._active_trade_id or str(uuid.uuid4())
                        qty_btc = round(pos_result.size_usd / price, 3)
                        new_pos = ActivePosition(
                            trade_id=trade_id,
                            direction=direction,
                            entry_price=price,
                            sl_price=sl_price_calc,
                            tp_price=tp_price_calc,
                            qty_btc=qty_btc,
                            license=license_result,
                            gate_result=gate_result,
                            regime=regime_state,
                            sl_tp=sl_tp,
                        )
                        active_positions[trade_id] = new_pos

                        # Set exchange safety SL with retry (CRITICAL for 24/7 safety)
                        sl_set = False
                        for sl_attempt in range(3):
                            if update_exchange_sl(active_positions):
                                sl_set = True
                                break
                            wait_s = 2 ** sl_attempt  # 1s, 2s, 4s
                            logger.warning(
                                f"Exchange SL attempt {sl_attempt + 1}/3 failed — "
                                f"retrying in {wait_s}s"
                            )
                            await asyncio.sleep(wait_s)

                        if not sl_set:
                            logger.error(
                                "CRITICAL: Exchange SL failed after 3 retries — "
                                "closing position for safety"
                            )
                            try:
                                close_partial_position(direction, qty_btc)
                            except Exception:
                                close_all_positions()
                            del active_positions[trade_id]
                            skip_tracker.record_skip(["Exchange SL failed — position closed"])
                            continue

                        _sync_shared_positions(active_positions, price)
                        logger.info(
                            f"Position [{trade_id[:8]}] added | "
                            f"Total open: {len(active_positions)}/{config.MAX_OPEN_POSITIONS}"
                        )
                        skip_tracker.record_trade()
                        last_trade_time = time.time()
                        relax_level = 0
                        last_regime_trades += 1
                    else:
                        logger.error("Order placement failed — staying flat")
                        skip_tracker.record_skip(["Order failed"])
            else:
                skip_tracker.record_skip(gate_result.reasons if gate_result.reasons else ["WAIT"])

                # ── Paper trade: "what if I had traded?" ──
                # Open paper trades even with low confidence to build learning data
                if license_result.action in (Action.LONG, Action.SHORT) and license_result.confidence >= 15:
                    direction = "LONG" if license_result.action == Action.LONG else "SHORT"
                    entry_p = price
                    atr_val = abs(price * 0.003)  # ~0.3% as rough ATR
                    sl_p = entry_p - atr_val if direction == "LONG" else entry_p + atr_val
                    tp_p = entry_p + atr_val * 1.5 if direction == "LONG" else entry_p - atr_val * 1.5
                    was_blocked = bool(gate_result.reasons)
                    block_reason = "; ".join(gate_result.reasons[:2]) if gate_result.reasons else "AI WAIT — low confidence"
                    paper_tracker.open_paper_trade(
                        direction=direction, entry_price=entry_p,
                        sl_price=sl_p, tp_price=tp_p,
                        confidence=license_result.confidence,
                        quality=license_result.entry_quality,
                        regime=regime_val,
                        was_blocked=was_blocked,
                        block_reason=block_reason,
                    )
                    # Record paper trade opening in journal too
                    journal._insert(
                        entry_type="PAPER_TRADE",
                        trigger=f"PAPER OPEN: {direction} @ ${price:.0f} (conf={license_result.confidence})",
                        observation=(
                            f"Imaginary {direction} @ ${entry_p:.0f} | SL=${sl_p:.0f} TP=${tp_p:.0f} | "
                            f"conf={license_result.confidence} quality={license_result.entry_quality} | "
                            f"regime={regime_val} | reason: {block_reason}\n"
                            f"AI thinking: {license_result.reason[:200] if hasattr(license_result, 'reason') else 'N/A'}"
                        ),
                        conclusion=f"Tracking to see if {direction} would have been profitable",
                        confidence=license_result.confidence,
                    )
                    logger.info(
                        f"PAPER OPEN: {direction} @ ${price:.0f} "
                        f"(SL=${sl_p:.0f} TP=${tp_p:.0f}) conf={license_result.confidence} | "
                        f"{block_reason}"
                    )

                if cycle % 10 == 0:
                    logger.info(
                        f"Cycle {cycle}: WAIT | ${price:.0f} | "
                        f"regime={regime_val} conf={license_result.confidence} | "
                        f"{paper_tracker.get_summary()}"
                    )

        except Exception as e:
            logger.error(f"Cycle {cycle} error: {e}", exc_info=True)
            consecutive_errors = getattr(run_bot, '_consecutive_errors', 0) + 1
            run_bot._consecutive_errors = consecutive_errors

            # On error, protect exchange positions with retry
            if active_positions and not dry_run:
                sl_ok = False
                for attempt in range(3):
                    try:
                        if update_exchange_sl(active_positions):
                            sl_ok = True
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(2 ** attempt)

                if not sl_ok:
                    logger.error(
                        "CRITICAL: Cannot set exchange SL after error — "
                        "closing all positions for safety"
                    )
                    try:
                        close_all_positions()
                        active_positions.clear()
                    except Exception as close_err:
                        logger.error(f"Emergency close failed: {close_err}")

            # Circuit breaker: 10 consecutive errors = halt
            if consecutive_errors >= 10:
                logger.error(
                    f"CIRCUIT BREAKER: {consecutive_errors} consecutive errors — "
                    "halting bot"
                )
                if active_positions and not dry_run:
                    close_all_positions()
                    active_positions.clear()
                running = False
                continue
        else:
            # Reset error counter on successful cycle
            run_bot._consecutive_errors = 0

        # ── Periodic equity re-sync from exchange (every 10 cycles / 5 min) ──
        if cycle % 10 == 0 and not dry_run:
            try:
                real_equity = await fetch_account_equity()
                if real_equity > 0:
                    drift = abs(real_equity - agent.equity)
                    if drift > 0.50:  # More than $0.50 drift
                        logger.warning(
                            f"EQUITY DRIFT: local=${agent.equity:.2f} vs exchange=${real_equity:.2f} "
                            f"(drift=${drift:.2f}) — syncing to exchange"
                        )
                        agent.equity = real_equity
                    if real_equity > peak_equity:
                        peak_equity = real_equity
            except Exception:
                pass

        # Wait for next cycle
        elapsed = time.time() - cycle_start
        wait = max(0, LOOP_INTERVAL_SEC - elapsed)
        if running and wait > 0:
            await asyncio.sleep(wait)

    # ── Cleanup with timeout ──
    logger.info("Bot stopping — cleaning up...")
    shutdown_start = time.time()
    SHUTDOWN_TIMEOUT = 30  # Max 30s for cleanup

    if active_positions and not dry_run:
        logger.info(f"Closing {len(active_positions)} active position(s) before shutdown")
        for attempt in range(3):
            if close_all_positions():
                break
            if time.time() - shutdown_start > SHUTDOWN_TIMEOUT:
                logger.error("SHUTDOWN TIMEOUT: Could not close positions in 30s")
                break
            logger.warning(f"Shutdown close attempt {attempt + 1}/3 failed — retrying")
            await asyncio.sleep(2)

    try:
        journal.close()
        optimizer.close()
        agent.data_collector.close()
    except Exception as e:
        logger.warning(f"Cleanup error (non-critical): {e}")
    logger.info("Bot stopped")


def main():
    parser = argparse.ArgumentParser(description="BTCUSDT Scalping Bot")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without orders")
    parser.add_argument("--dashboard", action="store_true", help="Start dashboard alongside bot")
    args = parser.parse_args()

    if args.dashboard:
        import threading

        def run_dashboard():
            try:
                import uvicorn
                from scripts.dashboard_server import create_app
                app = create_app()
                uvicorn.run(
                    app,
                    host=config.DASHBOARD_HOST,
                    port=config.DASHBOARD_PORT,
                    log_level="warning",
                )
            except Exception as e:
                logger.error(f"Dashboard failed: {e}")

        dash_thread = threading.Thread(target=run_dashboard, daemon=True)
        dash_thread.start()
        print(f"Dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")

    asyncio.run(run_bot(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
