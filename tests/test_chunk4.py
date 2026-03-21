"""Tests for Chunk 4 — Data Collection Engine."""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trading_agent import config
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, EntryGateResult, HTFAlignment,
    Regime, RegimeState, SetupType,
)
from trading_agent.risk_manager import SLTPLevels
from trading_agent.data_collector import DataCollector, MFEMAETracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_candles(n=50):
    candles = []
    base = 100000.0
    for i in range(n):
        price = base + i * 100
        candles.append(CandleData(
            timestamp=datetime(2025, 1, 1, i % 24, 0, tzinfo=timezone.utc),
            open=price, high=price + 60, low=price - 20,
            close=price + 50, volume=2000,
        ))
    return candles


def make_license(**kwargs):
    defaults = dict(
        action=Action.LONG, confidence=80, entry_quality=85,
        regime=Regime.TRENDING, setup_type=SetupType.CONTINUATION,
        htf_alignment=HTFAlignment.ALIGNED, reason="test entry",
    )
    defaults.update(kwargs)
    return DirectionalLicense(**defaults)


def make_regime():
    return RegimeState(
        regime=Regime.TRENDING, adx=30, chop=40, atr_pct=0.3,
        bb_width=1.5, ema_slope=0.001,
    )


def make_gate_result(passed=True, cvd_ok=True, oi_ok=True, session_ok=True):
    return EntryGateResult(
        passed=passed, htf_ok=True, trend_ok=True, extension_ok=True,
        candle_ok=True, volume_ok=True, reversal_ok=True, regime_ok=True,
        cvd_ok=cvd_ok, oi_ok=oi_ok, session_ok=session_ok,
    )


def make_sl_tp():
    return SLTPLevels(
        entry_price=104950.0, stop_loss=104600.0,
        take_profit=105500.0, sl_pct=0.33, tp_pct=0.52,
        net_rr=1.35, entry_type="MAKER",
    )


