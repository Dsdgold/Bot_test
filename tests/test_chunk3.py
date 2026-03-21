"""Tests for Chunk 3 — Microstructure, Execution, Kill Switches, Cooldowns."""

import sys
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trading_agent.models import (
    Action, CandleData, DirectionalLicense, HTFAlignment,
    Regime, RegimeState, SetupType,
)
from trading_agent.indicators import compute_cvd, check_cvd_alignment, check_oi_confirmation
from trading_agent.risk_manager import (
    CooldownState, calculate_dynamic_sl_tp, check_kill_switches, check_cooldowns,
)
from trading_agent.strategy import (
    evaluate_entry_gates, check_cvd, check_oi, check_session_filter,
)
from trading_agent import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_trending_candles(n=50, direction="up", volume=2000):
    candles = []
    base = 100000.0
    for i in range(n):
        if direction == "up":
            price = base + i * 100
            o, c = price, price + 50
        else:
            price = base - i * 100
            o, c = price, price - 50
        h = max(o, c) + 30
        l = min(o, c) - 20
        candles.append(CandleData(
            timestamp=datetime(2025, 1, 1, i % 24, 0, tzinfo=timezone.utc),
            open=o, high=h, low=l, close=c, volume=volume,
        ))
    return candles


def make_cvd_divergent_candles(n=50):
    """Price going up but selling pressure (bearish closes) → CVD divergence for LONG."""
    candles = []
    base = 100000.0
    for i in range(n):
        price = base + i * 50  # Price rising
        # But candles close near lows → negative delta
        o = price + 40
        c = price - 30
        h = price + 60
        l = price - 50
        candles.append(CandleData(
            timestamp=datetime(2025, 1, 1, i % 24, 0, tzinfo=timezone.utc),
            open=o, high=h, low=l, close=c, volume=1000,
        ))
    return candles


def make_license(**kwargs):
    defaults = dict(
        action=Action.LONG, confidence=80, entry_quality=85,
        regime=Regime.TRENDING, setup_type=SetupType.CONTINUATION,
        htf_alignment=HTFAlignment.ALIGNED, reason="test",
    )
    defaults.update(kwargs)
    return DirectionalLicense(**defaults)


