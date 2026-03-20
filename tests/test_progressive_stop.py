"""
Tests for the progressive profit-locking stop logic.
"""
import pytest
from dataclasses import dataclass
from trading_agent.risk_manager import RiskManager
from trading_agent.models import Side


@dataclass
class FakeConfig:
    """Minimal config stub for RiskManager."""
    progressive_stop_enabled: bool = True
    profit_step_net_usd: float = 4.0
    lock_step_net_usd: float = 1.0
    taker_fee_rate: float = 0.00055
    slippage_buffer_usd: float = 0.40
    min_stop_improvement_usd: float = 0.25
    disable_legacy_profit_protection: bool = True
    # fields required by RiskManager.__init__
    max_daily_loss_pct: float = 25.0
    cooldown_after_trade: int = 120
    min_hold_time: int = 120


def make_rm(step=4.0, lock=1.0, fee=0.00055, slip=0.40, min_imp=0.25):
    cfg = FakeConfig(
        profit_step_net_usd=step,
        lock_step_net_usd=lock,
        taker_fee_rate=fee,
        slippage_buffer_usd=slip,
        min_stop_improvement_usd=min_imp,
    )
    return RiskManager(cfg)


# ── Helpers ──────────────────────────────────────────────────────────

def fee_buffer(entry, exit_price, qty, fee=0.00055, slip=0.40):
    return entry * qty * fee + exit_price * qty * fee + slip


# ── 1. LONG: no first step ──────────────────────────────────────────

class TestLongNoStep:
    def test_net_pnl_below_step_returns_none(self):
        rm = make_rm()
        # LONG: entry=84000, qty=0.001, lev=20, current=84050
        # gross = (84050-84000)*0.001*20 = $1.00
        # fee_buf ≈ 84000*0.001*0.00055 + 84050*0.001*0.00055 + 0.40 ≈ 0.0462+0.04623+0.40 ≈ 0.4924
        # net ≈ 1.0 - 0.49 ≈ 0.51 → steps = floor(0.51/4) = 0 → None
        result = rm.calculate_progressive_stop(
            entry_price=84000.0,
            quantity=0.001,
            leverage=20,
            side=Side.LONG,
            current_price=84050.0,
            current_sl=83000.0,
        )
        assert result is None

    def test_negative_pnl_returns_none(self):
        rm = make_rm()
        result = rm.calculate_progressive_stop(
            entry_price=84000.0,
            quantity=0.001,
            leverage=20,
            side=Side.LONG,
            current_price=83900.0,
            current_sl=83000.0,
        )
        assert result is None


# ── 2. LONG: first step ─────────────────────────────────────────────

class TestLongFirstStep:
    def test_first_step_locks_profit(self):
        rm = make_rm()
        entry = 84000.0
        qty = 0.001
        lev = 20
        # We need net >= $4.0. gross must be > $4.0 + fee_buffer
        # gross = (cp - entry) * qty * lev → need cp such that gross ≈ $5
        # (cp - 84000)*0.001*20 = 5 → cp - 84000 = 250 → cp = 84250
        cp = 84250.0
        fb = fee_buffer(entry, cp, qty)
        gross = (cp - entry) * qty * lev  # = $5.0
        net = gross - fb  # ≈ 4.51
        assert net >= 4.0  # sanity

        result = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp, current_sl=83000.0,
        )
        assert result is not None
        # New SL should be above entry (locking profit)
        assert result > entry
        # New SL should be below current price
        assert result < cp

    def test_locked_amount_is_correct(self):
        rm = make_rm(step=4.0, lock=1.0)
        entry = 84000.0
        qty = 0.001
        lev = 20
        cp = 84250.0
        fb = fee_buffer(entry, cp, qty)

        result = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp, current_sl=83000.0,
        )
        # locked_gross = fb + 1*1.0
        # new_sl = entry + locked_gross / (qty * lev)
        locked_gross = fb + 1.0
        expected_sl = round(entry + locked_gross / (qty * lev), 2)
        assert result == expected_sl


# ── 3. LONG: second step ────────────────────────────────────────────

class TestLongSecondStep:
    def test_second_step_higher_than_first(self):
        rm = make_rm(step=4.0, lock=1.0)
        entry = 84000.0
        qty = 0.001
        lev = 20

        # First step at cp=84250 (net ≈ 4.5)
        cp1 = 84250.0
        sl1 = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp1, current_sl=83000.0,
        )
        assert sl1 is not None

        # Second step: need net >= $8.0
        # gross = (cp - 84000)*0.001*20 → need gross ≈ 9.0 → cp-84000=450 → cp=84450
        cp2 = 84450.0
        sl2 = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp2, current_sl=sl1,
        )
        assert sl2 is not None
        assert sl2 > sl1  # never goes back


