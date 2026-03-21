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
from datetime import datetime, timezone

from trading_agent import config
from trading_agent.agent import TradingAgent
from trading_agent.learning_journal import LearningJournal
from trading_agent.log_sanitizer import setup_sanitized_logging
from trading_agent.models import Action, CandleData, Regime
from trading_agent.position_sizer import calculate_position_size
from trading_agent.self_optimizer import SelfOptimizer

logger = logging.getLogger(__name__)

LOOP_INTERVAL_SEC = 60  # 1-minute candle cycle


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
    """Fetch candles from Bybit API."""
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
        return candles

    except ImportError:
        logger.warning("pybit not installed — run: pip install pybit")
        return []
    except Exception as e:
        logger.error(f"Failed to fetch candles ({interval}): {e}")
        return []


async def fetch_market_data() -> dict:
    """Fetch spread, funding rate, OI from Bybit."""
    data = {"spread": 0.0, "funding_rate": 0.0, "oi_current": None, "oi_previous": None, "latency_ms": 0}
    try:
        session = _get_session()
        start = time.time()

        # Orderbook (spread)
        ob = session.get_orderbook(category=config.CATEGORY, symbol=config.SYMBOL, limit=1)
        latency = (time.time() - start) * 1000
        data["latency_ms"] = latency

        bids = ob["result"]["b"]
        asks = ob["result"]["a"]
        if bids and asks:
            data["spread"] = float(asks[0][0]) - float(bids[0][0])
            data["best_bid"] = float(bids[0][0])
            data["best_ask"] = float(asks[0][0])

        # Funding rate
        try:
            tickers = session.get_tickers(category=config.CATEGORY, symbol=config.SYMBOL)
            data["funding_rate"] = float(tickers["result"]["list"][0].get("fundingRate", 0))
        except Exception:
            pass

        # Open Interest
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

