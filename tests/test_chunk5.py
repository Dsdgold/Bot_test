"""Tests for Chunk 5 — Analytics Engine + Capital Growth + Position Sizing."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trading_agent import config
from trading_agent.position_sizer import (
    PositionSizeResult, calculate_position_size, get_growth_tier,
    get_quality_multiplier, get_streak_multiplier, get_drawdown_multiplier,
    check_daily_loss_limit, check_weekly_loss_limit, check_equity_floor,
)

# Import analytics for tests 10-11
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from performance_engine import run_full_report

# Import data collector for seeding test data
from trading_agent.data_collector import DataCollector
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, EntryGateResult, HTFAlignment,
    Regime, RegimeState, SetupType,
)
from trading_agent.risk_manager import SLTPLevels
from datetime import datetime, timezone
import sqlite3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_candles(n=50):
    candles = []
    for i in range(n):
        p = 100000 + i * 100
        candles.append(CandleData(
            timestamp=datetime(2025, 1, 1, i % 24, 0, tzinfo=timezone.utc),
            open=p, high=p + 60, low=p - 20, close=p + 50, volume=2000,
        ))
    return candles


def seed_trades(db_path: str, count: int = 60) -> None:
    """Seed database with synthetic closed trades for analytics tests."""
    dc = DataCollector(db_path=db_path)
    candles = make_candles()
    regime = RegimeState(regime=Regime.TRENDING, adx=30, chop=40, atr_pct=0.3, bb_width=1.5, ema_slope=0.001)
    license = DirectionalLicense(
        action=Action.LONG, confidence=75, regime=Regime.TRENDING,
        setup_type=SetupType.CONTINUATION, entry_quality=82,
        htf_alignment=HTFAlignment.ALIGNED, reason="test",
    )
    sl_tp = SLTPLevels(
        entry_price=100000, stop_loss=99650, take_profit=100500,
        sl_pct=0.35, tp_pct=0.50, net_rr=1.3, entry_type="MAKER",
    )
    gate = EntryGateResult(passed=True, htf_ok=True, trend_ok=True, extension_ok=True,
                           candle_ok=True, volume_ok=True, reversal_ok=True, regime_ok=True,
                           cvd_ok=True, oi_ok=True, session_ok=True)

    import random
    random.seed(42)
    for i in range(count):
        tid = f"t{i:04d}"
        # Entry
        dc.save_trade_decision("ENTRY_LONG", license, gate, regime, candles,
                               sl_tp=sl_tp, equity=10000 + i * 10, trade_id=tid,
                               spread=1.5, funding_rate=0.0001)
        # Exit — 60% wins
        is_win = random.random() < 0.6
        net_pnl = random.uniform(10, 80) if is_win else random.uniform(-60, -5)
        dc.save_trade_decision(
            "EXIT_TP" if is_win else "EXIT_SL", license, gate, regime, candles,
            sl_tp=sl_tp, equity=10000 + i * 10, trade_id=tid,
            exit_price=100500 if is_win else 99650,
            exit_type="TP" if is_win else "SL",
            gross_pnl=net_pnl + 5, fees_paid=5.0, net_pnl=net_pnl,
            slippage_bps=2.0, hold_duration_sec=random.randint(30, 300),
            mfe=random.uniform(50, 200), mae=random.uniform(10, 100),
        )

    # Add some skips
    gate_skip = EntryGateResult(passed=False)
    gate_skip.add_block("Regime: RANGING blocks")
    for i in range(20):
        dc.save_trade_decision("SKIP", license, gate_skip, regime, candles, equity=10000)

    dc.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_1_position_scales_with_equity():
    """Test 1: Position size scales with equity (compound growth)."""
    r1 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=0,
    )
    r2 = calculate_position_size(
        equity=10000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=0,
    )
    assert r2.size_usd > r1.size_usd, f"Higher equity should = larger size: ${r1.size_usd:.0f} vs ${r2.size_usd:.0f}"
    ratio = r2.size_usd / r1.size_usd
    assert 1.5 < ratio < 3.0, f"Size ratio should scale roughly proportionally: {ratio:.2f}"
    print(f"  PASS: $5k→${r1.size_usd:.0f}, $10k→${r2.size_usd:.0f} (ratio {ratio:.2f}x)")


def test_2_quality_multiplier():
    """Test 2: Quality multiplier applies correctly."""
    r_aplus = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=92, consecutive_losses=0, drawdown_pct=0,
    )
    r_a = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=82, consecutive_losses=0, drawdown_pct=0,
    )
    r_b = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=72, consecutive_losses=0, drawdown_pct=0,
    )

    assert r_aplus.quality_mult == config.QUALITY_SIZE_MULTIPLIER_A_PLUS  # 1.5
    assert r_a.quality_mult == config.QUALITY_SIZE_MULTIPLIER_A  # 1.2
    assert r_b.quality_mult == config.QUALITY_SIZE_MULTIPLIER_B  # 1.0
    assert r_aplus.size_usd > r_a.size_usd > r_b.size_usd

    # Quality < 70 should be blocked
    r_low = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=60, consecutive_losses=0, drawdown_pct=0,
    )
    assert r_low.halted, "Quality < 70 should be blocked"

    print(f"  PASS: A+=${r_aplus.size_usd:.0f} (1.5x), A=${r_a.size_usd:.0f} (1.2x), "
          f"B=${r_b.size_usd:.0f} (1.0x), <70=blocked")


def test_3_streak_reduction():
    """Test 3: Streak-based reduction (1→0.75, 2→0.5, 3→0.25)."""
    base = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=0,
    )
    r1 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=1, drawdown_pct=0,
    )
    r2 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=2, drawdown_pct=0,
    )
    r3 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=3, drawdown_pct=0,
    )

    assert r1.streak_mult == 0.75
    assert r2.streak_mult == 0.50
    assert r3.streak_mult == 0.25
    assert r1.size_usd < base.size_usd
    assert r2.size_usd < r1.size_usd
    assert r3.size_usd < r2.size_usd

    print(f"  PASS: 0L=${base.size_usd:.0f}, 1L=${r1.size_usd:.0f} (0.75x), "
          f"2L=${r2.size_usd:.0f} (0.5x), 3L=${r3.size_usd:.0f} (0.25x)")


def test_4_drawdown_reduction():
    """Test 4: Drawdown-based reduction (3%→0.7, 5%→0.4, 8%→0.2, 10%→HALT)."""
    base = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=0,
    )
    r3 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=3.5,
    )
    r5 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=6.0,
    )
    r8 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=9.0,
    )
    r10 = calculate_position_size(
        equity=5000, entry_price=100000, sl_price=99650,
        entry_quality=85, consecutive_losses=0, drawdown_pct=11.0,
    )

    assert r3.drawdown_mult == 0.70
    assert r5.drawdown_mult == 0.40
    assert r8.drawdown_mult == 0.20
    assert r10.halted, "DD ≥ 10% should HALT"
    assert r10.size_usd == 0

    print(f"  PASS: 0%=${base.size_usd:.0f}, 3.5%=${r3.size_usd:.0f} (0.7x), "
          f"6%=${r5.size_usd:.0f} (0.4x), 9%=${r8.size_usd:.0f} (0.2x), 11%=HALT")


def test_5_daily_loss_halts():
    """Test 5: Daily loss limit halts trading."""
    r = calculate_position_size(
        equity=10000, entry_price=100000, sl_price=99650,
        entry_quality=85, daily_pnl=-350,  # -3.5% > 3%
    )
    assert r.halted, "Daily loss should halt"
    assert "Daily" in r.halt_reason
    print(f"  PASS: Daily loss -3.5% → HALT ({r.halt_reason})")


def test_6_weekly_loss_halts():
    """Test 6: Weekly loss limit halts trading."""
    r = calculate_position_size(
        equity=10000, entry_price=100000, sl_price=99650,
        entry_quality=85, weekly_pnl=-750,  # -7.5% > 7%
    )
    assert r.halted, "Weekly loss should halt"
    assert "Weekly" in r.halt_reason
    print(f"  PASS: Weekly loss -7.5% → HALT ({r.halt_reason})")


def test_7_equity_floor():
    """Test 7: Equity floor circuit breaker."""
    r = calculate_position_size(
        equity=400, entry_price=100000, sl_price=99650,
        entry_quality=85,
    )
    assert r.halted, "Below equity floor should halt"
    assert "floor" in r.halt_reason.lower()
    print(f"  PASS: Equity $400 < floor ${config.EQUITY_FLOOR_USDT} → HALT")


def test_8_max_leverage_cap():
    """Test 8: Max leverage cap never exceeded."""
    # Very small SL distance → would normally create huge leverage
    r = calculate_position_size(
        equity=1000, entry_price=100000, sl_price=99999,  # $1 risk
        entry_quality=92, consecutive_losses=0, drawdown_pct=0,
    )
    assert not r.halted
    assert r.leverage <= config.MAX_LEVERAGE, \
        f"Leverage {r.leverage:.1f}x > max {config.MAX_LEVERAGE}x"
    print(f"  PASS: Leverage capped at {r.leverage:.1f}x (max {config.MAX_LEVERAGE}x)")


def test_9_growth_tier_transitions():
    """Test 9: Growth tiers transition at correct equity thresholds."""
    tests = [
        (800, "STARTER"),
        (1000, "TIER_1"),
        (2500, "TIER_2"),
        (5000, "TIER_3"),
        (10000, "TIER_4"),
    ]
    for equity, expected_tier in tests:
        r = calculate_position_size(
            equity=equity, entry_price=100000, sl_price=99650,
            entry_quality=85,
        )
        assert r.tier == expected_tier, f"Equity ${equity} should be {expected_tier}, got {r.tier}"

    print(f"  PASS: All 5 tier transitions correct")


def test_10_analytics_sufficient_data():
    """Test 10: Analytics engine produces valid output with sufficient sample."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        seed_trades(db_path, count=60)
        report = run_full_report(db_path, min_sample=10)

        assert report["sufficient_data"], "Should have sufficient data"
        assert report["trade_count"] == 60
        assert "win_rate_decomposition" in report
        assert "regime" in report["win_rate_decomposition"]
        assert "factor_correlation" in report
        assert "mfe_mae" in report
        assert "regime_matrix" in report
        assert "fee_impact" in report
        assert "skip_analysis" in report
        assert "recommendations" in report

        # WR decomposition should have data
        regime_wr = report["win_rate_decomposition"]["regime"]
        assert len(regime_wr) > 0, "Should have regime WR data"

        print(f"  PASS: Analytics produced full report ({report['trade_count']} trades, "
              f"{len(report['recommendations'])} recommendations)")
    finally:
        os.unlink(db_path)


