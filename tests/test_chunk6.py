"""Tests for Chunk 6 — Token Optimization, Learning Journal, Self-Optimization."""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trading_agent import config
from trading_agent.models import (
    Action, CandleData, DirectionalLicense, Regime, RegimeState, SetupType, HTFAlignment,
)
from trading_agent.risk_manager import CooldownState
from trading_agent.token_manager import (
    CachedLicense, PreFilterResult, TokenBudgetTracker,
    build_compressed_prompt, run_pre_filters,
)
from trading_agent.learning_journal import LearningJournal
from trading_agent.self_optimizer import SelfOptimizer, TIER_3_PARAMS, ALL_PARAM_SPECS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path

def make_ranging_candles(n=50):
    candles = []
    base = 100000.0
    for i in range(n):
        offset = 150 * (1 if i % 2 == 0 else -1)
        o = base + offset
        c = base - offset
        h = max(o, c) + 80
        l = min(o, c) - 80
        candles.append(CandleData(
            timestamp=datetime(2025, 1, 1, i % 24, 0, tzinfo=timezone.utc),
            open=o, high=h, low=l, close=c, volume=500,
        ))
    return candles

def make_trending_candles(n=50):
    candles = []
    base = 100000.0
    for i in range(n):
        p = base + i * 100
        candles.append(CandleData(
            timestamp=datetime(2025, 1, 1, i % 24, 0, tzinfo=timezone.utc),
            open=p, high=p + 60, low=p - 20, close=p + 50, volume=2000,
        ))
    return candles

def make_license(**kw):
    defaults = dict(action=Action.LONG, confidence=80, entry_quality=85,
                    regime=Regime.TRENDING, setup_type=SetupType.CONTINUATION,
                    htf_alignment=HTFAlignment.ALIGNED, reason="test")
    defaults.update(kw)
    return DirectionalLicense(**defaults)


# ---------------------------------------------------------------------------
# 1-2: Pre-filters block LLM calls
# ---------------------------------------------------------------------------

def test_1_prefilter_blocks_ranging():
    """Test 1: Pre-filters prevent LLM call when regime is RANGING."""
    candles = make_ranging_candles()
    result = run_pre_filters(candles)
    assert not result.passed, "RANGING should be pre-filtered"
    assert not result.llm_call_needed, "LLM should NOT be called"
    assert any("Regime" in r or "RANGING" in r or "DEAD" in r for r in result.reasons), \
        f"Should mention regime: {result.reasons}"
    print(f"  PASS: Pre-filter blocks RANGING ({result.reasons[0]})")

def test_2_prefilter_blocks_kill_switch():
    """Test 2: Pre-filters prevent LLM call when kill switch is active."""
    candles = make_trending_candles()
    result = run_pre_filters(candles, spread=10.0)
    assert not result.passed
    assert not result.llm_call_needed
    assert any("Kill" in r for r in result.reasons)
    print(f"  PASS: Pre-filter blocks on kill switch ({result.reasons[0]})")


# ---------------------------------------------------------------------------
# 3-5: License cache
# ---------------------------------------------------------------------------

def test_3_cache_serves_within_ttl():
    """Test 3: Cache serves cached result within TTL."""
    lic = make_license()
    cache = CachedLicense(license=lic, cached_at=time.time(),
                          cached_price=100000, cached_atr=50,
                          cached_regime=Regime.TRENDING)
    invalid, _ = cache.should_invalidate(100010, 50, Regime.TRENDING, 1.0)
    assert not invalid, "Should not invalidate within TTL"
    assert not cache.is_expired
    print("  PASS: Cache valid within TTL")

def test_4_cache_invalidates_atr_move():
    """Test 4: Cache invalidates on ATR move > threshold."""
    lic = make_license()
    cache = CachedLicense(license=lic, cached_at=time.time(),
                          cached_price=100000, cached_atr=50,
                          cached_regime=Regime.TRENDING)
    # Move 2 ATR (threshold is 1.0)
    invalid, reason = cache.should_invalidate(100100, 50, Regime.TRENDING, 1.0)
    assert invalid, f"Should invalidate on 2 ATR move: {reason}"
    assert "ATR" in reason
    print(f"  PASS: Cache invalidates on ATR move ({reason})")