def place_order(direction: str, size_usd: float, price: float) -> dict | None:
    """
    Place a market order on Bybit.
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
            logger.info(f"ORDER PLACED: {side} {qty} BTC @ market | order_id={order_id}")
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

    # Fetch initial equity from Bybit
    initial_equity = await fetch_account_equity()
    if initial_equity > 0:
        agent.equity = initial_equity
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
    current_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current_week = datetime.now(timezone.utc).isocalendar()[1]

    # Active trade state
    active_trade_id: str | None = None
    active_direction: str | None = None
    active_entry_price: float = 0.0
    active_sl_price: float = 0.0
    active_tp_price: float = 0.0
    active_license = None
    active_gate_result = None
    active_regime = None
    active_sl_tp = None

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

            # ── Fetch data ──
            candles_1m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_1M, 100)
            candles_5m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_5M, 50)
            candles_15m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_15M, 50)
            candles_1h = await fetch_candles(config.SYMBOL, config.TIMEFRAME_1H, 30)

            if not candles_1m:
                logger.warning(f"Cycle {cycle}: No candle data — skipping")
                await asyncio.sleep(LOOP_INTERVAL_SEC)
                continue

            market = await fetch_market_data()
            price = candles_1m[-1].close

            # ── Update MFE/MAE for active trade ──
            if active_trade_id:
                agent.update_price_tick(price)

            # ── Check if active trade hit SL/TP (position monitoring) ──
            if active_trade_id and not dry_run:
                try:
                    session = _get_session()
                    positions = session.get_positions(
                        category=config.CATEGORY, symbol=config.SYMBOL
                    )
                    pos_list = positions.get("result", {}).get("list", [])
                    has_position = any(float(p.get("size", 0)) > 0 for p in pos_list)

                    if not has_position:
                        # Position was closed (SL/TP hit or liquidation)
                        exit_price = price
                        if active_direction == "LONG":
                            gross_pnl = (exit_price - active_entry_price) * (agent.equity * 0.01 / abs(active_entry_price - active_sl_price) if active_sl_price != active_entry_price else 0)
                        else:
                            gross_pnl = (active_entry_price - exit_price) * (agent.equity * 0.01 / abs(active_entry_price - active_sl_price) if active_sl_price != active_entry_price else 0)

                        fees = abs(gross_pnl) * config.TAKER_FEE_RATE * 2
                        net_pnl = gross_pnl - fees
                        is_win = net_pnl > 0

                        exit_type = "TP" if is_win else "SL"

                        # Record close
                        if active_license and active_gate_result and active_regime:
                            agent.close_trade(
                                active_license, active_gate_result, active_regime,
                                candles_1m, exit_price, exit_type,
                                gross_pnl, fees, net_pnl,
                                sl_tp=active_sl_tp,
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
                            trade_id=active_trade_id,
                            is_win=is_win,
                            net_pnl=net_pnl,
                            entry_quality=active_license.entry_quality if active_license else 0,
                            confidence=active_license.confidence if active_license else 0,
                            regime=active_license.regime.value if active_license else "UNKNOWN",
                            setup_type=active_license.setup_type.value if active_license else "NONE",
                            mfe=mfe,
                            mae=mae,
                            hold_sec=hold_duration,
                        )

                        # Deeper loss reflection
                        if not is_win:
                            sl_dist = abs(active_entry_price - active_sl_price) if active_sl_price else 0
                            mae_exceeded = mae > sl_dist * 1.1 if sl_dist > 0 else True
                            journal.record_post_loss_reflection(
                                trade_id=active_trade_id,
                                net_pnl=net_pnl,
                                recent_losses=consecutive_losses,
                                regime=active_license.regime.value if active_license else "UNKNOWN",
                                mae_exceeded_sl=mae_exceeded,
                            )

                        logger.info(
                            f"TRADE CLOSED: {exit_type} | {active_direction} | "
                            f"PnL=${net_pnl:+.2f} | Equity=${agent.equity:.2f}"
                        )

                        # Clear active trade
                        active_trade_id = None
                        active_direction = None
                        active_entry_price = 0.0
                        active_sl_price = 0.0
                        active_tp_price = 0.0
                        active_license = None
                        active_gate_result = None
                        active_regime = None
                        active_sl_tp = None

                except Exception as e:
                    logger.error(f"Position check error: {e}")

            # ── Skip if we already have an active trade ──
            if active_trade_id:
                await asyncio.sleep(max(0, LOOP_INTERVAL_SEC - (time.time() - cycle_start)))
                continue

            # ── Get journal insights for AI memory ──
            journal_entries = journal.get_recent_entries(limit=10)

            # ── Evaluate market (with journal context) ──
            license_result, gate_result, sl_tp = await agent.evaluate_market(
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
                    rollbacks = optimizer.check_probations(
                        current_dd=dd_pct,
                        current_expectancy=daily_pnl,
                    )
                    if rollbacks:
                        for rb in rollbacks:
                            journal.record_rollback(
                                rb["param"], rb["reverted_to"], rb["reverted_to"], rb["reason"]
                            )
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

            # ── Decision: TRADE or WAIT ──
            if gate_result.passed and license_result.is_trade:
                direction = "LONG" if license_result.action == Action.LONG else "SHORT"

                # Position sizing
                drawdown_pct = ((peak_equity - agent.equity) / peak_equity * 100) if peak_equity > 0 else 0
                entry_price = sl_tp.entry_price if sl_tp else price
                sl_price_calc = sl_tp.sl_price if sl_tp else entry_price * (0.997 if direction == "LONG" else 1.003)

                pos_result = calculate_position_size(
                    equity=agent.equity,
                    entry_price=entry_price,
                    sl_price=sl_price_calc,
                    entry_quality=license_result.entry_quality,
                    consecutive_losses=consecutive_losses,
                    drawdown_pct=drawdown_pct,
                    daily_pnl=daily_pnl,
                    weekly_pnl=weekly_pnl,
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
                else:
                    # ── LIVE EXECUTION ──
                    logger.info(
                        f"EXECUTING: {action} @ ${price:.1f} | "
                        f"Size: ${pos_result.size_usd:.2f} | SL: ${sl_price_calc:.2f} | "
                        f"TP: ${sl_tp.tp_price:.2f if sl_tp else 0}"
                    )

                    order = place_order(direction, pos_result.size_usd, price)
                    if order:
                        active_trade_id = agent.data_collector._active_trade_id or str(uuid.uuid4())
                        active_direction = direction
                        active_entry_price = price
                        active_sl_price = sl_price_calc
                        active_tp_price = sl_tp.tp_price if sl_tp else price * (1.005 if direction == "LONG" else 0.995)
                        active_license = license_result
                        active_gate_result = gate_result
                        active_regime = agent.data_collector._last_regime if hasattr(agent.data_collector, '_last_regime') else None
                        active_sl_tp = sl_tp

                        # Set SL/TP on exchange
                        set_stop_loss_take_profit(active_sl_price, active_tp_price, direction)
                        skip_tracker.record_trade()
                        last_regime_trades += 1
                    else:
                        logger.error("Order placement failed — staying flat")
                        skip_tracker.record_skip(["Order failed"])
            else:
                skip_tracker.record_skip(gate_result.reasons if gate_result.reasons else ["WAIT"])
                if cycle % 10 == 0:  # Log WAIT every 10 cycles to reduce noise
                    logger.info(
                        f"Cycle {cycle}: WAIT | ${price:.0f} | "
                        f"regime={regime_val} conf={license_result.confidence}"
                    )

        except Exception as e:
            logger.error(f"Cycle {cycle} error: {e}", exc_info=True)

        # Wait for next cycle
        elapsed = time.time() - cycle_start
        wait = max(0, LOOP_INTERVAL_SEC - elapsed)
        if running and wait > 0:
            await asyncio.sleep(wait)

    # ── Cleanup ──
    logger.info("Bot stopping — cleaning up...")
    if active_trade_id and not dry_run:
        logger.info("Closing active position before shutdown")
        close_all_positions()

    journal.close()
    optimizer.close()
    agent.data_collector.close()
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
