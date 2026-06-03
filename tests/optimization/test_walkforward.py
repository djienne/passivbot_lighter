"""Tests for the walk-forward optimization module.

Covers the pure helpers (window generation, Pareto selection, equity stitching,
parameter drift), the deterministic optimization cache key, the optimizer's
early-stop convergence logic, and that the new optimize.* config keys survive
format_config.
"""

import copy
import json
import os

import numpy as np
import pytest

from tools.wfo_utils import (
    generate_windows,
    select_best_from_pareto,
    stitch_oos_equity,
    param_drift,
    normalized_distance,
)


# ---------------------------------------------------------------------------
# Window generation
# ---------------------------------------------------------------------------
class TestGenerateWindows:
    def test_basic_calendar_windows_contiguous_and_nonoverlapping(self):
        windows = generate_windows(
            "2025-01-01", "2026-01-01",
            train_months=6, test_months=1, step_months=1, calendar_months=True,
        )
        assert len(windows) >= 6
        # First window: 6mo train then 1mo test.
        w0 = windows[0]
        assert w0.train_start == "2025-01-01"
        assert w0.train_end == "2025-07-01"
        assert w0.test_start == "2025-07-01"  # contiguous: test starts where train ends
        assert w0.test_end == "2025-08-01"
        # Step == test => consecutive OOS segments are non-overlapping and contiguous.
        for prev, nxt in zip(windows, windows[1:]):
            assert nxt.test_start == prev.test_end
        # Indices are sequential from 0.
        assert [w.index for w in windows] == list(range(len(windows)))

    def test_last_partial_test_window_dropped(self):
        # Span ends mid-month so the final OOS would be too short.
        windows = generate_windows(
            "2025-01-01", "2025-08-10",
            train_months=6, test_months=1, step_months=1, min_test_days=7,
        )
        # 6mo train -> 2025-07-01..2025-08-01 (full), next train_start 2025-02-01 ->
        # train_end 2025-08-01, test 2025-08-01..2025-09-01 clamped to 2025-08-10 (9 days, kept).
        for w in windows:
            assert w.test_end <= "2025-08-10"

    def test_fixed_30day_mode_differs_from_calendar(self):
        cal = generate_windows("2025-01-01", "2026-01-01", 6, 1, 1, calendar_months=True)
        fixed = generate_windows("2025-01-01", "2026-01-01", 6, 1, 1, calendar_months=False)
        assert cal[0].train_end == "2025-07-01"
        # 6*30 = 180 days from Jan 1 -> Jun 30, not Jul 1.
        assert fixed[0].train_end == "2025-06-30"

    def test_no_windows_when_history_too_short(self):
        windows = generate_windows("2025-01-01", "2025-03-01", 6, 1, 1)
        assert windows == []

    @pytest.mark.parametrize("bad", [{"train_months": 0}, {"test_months": 0}, {"step_months": 0}])
    def test_invalid_args_raise(self, bad):
        kwargs = {"train_months": 6, "test_months": 1, "step_months": 1}
        kwargs.update(bad)
        with pytest.raises(ValueError):
            generate_windows("2025-01-01", "2026-01-01", **kwargs)


# ---------------------------------------------------------------------------
# Pareto selection
# ---------------------------------------------------------------------------
def _write_pareto_entry(directory, hash_id, w0, w1, violation=0.0):
    entry = {
        "bot": {"long": {"x": 1.0}, "short": {}},
        "optimize": {"scoring": ["adg", "sharpe"]},
        "metrics": {
            "objectives": {"w_0": w0, "w_1": w1},
            "constraint_violation": violation,
            "stats": {"adg": {"mean": -w0}},
        },
    }
    with open(os.path.join(directory, f"{hash_id}.json"), "w", encoding="utf-8") as fh:
        json.dump(entry, fh)