def test_5_cache_invalidates_vol_spike():
    """Test 5: Cache invalidates on volume spike > threshold."""
    lic = make_license()
    cache = CachedLicense(license=lic, cached_at=time.time(),
                          cached_price=100000, cached_atr=50,
                          cached_regime=Regime.TRENDING)
    invalid, reason = cache.should_invalidate(100000, 50, Regime.TRENDING, 4.0)
    assert invalid, "Should invalidate on vol spike"
    assert "spike" in reason.lower()
    print(f"  PASS: Cache invalidates on vol spike ({reason})")


# ---------------------------------------------------------------------------
# 6-8: Token budget
# ---------------------------------------------------------------------------

def test_6_token_tracking():
    """Test 6: Token usage tracked per call type."""
    db = _tmp_db()
    try:
        tracker = TokenBudgetTracker(db_path=db)
        tracker.record_usage("license", 500, 100, model="claude")
        tracker.record_usage("journal", 200, 80, model="claude")
        assert tracker.daily_total == 880, f"Expected 880, got {tracker.daily_total}"
        tracker.close()
        print(f"  PASS: Token tracking accumulates correctly ({tracker.daily_total})")
    finally:
        os.unlink(db)

def test_7_budget_alert():
    """Test 7: Budget alert triggers at configured %."""
    db = _tmp_db()
    try:
        tracker = TokenBudgetTracker(db_path=db)
        orig = config.DAILY_TOKEN_BUDGET
        config.DAILY_TOKEN_BUDGET = 1000
        tracker.record_usage("test", 400, 400)  # 800/1000 = 80%
        assert tracker.budget_alert, f"Should alert at {tracker.budget_pct:.0f}%"
        config.DAILY_TOKEN_BUDGET = orig
        tracker.close()
        print(f"  PASS: Budget alert at {tracker.budget_pct:.0f}%")
    finally:
        os.unlink(db)

def test_8_budget_hard_stop():
    """Test 8: Budget hard stop switches to deterministic-only."""
    db = _tmp_db()
    try:
        tracker = TokenBudgetTracker(db_path=db)
        orig = config.DAILY_TOKEN_BUDGET
        config.DAILY_TOKEN_BUDGET = 100
        tracker.record_usage("test", 60, 50)  # 110 > 100
        assert tracker.budget_exceeded
        assert tracker.should_use_deterministic_only
        config.DAILY_TOKEN_BUDGET = orig
        tracker.close()
        print("  PASS: Budget hard stop → deterministic-only mode")
    finally:
        os.unlink(db)


# ---------------------------------------------------------------------------
# 9-10: Compressed prompt
# ---------------------------------------------------------------------------

def test_9_compressed_prompt():
    """Test 9: Compressed prompt has all fields, no raw candles."""
    candles = make_trending_candles()
    regime = RegimeState(regime=Regime.TRENDING, adx=25, chop=45,
                         atr_pct=0.3, bb_width=1.5, ema_slope=0.001)
    prompt = build_compressed_prompt(candles, candles[:20], candles[:10], None,
                                     regime, spread=1.5, funding_rate=0.0003)
    assert "BTCUSDT" in prompt
    assert "EMA9" in prompt
    assert "RSI" in prompt
    assert "ADX" in prompt
    assert "Regime:" in prompt
    assert "JSON" in prompt
    # Should NOT contain raw OHLCV
    assert "open=" not in prompt.lower() or "O=" not in prompt
    print(f"  PASS: Compressed prompt ({len(prompt)} chars, no raw candles)")

def test_10_max_tokens_enforced():
    """Test 10: LLM max_tokens is configured."""
    assert config.LLM_MAX_TOKENS_LICENSE == 200
    assert config.JOURNAL_MAX_TOKENS_POST_TRADE == 150
    print(f"  PASS: max_tokens configured (license={config.LLM_MAX_TOKENS_LICENSE}, journal={config.JOURNAL_MAX_TOKENS_POST_TRADE})")