def make_temp_collector():
    """Create a DataCollector with a temp database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return DataCollector(db_path=path), path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_1_snapshot_all_fields():
    """Test 1: Trade snapshot contains all required fields."""
    dc, db_path = make_temp_collector()
    try:
        candles = make_candles()
        license = make_license()
        regime = make_regime()
        gate = make_gate_result()
        sl_tp = make_sl_tp()

        row_id = dc.save_trade_decision(
            decision="ENTRY_LONG",
            license=license,
            gate_result=gate,
            regime=regime,
            candles_1m=candles,
            candles_5m=candles[:20],
            candles_15m=candles[:10],
            candles_1h=candles[:5],
            sl_tp=sl_tp,
            spread=1.5,
            funding_rate=0.0001,
            oi_current=50000,
            oi_previous=49000,
            equity=10000,
            latency_ms=50,
            ai_response_ms=200,
            trade_id="test123",
        )

        assert row_id > 0, "Should return valid row ID"

        # Check columns
        cols = dc.get_columns("trade_decisions")
        required = [
            "timestamp_utc", "decision", "trade_id", "price", "spread_usdt",
            "atr_1m", "volume_ratio", "ema9", "ema21", "rsi_14", "adx",
            "chop_index", "bb_width", "macd_hist", "cvd_value", "cvd_aligned",
            "oi_current", "oi_confirmed", "regime", "htf_alignment",
            "entry_quality", "confidence", "entry_price", "sl_price", "tp_price",
            "net_rr", "max_favorable_excursion", "max_adverse_excursion",
            "session_hour_local", "equity_before", "drawdown_pct",
        ]
        for col in required:
            assert col in cols, f"Missing column: {col}"

        # Verify data was written
        conn = dc._get_conn()
        row = conn.execute("SELECT * FROM trade_decisions WHERE id = ?", (row_id,)).fetchone()
        assert row is not None, "Row should exist"

        # Check critical non-NULL fields
        col_map = {cols[i]: i for i in range(len(cols))}
        assert row[col_map["decision"]] == "ENTRY_LONG"
        assert row[col_map["trade_id"]] == "test123"
        assert row[col_map["price"]] > 0
        assert row[col_map["regime"]] == "TRENDING"

        print(f"  PASS: Snapshot has {len(cols)} columns, all required fields present")
    finally:
        dc.close()
        os.unlink(db_path)


def test_2_mfe_mae_tracking():
    """Test 2: MFE/MAE tracking produces valid values."""
    tracker = MFEMAETracker(entry_price=100000.0, direction="LONG")

    # Price goes up (favorable)
    tracker.update(100500.0)  # +500 unrealized
    tracker.update(101000.0)  # +1000 unrealized (new MFE)
    # Price drops below entry (adverse)
    tracker.update(99500.0)   # -500 unrealized (MAE)
    tracker.update(99000.0)   # -1000 unrealized (new MAE)
    # Price recovers
    tracker.update(100200.0)

    mfe, mae = tracker.close()

    assert mfe == 1000.0, f"MFE should be 1000, got {mfe}"
    assert mae == 1000.0, f"MAE should be 1000, got {mae}"

    # SHORT direction
    tracker2 = MFEMAETracker(entry_price=100000.0, direction="SHORT")
    tracker2.update(99000.0)   # +1000 favorable for short
    tracker2.update(100500.0)  # -500 adverse for short
    mfe2, mae2 = tracker2.close()
    assert mfe2 == 1000.0, f"SHORT MFE should be 1000, got {mfe2}"
    assert mae2 == 500.0, f"SHORT MAE should be 500, got {mae2}"

    print(f"  PASS: MFE/MAE tracking correct (LONG: mfe={mfe}, mae={mae}; SHORT: mfe={mfe2}, mae={mae2})")


def test_3_market_snapshots():
    """Test 3: Market snapshots persist at configured interval."""
    dc, db_path = make_temp_collector()
    orig_interval = config.MARKET_SNAPSHOT_INTERVAL_SEC
    config.MARKET_SNAPSHOT_INTERVAL_SEC = 0  # Allow immediate snapshots
    try:
        candles = make_candles()
        regime = make_regime()

        # Force first snapshot
        saved1 = dc.save_market_snapshot(candles, regime, spread=1.0, force=True)
        assert saved1, "First snapshot should save"

        # Second immediate call without force should also save (interval=0)
        saved2 = dc.save_market_snapshot(candles, regime, spread=1.0)
        assert saved2, "Second snapshot should save with interval=0"

        count = dc.get_table_count("market_snapshots")
        assert count >= 2, f"Should have ≥2 snapshots, got {count}"

        # Verify columns
        cols = dc.get_columns("market_snapshots")
        for col in ["timestamp_utc", "price", "atr_1m", "regime", "adx", "cvd_value"]:
            assert col in cols, f"Missing column: {col}"

        print(f"  PASS: {count} market snapshots saved, all required columns present")
    finally:
        config.MARKET_SNAPSHOT_INTERVAL_SEC = orig_interval
        dc.close()
        os.unlink(db_path)


def test_4_equity_curve():
    """Test 4: Equity curve records on trade close AND at interval."""
    dc, db_path = make_temp_collector()
    orig_interval = config.EQUITY_SNAPSHOT_INTERVAL_SEC
    config.EQUITY_SNAPSHOT_INTERVAL_SEC = 0
    try:
        # Periodic snapshot
        saved = dc.save_equity_snapshot(10000.0, force=True)
        assert saved, "Equity snapshot should save"

        # After trade close (force)
        dc.record_trade_stats(is_win=True, net_pnl=50.0)
        saved2 = dc.save_equity_snapshot(10050.0, force=True)
        assert saved2, "Post-trade equity snapshot should save"

        count = dc.get_table_count("equity_curve")
        assert count >= 2, f"Should have ≥2 equity records, got {count}"

        # Verify drawdown tracking
        dc._peak_equity = 11000.0
        dc.save_equity_snapshot(10000.0, force=True)
        conn = dc._get_conn()
        row = conn.execute("SELECT drawdown_pct FROM equity_curve ORDER BY id DESC LIMIT 1").fetchone()
        assert row[0] > 0, f"Drawdown should be > 0, got {row[0]}"

        print(f"  PASS: {count + 1} equity records, drawdown tracking works")
    finally:
        config.EQUITY_SNAPSHOT_INTERVAL_SEC = orig_interval
        dc.close()
        os.unlink(db_path)


def test_5_daily_summary():
    """Test 5: Daily session summary auto-computes from trade data."""
    dc, db_path = make_temp_collector()
    try:
        candles = make_candles()
        license = make_license()
        regime = make_regime()
        sl_tp = make_sl_tp()

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Simulate some entries and exits
        gate_pass = make_gate_result(passed=True)
        gate_skip = make_gate_result(passed=False)
        gate_skip.add_block("Test skip reason")

        # Entry
        dc.save_trade_decision("ENTRY_LONG", license, gate_pass, regime,
                               candles, sl_tp=sl_tp, equity=10000, trade_id="t1")
        # Skip
        dc.save_trade_decision("SKIP", license, gate_skip, regime,
                               candles, equity=10000)
        # Exit win
        dc.save_trade_decision("EXIT_TP", license, gate_pass, regime,
                               candles, sl_tp=sl_tp, equity=10000, trade_id="t1",
                               exit_price=105500, exit_type="TP",
                               gross_pnl=55.0, fees_paid=5.0, net_pnl=50.0,
                               hold_duration_sec=120)
        # Another entry + loss exit
        dc.save_trade_decision("ENTRY_LONG", license, gate_pass, regime,
                               candles, sl_tp=sl_tp, equity=10050, trade_id="t2")
        dc.save_trade_decision("EXIT_SL", license, gate_pass, regime,
                               candles, sl_tp=sl_tp, equity=10050, trade_id="t2",
                               exit_price=104600, exit_type="SL",
                               gross_pnl=-30.0, fees_paid=5.0, net_pnl=-35.0,
                               hold_duration_sec=60)

        summary = dc.compute_daily_summary(today)
        assert summary is not None, "Summary should be computed"
        assert summary["trades_taken"] == 2, f"Expected 2 trades, got {summary['trades_taken']}"
        assert summary["trades_skipped"] == 1, f"Expected 1 skip, got {summary['trades_skipped']}"
        assert summary["wins"] == 1, f"Expected 1 win, got {summary['wins']}"
        assert summary["losses"] == 1, f"Expected 1 loss, got {summary['losses']}"
        assert summary["net_pnl"] == 15.0, f"Expected net_pnl=15.0, got {summary['net_pnl']}"

        # Verify persisted
        count = dc.get_table_count("daily_sessions")
        assert count == 1, f"Should have 1 daily session, got {count}"

        print(f"  PASS: Daily summary computed — {summary}")
    finally:
        dc.close()
        os.unlink(db_path)


def test_6_migration_safety():
    """Test 6: Database migration creates tables without breaking existing data."""
    dc, db_path = make_temp_collector()
    try:
        # Insert some data
        candles = make_candles()
        license = make_license()
        regime = make_regime()
        gate = make_gate_result()
        dc.save_trade_decision("SKIP", license, gate, regime, candles, equity=5000)
        dc.save_market_snapshot(candles, regime, force=True)
        dc.save_equity_snapshot(5000.0, force=True)

        # Verify tables exist
        tables = dc.get_tables()
        required_tables = ["trade_decisions", "market_snapshots", "equity_curve", "daily_sessions"]
        for t in required_tables:
            assert t in tables, f"Missing table: {t}"

        # Close and reopen (simulates restart with migration)
        dc.close()
        dc2 = DataCollector(db_path=db_path)

        # Existing data should survive
        assert dc2.get_table_count("trade_decisions") == 1
        assert dc2.get_table_count("market_snapshots") == 1
        assert dc2.get_table_count("equity_curve") == 1

        # New inserts should work
        dc2.save_trade_decision("SKIP", license, gate, regime, candles, equity=5000)
        assert dc2.get_table_count("trade_decisions") == 2

        print(f"  PASS: Migration creates all {len(required_tables)} tables, preserves existing data")
        dc2.close()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all_tests():
    tests = [
        ("1. Trade snapshot all fields", test_1_snapshot_all_fields),
        ("2. MFE/MAE tracking", test_2_mfe_mae_tracking),
        ("3. Market snapshots persist", test_3_market_snapshots),
        ("4. Equity curve records", test_4_equity_curve),
        ("5. Daily session summary", test_5_daily_summary),
        ("6. Migration safety", test_6_migration_safety),
    ]

    print("=" * 60)
    print(" CHUNK 4 TESTS — Data Collection Engine")
    print("=" * 60)

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\nTest {name}:")
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 60}")
    print(f" Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 60}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
