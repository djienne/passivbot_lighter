"""Tests for the shared walk-forward live-rolling helpers and config keys.

Covers the boundary handoff rule (should_flatten), the deterministic current-live-window
resolver, and that the new live.wfo_rolling config block survives format_config.
"""

import copy

import pytest

from tools.wfo_handoff import (
    should_flatten,
    current_live_window,
    decide_rolling_state,
    advance_carry,
)


# ---------------------------------------------------------------------------
# Boundary handoff rule
# ---------------------------------------------------------------------------
class TestShouldFlatten:
    def test_profit_always_flattens(self):
        assert should_flatten(10.0, 1000.0) is True

    def test_zero_pnl_flattens(self):
        assert should_flatten(0.0, 1000.0) is True

    def test_small_loss_flattens(self):
        # loss 40 < 5% of 1000 (=50) => flatten
        assert should_flatten(-40.0, 1000.0) is True

    def test_loss_at_threshold_is_kept(self):
        # loss 50 == 5% of 1000 => NOT < threshold => keep
        assert should_flatten(-50.0, 1000.0) is False

    def test_large_loss_is_kept(self):
        assert should_flatten(-200.0, 1000.0) is False

    def test_custom_fraction(self):
        # 10% tolerance: loss 80 < 100 => flatten
        assert should_flatten(-80.0, 1000.0, max_loss_frac=0.10) is True
        assert should_flatten(-120.0, 1000.0, max_loss_frac=0.10) is False

    def test_nonpositive_equity_keeps_loser(self):
        assert should_flatten(-1.0, 0.0) is False
        assert should_flatten(-1.0, -500.0) is False
        # but a profit still flattens regardless of equity sign
        assert should_flatten(5.0, 0.0) is True


# ---------------------------------------------------------------------------
# Current live window
# ---------------------------------------------------------------------------
class TestCurrentLiveWindow:
    ANCHOR = "2025-01-01"

    def _win(self, today):
        return current_live_window(self.ANCHOR, 6, 1, 1, today, calendar_months=True)

    def test_before_first_test_period_returns_none(self):
        assert self._win("2025-06-30") is None

    def test_first_period(self):
        w = self._win("2025-07-15")
        assert w is not None
        assert w.index == 0
        assert w.train_start == "2025-01-01"
        assert w.train_end == "2025-07-01"
        assert w.test_start == "2025-07-01"
        assert w.test_end == "2025-08-01"

    def test_boundary_start_is_inclusive(self):
        w = self._win("2025-07-01")
        assert w.index == 0
        assert w.test_start == "2025-07-01"

    def test_second_period(self):
        w = self._win("2025-08-01")
        assert w.index == 1
        assert w.train_start == "2025-02-01"
        assert w.test_start == "2025-08-01"
        assert w.test_end == "2025-09-01"

    def test_far_future_keeps_rolling(self):
        w = self._win("2026-03-10")
        # test_start <= today < test_end for the containing month
        assert w.test_start <= "2026-03-10" < w.test_end

    def test_invalid_args_raise(self):
        with pytest.raises(ValueError):
            current_live_window(self.ANCHOR, 0, 1, 1, "2025-08-01")


# ---------------------------------------------------------------------------
# Stateful carry between OOS segments
# ---------------------------------------------------------------------------
class TestAdvanceCarry:
    def test_keeps_big_loser_realizes_winner_and_small_loser(self):
        # equity 1000; 5% threshold = 50.
        end_state = {
            "equity": 1000.0,
            "positions": [
                {"coin": "A", "side": "long", "size": 1.0, "entry": 100.0, "upnl": 30.0},   # profit -> flatten
                {"coin": "B", "side": "long", "size": 2.0, "entry": 50.0, "upnl": -40.0},    # small loss -> flatten
                {"coin": "C", "side": "long", "size": 3.0, "entry": 20.0, "upnl": -200.0},   # big loss -> keep
            ],
        }
        out = advance_carry(end_state, max_loss_frac=0.05)
        # kept = [C]; next_balance = 1000 - (-200) = 1200
        assert out["balance"] == pytest.approx(1200.0)
        assert set(out["positions"].keys()) == {"C"}
        assert out["positions"]["C"]["long"] == {"size": 3.0, "price": 20.0}

    def test_all_flattened_balance_equals_equity(self):
        end_state = {
            "equity": 500.0,
            "positions": [
                {"coin": "A", "side": "long", "size": 1.0, "entry": 1.0, "upnl": 10.0},
            ],
        }
        out = advance_carry(end_state, max_loss_frac=0.05)
        assert out["balance"] == pytest.approx(500.0)
        assert out["positions"] == {}

    def test_empty_positions(self):
        out = advance_carry({"equity": 100.0, "positions": []}, max_loss_frac=0.05)
        assert out["balance"] == pytest.approx(100.0)
        assert out["positions"] == {}


# ---------------------------------------------------------------------------
# Rolling state machine
# ---------------------------------------------------------------------------
class TestDecideRollingState:
    def test_adopt_when_published_differs_from_loaded(self):
        assert decide_rolling_state("p7", "p8", "2025-08-01", "2025-08-01") == "ADOPT"

    def test_adopt_bootstrap_when_nothing_loaded(self):
        assert decide_rolling_state(None, "p8", "2025-08-01", "2025-08-01") == "ADOPT"

    def test_wind_down_when_calendar_rolled_past_published(self):
        # running the published period, but today is already in a later period
        assert decide_rolling_state("p8", "p8", "2025-09-01", "2025-08-01") == "WIND_DOWN"

    def test_normal_when_aligned(self):
        assert decide_rolling_state("p8", "p8", "2025-08-01", "2025-08-01") == "NORMAL"

    def test_normal_when_nothing_published(self):
        assert decide_rolling_state(None, None, "2025-08-01", None) == "NORMAL"


# ---------------------------------------------------------------------------
# Config: live.wfo_rolling survives format_config
# ---------------------------------------------------------------------------
def test_wfo_rolling_keys_survive_format_config():
    from config_utils import get_template_config, format_config

    tmpl = get_template_config()
    assert "wfo_rolling" in tmpl["live"]

    cfg = copy.deepcopy(tmpl)
    cfg["live"]["wfo_rolling"] = {
        "enabled": True,
        "active_dir": "runs/walkforward/live",
        "max_loss_flatten_frac": 0.07,
        "check_interval_minutes": 30.0,
    }
    out = format_config(copy.deepcopy(cfg), verbose=False)
    wr = out["live"]["wfo_rolling"]
    assert bool(wr["enabled"]) is True
    assert wr["active_dir"] == "runs/walkforward/live"
    assert float(wr["max_loss_flatten_frac"]) == pytest.approx(0.07)
    assert float(wr["check_interval_minutes"]) == pytest.approx(30.0)