# ---------------------------------------------------------------------------
# 11-13: Learning journal
# ---------------------------------------------------------------------------

def test_11_journal_post_win():
    """Test 11: Journal creates POST_WIN after winning trade."""
    db = _tmp_db()
    try:
        journal = LearningJournal(db_path=db)
        rid = journal.record_post_trade("t001", is_win=True, net_pnl=42.0,
                                         entry_quality=85, confidence=78,
                                         regime="TRENDING", setup_type="CONTINUATION",
                                         mfe=100, mae=20, hold_sec=120)
        assert rid > 0
        entries = journal.get_recent_entries(1)
        assert entries[0]["entry_type"] == "POST_WIN"
        journal.close()
        print(f"  PASS: POST_WIN journal entry created (id={rid})")
    finally:
        os.unlink(db)

def test_12_journal_post_loss():
    """Test 12: Journal creates POST_LOSS with deeper reflection."""
    db = _tmp_db()
    try:
        journal = LearningJournal(db_path=db)
        rid = journal.record_post_trade("t002", is_win=False, net_pnl=-25.0,
                                         entry_quality=72, confidence=80,
                                         regime="TRENDING", setup_type="CONTINUATION",
                                         mfe=30, mae=60, hold_sec=90,
                                         entry_price=100000, sl_price=99650)
        assert rid > 0
        entries = journal.get_recent_entries(1)
        assert entries[0]["entry_type"] == "POST_LOSS"
        assert "overconfident" in entries[0]["conclusion"].lower() or "reversing" in entries[0]["conclusion"].lower() \
            or "Normal" in entries[0]["conclusion"]
        journal.close()
        print(f"  PASS: POST_LOSS journal entry with reflection (id={rid})")
    finally:
        os.unlink(db)

def test_13_journal_skip_review():
    """Test 13: Skip review runs at configured interval."""
    db = _tmp_db()
    try:
        journal = LearningJournal(db_path=db)
        journal._last_skip_review = 0  # Force review
        assert journal.should_run_skip_review()
        rid = journal.record_skip_review(
            skip_count=180, top_reasons={"Regime": 100, "Volume": 50, "Extension": 30},
            total_decisions=200,
        )
        assert rid > 0
        assert not journal.should_run_skip_review()  # Just ran
        entries = journal.get_recent_entries(1)
        assert entries[0]["entry_type"] == "POST_SKIP_REVIEW"
        journal.close()
        print(f"  PASS: Skip review created (id={rid})")
    finally:
        os.unlink(db)


# ---------------------------------------------------------------------------
# 14-22: Self-optimizer
# ---------------------------------------------------------------------------

def test_14_tier1_auto_adjusts():
    """Test 14: Tier 1 auto-adjusts within bounds."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig = config.MIN_CONFIDENCE
        ok, reason = opt.apply_change("MIN_CONFIDENCE", orig + 2,
                                       trigger="test", sample_size=50, confidence=75)
        assert ok, f"Tier 1 should auto-apply: {reason}"
        assert config.MIN_CONFIDENCE == orig + 2
        config.MIN_CONFIDENCE = orig
        opt.close()
        print(f"  PASS: Tier 1 auto-adjusts ({reason})")
    finally:
        os.unlink(db)

def test_15_tier1_bounds_enforced():
    """Test 15: Tier 1 cannot exceed bounds."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        ok, reason = opt.apply_change("MIN_CONFIDENCE", 999,
                                       trigger="test", sample_size=50, confidence=75)
        assert not ok, "Should reject out-of-bounds"
        assert "max" in reason.lower()
        opt.close()
        print(f"  PASS: Tier 1 bounds enforced ({reason})")
    finally:
        os.unlink(db)