# ── 4. SHORT: first step ────────────────────────────────────────────

class TestShortFirstStep:
    def test_short_first_step(self):
        rm = make_rm()
        entry = 84000.0
        qty = 0.001
        lev = 20
        # SHORT profit: entry > cp → cp = 83750
        # gross = (84000-83750)*0.001*20 = $5.0
        cp = 83750.0
        fb = fee_buffer(entry, cp, qty)
        net = 5.0 - fb
        assert net >= 4.0

        result = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.SHORT, current_price=cp, current_sl=85000.0,
        )
        assert result is not None
        # SL should be below entry (locking SHORT profit)
        assert result < entry
        # SL should be above current price
        assert result > cp


# ── 5. Never go backwards ───────────────────────────────────────────

class TestNeverGoBack:
    def test_worse_sl_rejected_long(self):
        rm = make_rm()
        # If current SL is already very good, a worse SL should be rejected
        result = rm.calculate_progressive_stop(
            entry_price=84000.0, quantity=0.001, leverage=20,
            side=Side.LONG, current_price=84250.0, current_sl=84200.0,
        )
        # The calculated SL for step 1 would be around 84000 + locked_gross/0.02
        # which is well below 84200 → should return None
        assert result is None

    def test_worse_sl_rejected_short(self):
        rm = make_rm()
        result = rm.calculate_progressive_stop(
            entry_price=84000.0, quantity=0.001, leverage=20,
            side=Side.SHORT, current_price=83750.0, current_sl=83800.0,
        )
        # Calculated SL for step 1 would be around 84000 - locked_gross/0.02
        # which is above 83800 → should return None
        assert result is None


# ── 6. Minimum improvement ──────────────────────────────────────────

class TestMinImprovement:
    def test_tiny_improvement_rejected(self):
        rm = make_rm(min_imp=10.0)  # Very high threshold
        entry = 84000.0
        qty = 0.001
        lev = 20
        cp = 84250.0

        # Step 1 SL is slightly above a current SL near it
        fb = fee_buffer(entry, cp, qty)
        locked_gross = fb + 1.0
        expected_sl = entry + locked_gross / (qty * lev)
        # Set current SL just barely below expected → improvement < $10
        current_sl = expected_sl - 0.1  # tiny diff

        result = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp, current_sl=current_sl,
        )
        # improvement_usd = 0.1 * 0.001 * 20 = $0.002 << $10
        assert result is None


# ── 7. Restart-safe: recalculates from raw data ─────────────────────

class TestRestartSafe:
    def test_recalculates_from_scratch(self):
        """After restart, same inputs produce same SL — no saved state needed."""
        rm = make_rm()
        entry = 84000.0
        qty = 0.001
        lev = 20
        cp = 84450.0

        # Simulate first run
        sl1 = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp, current_sl=83000.0,
        )

        # Simulate restart: brand new RiskManager, same market state
        rm2 = make_rm()
        sl2 = rm2.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp, current_sl=83000.0,
        )

        assert sl1 == sl2  # Deterministic, no hidden state


# ── 8. Configurable step sizes ──────────────────────────────────────

class TestConfigurable:
    def test_aggressive_config(self):
        """2.0/0.5 config should trigger earlier."""
        rm = make_rm(step=2.0, lock=0.5)
        entry = 84000.0
        qty = 0.001
        lev = 20
        # cp=84150 → gross=$3.0, fb≈0.49 → net≈2.51 → steps=floor(2.51/2)=1
        cp = 84150.0
        result = rm.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp, current_sl=83000.0,
        )
        assert result is not None

        # Same price with default 4.0/1.0 → net≈2.51 → steps=0 → None
        rm_default = make_rm(step=4.0, lock=1.0)
        result_default = rm_default.calculate_progressive_stop(
            entry_price=entry, quantity=qty, leverage=lev,
            side=Side.LONG, current_price=cp, current_sl=83000.0,
        )
        assert result_default is None


# ── 9. Edge: SL would cross current price ───────────────────────────

class TestEdgeCases:
    def test_sl_above_current_price_rejected_long(self):
        """If locked_gross is so large that SL > current_price, reject."""
        rm = make_rm(step=0.1, lock=50.0)  # absurd lock
        result = rm.calculate_progressive_stop(
            entry_price=84000.0, quantity=0.001, leverage=20,
            side=Side.LONG, current_price=84250.0, current_sl=83000.0,
        )
        assert result is None

    def test_zero_step_returns_none(self):
        rm = make_rm(step=0.0)
        result = rm.calculate_progressive_stop(
            entry_price=84000.0, quantity=0.001, leverage=20,
            side=Side.LONG, current_price=84250.0, current_sl=83000.0,
        )
        assert result is None
