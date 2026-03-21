#!/usr/bin/env python3
"""
BTCUSDT Perpetual Futures Scalping Bot — Main Entry Point.

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
from datetime import datetime, timezone

from trading_agent import config
from trading_agent.agent import TradingAgent
from trading_agent.log_sanitizer import setup_sanitized_logging
from trading_agent.models import Action, CandleData

logger = logging.getLogger(__name__)

LOOP_INTERVAL_SEC = 60  # 1-minute candle cycle


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


async def fetch_candles(symbol: str, interval: str, limit: int) -> list[CandleData]:
    """
    Fetch candles from Bybit API.
    Returns empty list if pybit is not configured or fails.
    """
    try:
        from pybit.unified_trading import HTTP

        session = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )
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
        from pybit.unified_trading import HTTP

        session = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )

        start = time.time()

        # Orderbook (spread)
        ob = session.get_orderbook(category=config.CATEGORY, symbol=config.SYMBOL, limit=1)
        latency = (time.time() - start) * 1000
        data["latency_ms"] = latency

        bids = ob["result"]["b"]
        asks = ob["result"]["a"]
        if bids and asks:
            data["spread"] = float(asks[0][0]) - float(bids[0][0])

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


async def run_bot(dry_run: bool = False):
    """Main bot loop."""
    setup_sanitized_logging()

    if not validate_api_keys():
        return

    agent = TradingAgent()
    agent.equity = config.POSITION_SIZE_USD * 10  # Will be replaced by actual balance

    logger.info("=" * 60)
    logger.info(f" BTCUSDT Scalping Bot — {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f" Testnet: {config.BYBIT_TESTNET}")
    logger.info(f" MIN_CONFIDENCE: {config.MIN_CONFIDENCE}")
    logger.info(f" TRADE_QUALITY_MIN: {config.TRADE_QUALITY_MIN}")
    logger.info(f" REGIME_FILTER: {config.REGIME_FILTER_ENABLED}")
    logger.info(f" FALLBACK_OVERRIDE: {config.ENABLE_FALLBACK_OVERRIDE}")
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
            # Fetch data
            candles_1m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_1M, 100)
            candles_5m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_5M, 50)
            candles_15m = await fetch_candles(config.SYMBOL, config.TIMEFRAME_15M, 50)
            candles_1h = await fetch_candles(config.SYMBOL, config.TIMEFRAME_1H, 30)

            if not candles_1m:
                logger.warning(f"Cycle {cycle}: No candle data — skipping")
                await asyncio.sleep(LOOP_INTERVAL_SEC)
                continue

            market = await fetch_market_data()

            # Evaluate
            license, gate_result, sl_tp = await agent.evaluate_market(
                candles_1m=candles_1m,
                candles_5m=candles_5m,
                candles_15m=candles_15m,
                candles_1h=candles_1h,
                spread=market["spread"],
                funding_rate=market["funding_rate"],
                latency_ms=market["latency_ms"],
                oi_current=market["oi_current"],
                oi_previous=market["oi_previous"],
            )

            price = candles_1m[-1].close
            action = license.action.value

            if gate_result.passed and license.is_trade:
                if dry_run:
                    logger.info(f"[DRY RUN] Would {action} @ ${price:.1f}")
                else:
                    logger.info(f"EXECUTE: {action} @ ${price:.1f}")
                    # TODO: Place order via pybit
            else:
                logger.info(
                    f"Cycle {cycle}: WAIT | ${price:.0f} | "
                    f"regime={license.regime.value} conf={license.confidence}"
                )

        except Exception as e:
            logger.error(f"Cycle {cycle} error: {e}")

        # Wait for next cycle
        elapsed = time.time() - cycle_start
        wait = max(0, LOOP_INTERVAL_SEC - elapsed)
        if running and wait > 0:
            await asyncio.sleep(wait)

    logger.info("Bot stopped")
    agent.data_collector.close()


def main():
    parser = argparse.ArgumentParser(description="BTCUSDT Scalping Bot")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without orders")
    parser.add_argument("--dashboard", action="store_true", help="Also start dashboard")
    args = parser.parse_args()

    if args.dashboard:
        print("Dashboard: python scripts/dashboard_server.py")
        print("(Run in a separate terminal)")

    asyncio.run(run_bot(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