def test_16_tier2_enters_probation():
    """Test 16: Tier 2 enters probation after auto-apply."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig = config.TARGET_RR_A_PLUS
        ok, reason = opt.apply_change("TARGET_RR_A_PLUS", orig + 0.1,
                                       trigger="test", sample_size=50, confidence=75)
        assert ok, f"Tier 2 should enter probation: {reason}"
        assert "probation" in reason.lower()
        history = opt.get_history(1)
        assert history[0]["status"] == "PROBATION"
        config.TARGET_RR_A_PLUS = orig
        opt.close()
        print(f"  PASS: Tier 2 enters probation ({reason})")
    finally:
        os.unlink(db)

def test_17_tier2_rollback():
    """Test 17: Tier 2 rolls back when performance worse."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig = config.TARGET_RR_A_PLUS
        opt._trade_counter = 0
        ok, _ = opt.apply_change("TARGET_RR_A_PLUS", orig + 0.1,
                                  trigger="test", sample_size=50, confidence=75)
        assert ok

        # Advance trade counter past probation
        opt._trade_counter = config.PROBATION_TRADES + 1
        rollbacks = opt.check_probations(
            current_wr=40, pre_wr=60,  # WR dropped 20%
            current_dd=2, current_expectancy=0.5,
        )
        assert len(rollbacks) > 0, "Should rollback"
        assert config.TARGET_RR_A_PLUS == orig
        opt.close()
        print(f"  PASS: Tier 2 rolled back (reason: {rollbacks[0]['reason']})")
    finally:
        os.unlink(db)

def test_18_tier3_never_auto():
    """Test 18: Tier 3 is NEVER auto-applied."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig = config.MAX_LEVERAGE
        ok, reason = opt.apply_change("MAX_LEVERAGE", 20,
                                       trigger="test", sample_size=100, confidence=90)
        assert not ok, "Tier 3 should NEVER auto-apply"
        assert config.MAX_LEVERAGE == orig
        assert "Tier 3" in reason or "recommendation" in reason.lower()
        opt.close()
        print(f"  PASS: Tier 3 never auto-applied ({reason})")
    finally:
        os.unlink(db)

def test_19_rollback_lockout():
    """Test 19: Lockout prevents retrying same failed direction."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig = config.TARGET_RR_A

        # Apply and rollback
        opt._trade_counter = 0
        opt.apply_change("TARGET_RR_A", orig + 0.1,
                         trigger="test", sample_size=50, confidence=75)
        opt._trade_counter = config.PROBATION_TRADES + 1
        opt.check_probations(current_wr=35, pre_wr=60, current_dd=2, current_expectancy=0.3)

        # Now try again — should be locked out
        ok, reason = opt.apply_change("TARGET_RR_A", orig + 0.1,
                                       trigger="retry", sample_size=50, confidence=75)
        assert not ok, "Should be locked out"
        assert "locked" in reason.lower()

        config.TARGET_RR_A = orig
        opt.close()
        print(f"  PASS: Lockout prevents retry ({reason})")
    finally:
        os.unlink(db)

def test_20_max_concurrent_probations():
    """Test 20: Max concurrent probations enforced."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig_max = config.MAX_CONCURRENT_PROBATIONS
        config.MAX_CONCURRENT_PROBATIONS = 1

        orig1 = config.TARGET_RR_A_PLUS
        orig2 = config.TARGET_RR_A

        ok1, _ = opt.apply_change("TARGET_RR_A_PLUS", orig1 + 0.1,
                                   trigger="test", sample_size=50, confidence=75)
        assert ok1, "First probation should work"

        ok2, reason = opt.apply_change("TARGET_RR_A", orig2 + 0.1,
                                        trigger="test", sample_size=50, confidence=75)
        assert not ok2, "Should hit probation limit"
        assert "probation" in reason.lower()

        config.TARGET_RR_A_PLUS = orig1
        config.TARGET_RR_A = orig2
        config.MAX_CONCURRENT_PROBATIONS = orig_max
        opt.close()
        print(f"  PASS: Max concurrent probations enforced ({reason})")
    finally:
        os.unlink(db)

def test_21_tuning_freezes_on_drawdown():
    """Test 21: Tuning freezes during drawdown > threshold."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig = config.MIN_CONFIDENCE
        ok, reason = opt.apply_change("MIN_CONFIDENCE", orig + 2,
                                       trigger="test", sample_size=50,
                                       confidence=75, drawdown_pct=6.0)
        assert not ok, "Should freeze during drawdown"
        assert "frozen" in reason.lower() or "drawdown" in reason.lower()
        opt.close()
        print(f"  PASS: Tuning frozen during drawdown ({reason})")
    finally:
        os.unlink(db)