def test_11_analytics_sparse_data():
    """Test 11: Analytics engine flags insufficient data."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        seed_trades(db_path, count=3)
        report = run_full_report(db_path, min_sample=50)

        assert not report["sufficient_data"], "Should flag insufficient data"
        assert report["trade_count"] == 3
        # Recommendations should mention insufficient data
        recs = report.get("recommendations", [])
        assert any("Insufficient" in r for r in recs), f"Should mention insufficient data: {recs}"

        print(f"  PASS: Sparse data flagged correctly ({report['trade_count']} trades < 50 min)")
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all_tests():
    tests = [
        ("1.  Position scales with equity", test_1_position_scales_with_equity),
        ("2.  Quality multiplier", test_2_quality_multiplier),
        ("3.  Streak reduction", test_3_streak_reduction),
        ("4.  Drawdown reduction", test_4_drawdown_reduction),
        ("5.  Daily loss halts", test_5_daily_loss_halts),
        ("6.  Weekly loss halts", test_6_weekly_loss_halts),
        ("7.  Equity floor breaker", test_7_equity_floor),
        ("8.  Max leverage cap", test_8_max_leverage_cap),
        ("9.  Growth tier transitions", test_9_growth_tier_transitions),
        ("10. Analytics sufficient data", test_10_analytics_sufficient_data),
        ("11. Analytics sparse data", test_11_analytics_sparse_data),
    ]

    print("=" * 60)
    print(" CHUNK 5 TESTS — Analytics + Position Sizing + Capital Protection")
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