class TestSelectBestFromPareto:
    def test_picks_member_closest_to_ideal(self, tmp_path):
        d = tmp_path / "pareto"
        d.mkdir()
        # objectives are minimized; ideal = (-1.0, -1.0)
        _write_pareto_entry(d, "a", -1.0, -0.5)
        _write_pareto_entry(d, "b", -0.5, -1.0)
        _write_pareto_entry(d, "c", -0.9, -0.9)  # closest to ideal corner
        choice = select_best_from_pareto(str(d), ["adg", "sharpe"])
        assert choice is not None
        assert choice.hash_id == "c"
        assert choice.n_candidates == 3
        # The returned config is a full config with metrics stripped.
        assert "bot" in choice.config
        assert "metrics" not in choice.config

    def test_feasible_members_preferred_over_lower_objective_infeasible(self, tmp_path):
        d = tmp_path / "pareto"
        d.mkdir()
        _write_pareto_entry(d, "c", -0.9, -0.9, violation=0.0)
        _write_pareto_entry(d, "infeasible", -2.0, -2.0, violation=1e6)
        choice = select_best_from_pareto(str(d), ["adg", "sharpe"])
        assert choice.hash_id == "c"

    def test_deterministic_tie_break_by_hash(self, tmp_path):
        d = tmp_path / "pareto"
        d.mkdir()
        _write_pareto_entry(d, "bbb", -1.0, -1.0)
        _write_pareto_entry(d, "aaa", -1.0, -1.0)
        choice = select_best_from_pareto(str(d), ["adg", "sharpe"])
        assert choice.hash_id == "aaa"

    def test_empty_dir_returns_none(self, tmp_path):
        d = tmp_path / "pareto"
        d.mkdir()
        assert select_best_from_pareto(str(d), ["adg", "sharpe"]) is None


# ---------------------------------------------------------------------------
# Equity stitching
# ---------------------------------------------------------------------------
class TestStitchOosEquity:
    def test_compounds_segments(self):
        # segment 1: +10% (100 -> 110); segment 2: -50% then back (200 -> 100, ratio 0.5)
        seg1 = [100.0, 105.0, 110.0]
        seg2 = [200.0, 150.0, 100.0]
        out = stitch_oos_equity([seg1, seg2], starting_balance=1000.0)
        eq = out["equity"]
        assert eq[0] == pytest.approx(1000.0)
        # after seg1: 1000 * 1.1 = 1100
        assert eq[2] == pytest.approx(1100.0)
        # after seg2: 1100 * 0.5 = 550
        assert eq[-1] == pytest.approx(550.0)
        assert out["metrics"]["final_equity"] == pytest.approx(550.0)
        assert out["metrics"]["total_return"] == pytest.approx(-0.45)
        assert out["metrics"]["max_drawdown"] == pytest.approx(0.5)

    def test_empty_segments(self):
        out = stitch_oos_equity([], starting_balance=100.0)
        assert out["equity"] == []
        assert out["metrics"]["final_equity"] == pytest.approx(100.0)

    def test_dict_segment_without_timestamps_uses_legacy_stitching(self):
        out = stitch_oos_equity([{"equity": [100.0, 110.0]}], starting_balance=1000.0)
        assert out["equity"] == pytest.approx([1000.0, 1100.0])
        assert "timestamps" not in out

    def test_timestamped_segments_dedupe_overlap(self):
        seg1 = {"timestamps": [1, 2, 3], "equity": [100.0, 110.0, 121.0]}
        # Starts at the same timestamp as seg1's last point. The duplicate timestamp
        # is used as the anchor, then skipped, so the overlap is not double-counted.
        seg2 = {"timestamps": [3, 4], "equity": [200.0, 220.0]}
        out = stitch_oos_equity([seg1, seg2], starting_balance=1000.0)
        assert out["timestamps"] == [1, 2, 3, 4]
        assert out["equity"] == pytest.approx([1000.0, 1100.0, 1210.0, 1331.0])

    def test_timestamped_segments_sorted_before_stitching(self):
        seg = {"timestamps": [3, 1, 2], "equity": [121.0, 100.0, 110.0]}
        out = stitch_oos_equity([seg], starting_balance=1000.0)
        assert out["timestamps"] == [1, 2, 3]
        assert out["equity"] == pytest.approx([1000.0, 1100.0, 1210.0])