def test_22_audit_trail():
    """Test 22: Full audit trail in parameter_history."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig = config.MIN_CONFIDENCE
        opt.apply_change("MIN_CONFIDENCE", orig + 2,
                         trigger="test_trigger", evidence="test_evidence",
                         sample_size=100, confidence=85)
        history = opt.get_history(1)
        h = history[0]
        assert h["parameter_name"] == "MIN_CONFIDENCE"
        assert float(h["old_value"]) == float(orig)
        assert float(h["new_value"]) == float(orig + 2)
        assert h["tier"] == 1
        assert h["trigger"] == "test_trigger"
        assert h["supporting_evidence"] == "test_evidence"
        assert h["sample_size"] == 100
        config.MIN_CONFIDENCE = orig
        opt.close()
        print(f"  PASS: Full audit trail recorded")
    finally:
        os.unlink(db)

def test_23_emergency_override():
    """Test 23: Emergency override freezes all tuning."""
    db = _tmp_db()
    try:
        opt = SelfOptimizer(db_path=db)
        orig_enabled = config.AUTONOMOUS_TUNING_ENABLED
        config.AUTONOMOUS_TUNING_ENABLED = False

        ok, reason = opt.apply_change("MIN_CONFIDENCE", 65,
                                       trigger="test", sample_size=50, confidence=75)
        assert not ok
        assert "disabled" in reason.lower()

        config.AUTONOMOUS_TUNING_ENABLED = orig_enabled
        opt.close()
        print(f"  PASS: Emergency override freezes all tuning ({reason})")
    finally:
        os.unlink(db)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all_tests():
    tests = [
        ("1.  Pre-filter blocks RANGING", test_1_prefilter_blocks_ranging),
        ("2.  Pre-filter blocks kill switch", test_2_prefilter_blocks_kill_switch),
        ("3.  Cache serves within TTL", test_3_cache_serves_within_ttl),
        ("4.  Cache invalidates on ATR move", test_4_cache_invalidates_atr_move),
        ("5.  Cache invalidates on vol spike", test_5_cache_invalidates_vol_spike),
        ("6.  Token tracking", test_6_token_tracking),
        ("7.  Budget alert", test_7_budget_alert),
        ("8.  Budget hard stop", test_8_budget_hard_stop),
        ("9.  Compressed prompt", test_9_compressed_prompt),
        ("10. Max tokens configured", test_10_max_tokens_enforced),
        ("11. Journal POST_WIN", test_11_journal_post_win),
        ("12. Journal POST_LOSS reflection", test_12_journal_post_loss),
        ("13. Journal skip review", test_13_journal_skip_review),
        ("14. Tier 1 auto-adjusts", test_14_tier1_auto_adjusts),
        ("15. Tier 1 bounds enforced", test_15_tier1_bounds_enforced),
        ("16. Tier 2 enters probation", test_16_tier2_enters_probation),
        ("17. Tier 2 rollback", test_17_tier2_rollback),
        ("18. Tier 3 never auto", test_18_tier3_never_auto),
        ("19. Rollback lockout", test_19_rollback_lockout),
        ("20. Max concurrent probations", test_20_max_concurrent_probations),
        ("21. Tuning freezes on drawdown", test_21_tuning_freezes_on_drawdown),
        ("22. Audit trail", test_22_audit_trail),
        ("23. Emergency override", test_23_emergency_override),
    ]

    print("=" * 60)
    print(" CHUNK 6 TESTS — Token, Journal, Self-Optimization")
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