def make_regime(**kwargs):
    defaults = dict(
        regime=Regime.TRENDING, adx=30, chop=40, atr_pct=0.3,
        bb_width=1.5, ema_slope=0.001,
    )
    defaults.update(kwargs)
    return RegimeState(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_1_cvd_divergence_blocks():
    """Test 1: CVD divergence → WAIT."""
    candles = make_cvd_divergent_candles()
    # Price going up, but CVD should be falling (bearish closes)
    ok, reason = check_cvd_alignment(candles, "LONG")
    assert not ok, f"CVD divergence should block LONG: {reason}"
    print(f"  PASS: CVD divergence blocks LONG ({reason})")


def test_2_oi_dropping_on_breakout_blocks():
    """Test 2: OI dropping on breakout → WAIT."""
    ok, reason = check_oi_confirmation(
        oi_current=50000, oi_previous=55000,  # OI dropping
        price_new_extreme=True,
    )
    assert not ok, f"Dropping OI at price extreme should block: {reason}"
    print(f"  PASS: OI drop on breakout blocks ({reason})")


def test_3_dynamic_sl_tp_sane_levels():
    """Test 3: Dynamic SL/TP produces sane levels within guardrails."""
    candles = make_trending_candles()
    license = make_license(entry_quality=90)
    entry_price = candles[-1].close

    sl_tp = calculate_dynamic_sl_tp(entry_price, license, candles, is_maker=True)

    # SL must be below entry for LONG
    assert sl_tp.stop_loss < entry_price, f"SL {sl_tp.stop_loss} should be < entry {entry_price}"
    # TP must be above entry for LONG
    assert sl_tp.take_profit > entry_price, f"TP {sl_tp.take_profit} should be > entry {entry_price}"
    # SL% within guardrails
    assert config.MIN_SL_PCT <= sl_tp.sl_pct <= config.MAX_SL_PCT, \
        f"SL% {sl_tp.sl_pct} outside guardrails [{config.MIN_SL_PCT}, {config.MAX_SL_PCT}]"
    # TP% within guardrails
    assert config.MIN_TP_PCT <= sl_tp.tp_pct <= config.MAX_TP_PCT, \
        f"TP% {sl_tp.tp_pct} outside guardrails [{config.MIN_TP_PCT}, {config.MAX_TP_PCT}]"
    # Net R:R must be positive
    assert sl_tp.net_rr > 0, f"Net R:R should be positive: {sl_tp.net_rr}"

    print(f"  PASS: SL={sl_tp.stop_loss:.1f} ({sl_tp.sl_pct:.2f}%), "
          f"TP={sl_tp.take_profit:.1f} ({sl_tp.tp_pct:.2f}%), "
          f"net_RR={sl_tp.net_rr:.2f}")


def test_4_fee_aware_rr_blocks():
    """Test 4: Fee-aware net R:R below MIN_NET_RR → blocked."""
    candles = make_trending_candles()
    # Low quality → tighter target → worse R:R
    license = make_license(entry_quality=50)
    entry_price = candles[-1].close

    # Force taker fees (higher) and set a high MIN_NET_RR to trigger block
    orig_min_rr = config.MIN_NET_RR
    config.MIN_NET_RR = 5.0  # Unreasonably high to force failure

    sl_tp = calculate_dynamic_sl_tp(entry_price, license, candles, is_maker=False)
    assert not sl_tp.is_valid, f"Net R:R {sl_tp.net_rr:.2f} should be < {config.MIN_NET_RR}"

    config.MIN_NET_RR = orig_min_rr
    print(f"  PASS: Net R:R {sl_tp.net_rr:.2f} below threshold → blocked")


def test_5_spread_kills():
    """Test 5: Spread exceeds MAX_SPREAD_TOLERANCE_USDT → all entries frozen."""
    state = check_kill_switches(spread=5.0)  # Way above 2.5 default
    assert state.spread_frozen, "Wide spread should freeze"
    assert state.any_active, "Kill switch should be active"
    print(f"  PASS: Spread {5.0} > {config.MAX_SPREAD_TOLERANCE_USDT} → frozen")


def test_6_extreme_funding_blocks():
    """Test 6: Extreme funding rate → continuation blocked."""
    # Positive funding (longs pay) + LONG trade → blocked
    state = check_kill_switches(
        funding_rate=0.005,  # Way above 0.001 default
        trade_direction="LONG",
    )
    assert state.funding_frozen, "Extreme funding should freeze LONG"

    # Same funding but SHORT trade → NOT blocked (shorts receive)
    state2 = check_kill_switches(
        funding_rate=0.005,
        trade_direction="SHORT",
    )
    assert not state2.funding_frozen, "Positive funding should NOT freeze SHORT"
    print(f"  PASS: Extreme funding blocks continuation direction only")


def test_7_latency_kills():
    """Test 7: API latency exceeds threshold → trading paused."""
    state = check_kill_switches(latency_ms=1000)  # Above 500ms default
    assert state.latency_frozen, "High latency should freeze"
    assert state.any_active
    print(f"  PASS: Latency {1000}ms > {config.LATENCY_KILL_SWITCH_MS}ms → frozen")


def test_8_post_loss_cooldown():
    """Test 8: Post-loss cooldown active → blocked."""
    cd = CooldownState()
    cd.last_loss_time = time.time()  # Loss just happened

    ok, reason = check_cooldowns(cd, "LONG", current_candle_index=100)
    assert not ok, f"Post-loss cooldown should block: {reason}"
    print(f"  PASS: Post-loss cooldown blocks ({reason})")


def test_9_same_side_losses_freeze():
    """Test 9: Same-side consecutive losses exceed limit → side frozen."""
    cd = CooldownState()
    # Record consecutive LONG losses
    cd.record_trade("LONG", is_win=False, candle_index=1)
    cd.record_trade("LONG", is_win=False, candle_index=2)

    ok, reason = check_cooldowns(cd, "LONG", current_candle_index=100)
    assert not ok, f"Same-side losses should freeze LONG: {reason}"

    # SHORT should still be allowed (after re-entry cooldown passes)
    cd.last_loss_time = 0  # Clear post-loss cooldown for clean test
    cd.last_trade_candle_index = -100  # Clear re-entry cooldown
    ok2, reason2 = check_cooldowns(cd, "SHORT", current_candle_index=100)
    assert ok2, f"SHORT should not be frozen: {reason2}"

    print(f"  PASS: LONG frozen after {config.SAME_SIDE_LOSS_PAUSE_COUNT} losses, SHORT still ok")


def test_10_blocked_session_hour():
    """Test 10: Blocked session hour → blocked."""
    orig = config.BLOCKED_HOURS_LOCAL
    orig_enabled = config.SESSION_FILTER_ENABLED

    # Get current local hour and block it
    from datetime import datetime as dt, timezone as tz
    try:
        from zoneinfo import ZoneInfo
        now = dt.now(tz.utc).astimezone(ZoneInfo(config.TIMEZONE))
        current_hour = now.hour
    except Exception:
        current_hour = dt.now(tz.utc).hour

    config.SESSION_FILTER_ENABLED = True
    config.BLOCKED_HOURS_LOCAL = [current_hour]

    ok, reason = check_session_filter()
    assert not ok, f"Blocked hour should block: {reason}"

    config.BLOCKED_HOURS_LOCAL = orig
    config.SESSION_FILTER_ENABLED = orig_enabled
    print(f"  PASS: Blocked hour {current_hour}:00 → blocked ({reason})")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all_tests():
    tests = [
        ("1.  CVD divergence blocks", test_1_cvd_divergence_blocks),
        ("2.  OI dropping on breakout blocks", test_2_oi_dropping_on_breakout_blocks),
        ("3.  Dynamic SL/TP sane levels", test_3_dynamic_sl_tp_sane_levels),
        ("4.  Fee-aware R:R blocks", test_4_fee_aware_rr_blocks),
        ("5.  Spread kills all entries", test_5_spread_kills),
        ("6.  Extreme funding blocks continuation", test_6_extreme_funding_blocks),
        ("7.  Latency kills trading", test_7_latency_kills),
        ("8.  Post-loss cooldown blocks", test_8_post_loss_cooldown),
        ("9.  Same-side losses freeze side", test_9_same_side_losses_freeze),
        ("10. Blocked session hour", test_10_blocked_session_hour),
    ]

    print("=" * 60)
    print(" CHUNK 3 TESTS — Microstructure, Execution, Kill Switches")
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
            failed += 1

    print(f"\n{'=' * 60}")
    print(f" Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 60}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