# ---------------------------------------------------------------------------
# Parameter drift
# ---------------------------------------------------------------------------
class TestParamDrift:
    def test_drift_and_normalization(self):
        prev = {"bot": {"long": {"a": 1.0, "b": 2.0}}}
        cur = {"bot": {"long": {"a": 1.5, "b": 2.0}}}
        ranges = {"long.a": 1.0, "long.b": 4.0}
        out = param_drift(prev, cur, ranges)
        assert out["per_param"]["long.a"]["delta"] == pytest.approx(0.5)
        assert out["per_param"]["long.a"]["normalized"] == pytest.approx(0.5)
        assert out["per_param"]["long.b"]["delta"] == pytest.approx(0.0)
        assert out["l2_normalized"] == pytest.approx(0.5)

    def test_normalized_distance(self):
        assert normalized_distance([1.0, 1.0], [0.0, 0.0], [1.0, 1.0]) == pytest.approx(1.0)
        assert normalized_distance([1.0], [1.0], [2.0]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Deterministic optimization cache key
# ---------------------------------------------------------------------------
def _train_cfg():
    return {
        "bot": {"long": {"x": 1.0}, "short": {}},
        "optimize": {
            "seed": 7,
            "scoring": ["adg"],
            "bounds": {"long_x": [0.0, 2.0]},
            "stop": {"patience": 10, "min_rel_improvement": 0.001, "max_evals": 0},
            "proximity": {"weight": 0.0, "reference_config": ""},
            "n_cpus": 5,
            "iters": 1000,
            "population_size": 100,
        },
        "backtest": {
            "start_date": "2025-01-01",
            "end_date": "2025-07-01",
            "exchanges": ["lighter"],
            "starting_balance": 100.0,
            "base_dir": "some/dir",
            "cache_dir": {"lighter": "x"},
            "coins": {"lighter": ["HYPE"]},
        },
        "results_dir": "volatile/path",
    }


class TestWindowCacheKey:
    def test_identical_meta_same_key(self):
        from walkforward import window_cache_key

        assert window_cache_key(_train_cfg(), None) == window_cache_key(_train_cfg(), None)

    def test_volatile_fields_excluded(self):
        from walkforward import window_cache_key

        base = _train_cfg()
        variant = _train_cfg()
        variant["results_dir"] = "other/path"
        variant["backtest"]["base_dir"] = "other"
        variant["backtest"]["cache_dir"] = {"lighter": "y"}
        variant["optimize"]["n_cpus"] = 16
        assert window_cache_key(base, None) == window_cache_key(variant, None)

    def test_seed_changes_key(self):
        from walkforward import window_cache_key

        base = _train_cfg()
        variant = _train_cfg()
        variant["optimize"]["seed"] = 999
        assert window_cache_key(base, None) != window_cache_key(variant, None)

    def test_dates_change_key(self):
        from walkforward import window_cache_key

        base = _train_cfg()
        variant = _train_cfg()
        variant["backtest"]["start_date"] = "2025-02-01"
        assert window_cache_key(base, None) != window_cache_key(variant, None)

    def test_coin_universe_changes_key(self):
        from walkforward import window_cache_key

        base = _train_cfg()
        variant = _train_cfg()
        variant["backtest"]["coins"] = {"lighter": ["OTHER"]}
        assert window_cache_key(base, None) != window_cache_key(variant, None)

    def test_walk_forward_run_id_does_not_change_key(self):
        from walkforward import window_cache_key

        base = _train_cfg()
        variant = _train_cfg()
        base["walk_forward"] = {"run_id": "run-a", "train_months": 6}
        variant["walk_forward"] = {"run_id": "run-b", "train_months": 6}
        assert window_cache_key(base, None) == window_cache_key(variant, None)

    def test_invariant_to_config_metadata_and_base_path(self):
        from walkforward import window_cache_key

        base = _train_cfg()
        base.setdefault("live", {})["base_config_path"] = "/runs/A/train_config.json"
        variant = _train_cfg()
        # Per-run metadata that must NOT affect the cache key.
        variant["_transform_log"] = [{"ts_ms": 123456789, "step": "load_config"}]
        variant["_raw"] = {"whatever": 1}
        variant.setdefault("live", {})["base_config_path"] = "/runs/B/train_config.json"
        assert window_cache_key(base, None) == window_cache_key(variant, None)

    def test_warm_start_hash_ignores_metadata_and_base_path(self, tmp_path):
        from walkforward import window_cache_key

        ws_a = tmp_path / "a.json"
        ws_b = tmp_path / "b.json"
        # Same bot params, different run-specific metadata/path => same effective warm-start.
        ws_a.write_text(json.dumps({
            "bot": {"long": {"x": 1.0}},
            "live": {"base_config_path": "/runs/A/x.json"},
        }))
        ws_b.write_text(json.dumps({
            "bot": {"long": {"x": 1.0}},
            "live": {"base_config_path": "/runs/B/x.json"},
            "_transform_log": [{"ts_ms": 999}],
        }))
        assert window_cache_key(_train_cfg(), str(ws_a)) == window_cache_key(_train_cfg(), str(ws_b))

    def test_warm_start_content_changes_key(self, tmp_path):
        from walkforward import window_cache_key

        ws1 = tmp_path / "ws1.json"
        ws2 = tmp_path / "ws2.json"
        ws1.write_text(json.dumps({"bot": {"long": {"x": 1.0}}}))
        ws2.write_text(json.dumps({"bot": {"long": {"x": 9.0}}}))
        k_none = window_cache_key(_train_cfg(), None)
        k1 = window_cache_key(_train_cfg(), str(ws1))
        k2 = window_cache_key(_train_cfg(), str(ws2))
        assert k1 != k_none
        assert k1 != k2
        # Same content => same key.
        assert k1 == window_cache_key(_train_cfg(), str(ws1))

    def test_cached_choice_rejects_mismatched_key_window_or_seed(self, tmp_path):
        from walkforward import _load_cached_choice

        payload = {
            "cache_key": "key-a",
            "window": {"index": 0},
            "seed": 7,
            "hash_id": "abc",
            "objectives": [1.0],
            "violation": 0.0,
            "distance": 0.0,
            "n_candidates": 1,
            "metrics": {},
            "config": {"bot": {"long": {}}},
        }
        path = tmp_path / "choice.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert _load_cached_choice(path, expected_key="key-a", expected_window={"index": 0}, expected_seed=7)
        assert _load_cached_choice(path, expected_key="key-b", expected_window={"index": 0}, expected_seed=7) is None
        assert _load_cached_choice(path, expected_key="key-a", expected_window={"index": 1}, expected_seed=7) is None
        assert _load_cached_choice(path, expected_key="key-a", expected_window={"index": 0}, expected_seed=8) is None


# ---------------------------------------------------------------------------
# Per-window train config: proximity gating
# ---------------------------------------------------------------------------
class TestBuildTrainConfigProximity:
    def _base(self):
        return {"backtest": {}, "optimize": {}}

    def _window(self):
        from tools.wfo_utils import Window

        return Window(0, "2025-01-01", "2025-07-01", "2025-07-01", "2025-08-01")

    def test_window0_has_no_proximity_penalty(self):
        from walkforward import build_train_config

        cfg = build_train_config(
            self._base(), self._window(), seed=0,
            stop_cfg={"patience": 0, "min_rel_improvement": 0.0, "max_evals": 0},
            proximity_weight=0.05, reference_config=None, iters=None, n_cpus=None,
        )
        # No previous window => no reference => penalty disabled even with weight > 0.
        assert cfg["optimize"]["proximity"]["weight"] == 0.0
        assert cfg["optimize"]["proximity"]["reference_config"] == ""

    def test_later_window_has_proximity_penalty(self, tmp_path):
        from walkforward import build_train_config

        prev = tmp_path / "prev.json"
        prev.write_text(json.dumps({"bot": {"long": {"x": 1.0}}}), encoding="utf-8")
        cfg = build_train_config(
            self._base(), self._window(), seed=1,
            stop_cfg={"patience": 0, "min_rel_improvement": 0.0, "max_evals": 0},
            proximity_weight=0.05, reference_config=str(prev), iters=None, n_cpus=None,
        )
        assert cfg["optimize"]["proximity"]["weight"] == pytest.approx(0.05)
        assert cfg["optimize"]["proximity"]["reference_config"]  # resolved abs path

    def test_zero_weight_never_sets_proximity(self, tmp_path):
        from walkforward import build_train_config

        prev = tmp_path / "prev.json"
        prev.write_text(json.dumps({"bot": {"long": {"x": 1.0}}}), encoding="utf-8")
        cfg = build_train_config(
            self._base(), self._window(), seed=1,
            stop_cfg={"patience": 0, "min_rel_improvement": 0.0, "max_evals": 0},
            proximity_weight=0.0, reference_config=str(prev), iters=None, n_cpus=None,
        )
        assert cfg["optimize"]["proximity"]["weight"] == 0.0
        assert cfg["optimize"]["proximity"]["reference_config"] == ""


class TestOptimizeOneWindow:
    def test_raises_window_error_on_optimizer_failure(self, tmp_path):
        from walkforward import optimize_one_window, WindowOptimizeError
        from tools.wfo_utils import Window

        base = {"backtest": {}, "optimize": {}, "bot": {"long": {}, "short": {}}}
        w = Window(0, "2025-01-01", "2025-07-01", "2025-07-01", "2025-08-01")
        with pytest.raises(WindowOptimizeError) as ei:
            optimize_one_window(
                base, w,
                seed=0,
                stop_cfg={"patience": 0, "min_rel_improvement": 0.0, "max_evals": 0},
                proximity_weight=0.0,
                proximity_reference=None,
                warm_start=None,
                scoring_keys=["adg"],
                train_cfg_path=str(tmp_path / "train.json"),
                results_dir=str(tmp_path / "res"),
                log_path=str(tmp_path / "opt.log"),
                cache_dir=None,
                no_cache=True,
                optimize_script=str(tmp_path / "does_not_exist.py"),
            )
        assert ei.value.code == 2
        # The train config is written before the optimizer runs (so it's reusable).
        assert (tmp_path / "train.json").exists()


# ---------------------------------------------------------------------------
# Optimizer early-stop convergence
# ---------------------------------------------------------------------------
import optimize  # noqa: E402


def _valid_individual():
    from unittest.mock import MagicMock

    ind = MagicMock()
    ind.fitness.valid = True
    return ind


class _FakeFitness:
    def __init__(self):
        self._values = ()
        self.constraint_violation = 0.0

    @property
    def valid(self):
        return bool(self._values)

    @property
    def values(self):
        return self._values

    @values.setter
    def values(self, values):
        self._values = tuple(values)


class _FakeIndividual(list):
    def __init__(self):
        super().__init__([0.0])
        self.fitness = _FakeFitness()


class _ImmediateResult:
    def ready(self):
        return True

    def get(self):
        return (1.0,), 0.0, None


class _CountingPool:
    def __init__(self):
        self.calls = 0

    def apply_async(self, fn, args):
        self.calls += 1
        return _ImmediateResult()


@pytest.mark.skipif(optimize.algorithms is None, reason="deap not installed")
class TestEarlyStop:
    def _run(self, monkeypatch, ngen, stop_cfg):
        from unittest.mock import MagicMock

        pop = [_valid_individual(), _valid_individual()]
        varor = MagicMock(return_value=[])
        monkeypatch.setattr(optimize.algorithms, "varOr", varor)
        toolbox = MagicMock()
        toolbox.select.side_effect = lambda combined, mu: pop
        stats = MagicMock()
        stats.compile.return_value = {"min": np.array([10.0]), "max": np.array([10.0])}
        optimize.ea_mu_plus_lambda_stream(
            population=pop, toolbox=toolbox, mu=2, lambda_=2, cxpb=0.5, mutpb=0.5,
            ngen=ngen, stats=stats, halloffame=MagicMock(), verbose=False,
            recorder=MagicMock(), evaluator_config={}, overrides_list=[],
            pool=MagicMock(), duplicate_counter={"total": 0, "resolved": 0, "reused": 0},
            pool_state={"terminated": False}, stop_cfg=stop_cfg,
        )
        return varor

    def test_breaks_after_patience(self, monkeypatch):
        # Constant signal => no improvement => break once patience stale gens reached.
        varor = self._run(monkeypatch, ngen=50, stop_cfg={"patience": 3, "min_rel_improvement": 0.0})
        # best set on gen1; stale increments gens 2,3,4 -> break at gen4.
        assert varor.call_count == 4

    def test_patience_zero_runs_full(self, monkeypatch):
        varor = self._run(monkeypatch, ngen=3, stop_cfg={"patience": 0})
        assert varor.call_count == 3

    def test_max_evals_caps_initial_population(self, monkeypatch):
        from unittest.mock import MagicMock

        pop = [_FakeIndividual() for _ in range(5)]
        pool = _CountingPool()
        stats = MagicMock()
        stats.compile.return_value = {"min": np.array([1.0]), "max": np.array([1.0])}
        varor = MagicMock(return_value=[])
        monkeypatch.setattr(optimize.algorithms, "varOr", varor)
        optimize.ea_mu_plus_lambda_stream(
            population=pop, toolbox=MagicMock(), mu=5, lambda_=5, cxpb=0.5, mutpb=0.5,
            ngen=10, stats=stats, halloffame=MagicMock(), verbose=False,
            recorder=MagicMock(), evaluator_config={}, overrides_list=[],
            pool=pool, duplicate_counter={"total": 0, "resolved": 0, "reused": 0},
            pool_state={"terminated": False}, stop_cfg={"max_evals": 1},
        )
        assert pool.calls == 1
        assert varor.call_count == 0

    def test_max_evals_allows_only_initial_population_at_population_size(self, monkeypatch):
        from unittest.mock import MagicMock

        pop = [_FakeIndividual() for _ in range(3)]
        pool = _CountingPool()
        stats = MagicMock()
        stats.compile.return_value = {"min": np.array([1.0]), "max": np.array([1.0])}
        varor = MagicMock(return_value=[])
        monkeypatch.setattr(optimize.algorithms, "varOr", varor)
        optimize.ea_mu_plus_lambda_stream(
            population=pop, toolbox=MagicMock(), mu=3, lambda_=3, cxpb=0.5, mutpb=0.5,
            ngen=10, stats=stats, halloffame=MagicMock(), verbose=False,
            recorder=MagicMock(), evaluator_config={}, overrides_list=[],
            pool=pool, duplicate_counter={"total": 0, "resolved": 0, "reused": 0},
            pool_state={"terminated": False}, stop_cfg={"max_evals": 3},
        )
        assert pool.calls == 3
        assert varor.call_count == 0


def test_proximity_penalty_does_not_increase_constraint_violation(monkeypatch):
    class Individual(list):
        pass

    evaluator = optimize.Evaluator.__new__(optimize.Evaluator)
    evaluator.bounds = []
    evaluator.sig_digits = 5
    evaluator.config = {"optimize": {"scoring": ["adg"]}}
    evaluator.seen_hashes = {}
    evaluator.duplicate_counter = {"total": 0, "resolved": 0, "reused": 0}
    evaluator.exchanges = ["lighter"]
    evaluator.shared_hlcvs_np = {"lighter": np.array([])}
    evaluator.shared_btc_np = {"lighter": np.array([])}
    evaluator.msss = {"lighter": {}}
    evaluator.timestamps = {"lighter": np.array([])}
    evaluator._ensure_attached = lambda exchange: None
    evaluator.calc_fitness = lambda stats: ((1.0,), 0.0)
    evaluator._proximity_penalty = lambda individual: 0.25
    evaluator._attachments = {}

    monkeypatch.setattr(optimize, "enforce_bounds", lambda individual, bounds, sig_digits: individual)
    monkeypatch.setattr(optimize, "individual_to_config", lambda individual, overrides, overrides_list, config: config)
    monkeypatch.setattr(optimize, "build_backtest_payload", lambda *args, **kwargs: {})
    monkeypatch.setattr(optimize, "execute_backtest", lambda payload, config: ([], [], {}))
    monkeypatch.setattr(optimize, "build_scenario_metrics", lambda analyses: {"stats": {}})

    objectives, penalty, metrics = evaluator.evaluate(Individual([0.0]), [])
    assert objectives == pytest.approx((1.25,))
    assert penalty == pytest.approx(0.0)
    assert metrics["constraint_violation"] == pytest.approx(0.0)
    assert metrics["proximity_penalty"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# New optimize.* config keys survive format_config
# ---------------------------------------------------------------------------
def test_new_optimize_keys_survive_format_config():
    from config_utils import get_template_config, format_config

    tmpl = get_template_config()
    assert "seed" in tmpl["optimize"]
    assert "stop" in tmpl["optimize"]
    assert "proximity" in tmpl["optimize"]

    cfg = copy.deepcopy(tmpl)
    cfg["optimize"]["seed"] = 4242
    cfg["optimize"]["stop"] = {"patience": 9, "min_rel_improvement": 0.002, "max_evals": 5000}
    cfg["optimize"]["proximity"] = {"weight": 0.05, "reference_config": "configs/hype_top.json"}
    out = format_config(copy.deepcopy(cfg), verbose=False)
    assert out["optimize"]["seed"] == 4242
    assert int(out["optimize"]["stop"]["patience"]) == 9
    assert float(out["optimize"]["stop"]["min_rel_improvement"]) == pytest.approx(0.002)
    assert float(out["optimize"]["proximity"]["weight"]) == pytest.approx(0.05)
    assert out["optimize"]["proximity"]["reference_config"] == "configs/hype_top.json"
