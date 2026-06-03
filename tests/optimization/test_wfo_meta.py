"""Tests for the stand-alone walk-forward meta-parameter file and its loader.

Covers WF_DEFAULTS centralization, the shared merge rule, the meta-file loader
(``load_wf_meta``), and that the shipped ``configs/wfo_meta.json`` mirrors the
embedded ``walk_forward`` block of its base config so ``--meta`` and ``--config``
produce the same windows.
"""

import json
import os

import pytest

from tools.wfo_meta import WF_DEFAULTS, merge_wf_params, load_wf_meta


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_default_threshold_is_two_percent():
    assert WF_DEFAULTS["max_loss_flatten_frac"] == pytest.approx(0.02)
    assert WF_DEFAULTS["live_rolling"]["max_loss_flatten_frac"] == pytest.approx(0.02)


def test_walkforward_and_scheduler_share_defaults():
    # Both modules must resolve to the one home in tools.wfo_meta.
    import walkforward
    import wfo_scheduler

    assert walkforward.WF_DEFAULTS is WF_DEFAULTS
    assert wfo_scheduler.WF_DEFAULTS is WF_DEFAULTS


# ---------------------------------------------------------------------------
# merge_wf_params
# ---------------------------------------------------------------------------
class TestMergeWfParams:
    def test_empty_block_returns_defaults_copy(self):
        out = merge_wf_params({})
        assert out == WF_DEFAULTS
        assert out is not WF_DEFAULTS  # deep copy, not the shared dict
        out["stop"]["patience"] = 999
        assert WF_DEFAULTS["stop"]["patience"] != 999

    def test_none_values_keep_defaults(self):
        out = merge_wf_params({"train_months": None, "start_date": None})
        assert out["train_months"] == WF_DEFAULTS["train_months"]

    def test_nested_stop_merges_partially(self):
        out = merge_wf_params({"stop": {"patience": 20}})
        assert out["stop"]["patience"] == 20
        # other stop keys keep their defaults
        assert out["stop"]["max_evals"] == WF_DEFAULTS["stop"]["max_evals"]

    def test_nested_live_rolling_merges_partially(self):
        out = merge_wf_params({"live_rolling": {"check_interval_minutes": 30.0}})
        assert out["live_rolling"]["check_interval_minutes"] == 30.0
        assert out["live_rolling"]["active_dir"] == WF_DEFAULTS["live_rolling"]["active_dir"]

    def test_full_dict_is_idempotent(self):
        # Re-merging an already-merged dict (the --meta + CLI path) is a no-op.
        once = merge_wf_params({"train_months": 4, "stop": {"patience": 7}})
        twice = merge_wf_params(once)
        assert twice == once


# ---------------------------------------------------------------------------
# load_wf_meta
# ---------------------------------------------------------------------------
class TestLoadWfMeta:
    def test_requires_base_config(self, tmp_path):
        p = tmp_path / "meta.json"
        p.write_text(json.dumps({"train_months": 6}), encoding="utf-8")
        with pytest.raises(ValueError):
            load_wf_meta(str(p))

    def test_merges_and_resolves_base_config(self, tmp_path):
        p = tmp_path / "meta.json"
        p.write_text(json.dumps({
            "base_config": "configs/wfo_hype.json",
            "train_months": 4,
            "stop": {"patience": 11},
            "max_loss_flatten_frac": 0.03,
        }), encoding="utf-8")
        wf, base_config_path = load_wf_meta(str(p))

        assert wf["train_months"] == 4
        assert wf["stop"]["patience"] == 11
        assert wf["stop"]["max_evals"] == WF_DEFAULTS["stop"]["max_evals"]  # nested merge
        assert wf["max_loss_flatten_frac"] == pytest.approx(0.03)
        assert "base_config" not in wf  # consumed, not leaked into params
        assert os.path.isabs(base_config_path)
        assert base_config_path.replace("\\", "/").endswith("configs/wfo_hype.json")
        assert os.path.exists(base_config_path)


# ---------------------------------------------------------------------------
# Shipped meta file mirrors its base config's embedded walk_forward block
# ---------------------------------------------------------------------------
def test_shipped_meta_matches_embedded_block():
    from config_utils import load_hjson_config
    from tools.wfo_meta import REPO_ROOT

    wf, base_config_path = load_wf_meta("configs/wfo_meta.json")
    assert base_config_path.replace("\\", "/").endswith("configs/wfo_hype.json")

    base = load_hjson_config(str(REPO_ROOT / "configs" / "wfo_hype.json"))
    embedded = base.get("walk_forward", {})

    # Every window-determining field in the meta must equal the embedded block,
    # so `--meta` and `--config` generate identical windows.
    for key in ("train_months", "test_months", "step_months", "base_seed",
                "calendar_months", "min_test_days", "proximity_weight", "initial_config"):
        assert wf[key] == embedded[key], f"meta/{key} diverges from embedded walk_forward"
    for key in ("patience", "min_rel_improvement", "max_evals"):
        assert wf["stop"][key] == embedded["stop"][key], f"meta/stop.{key} diverges"

    # New single-source threshold default.
    assert wf["max_loss_flatten_frac"] == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# Smart cache: the loss threshold (a live/carry-only knob) must NOT change the
# per-window optimization cache key, while training params must.
# ---------------------------------------------------------------------------
def test_threshold_does_not_invalidate_optimize_cache():
    from copy import deepcopy
    from config_utils import load_config
    from walkforward import build_train_config, window_cache_key
    from tools.wfo_utils import generate_windows

    base = load_config("configs/wfo_hype.json", verbose=False)
    w = generate_windows("2025-02-24", "2026-06-01", 6, 1, 1, True, 7)[0]
    stop = {"patience": 20, "min_rel_improvement": 0.001, "max_evals": 0}

    def key_for(b, seed=0, st=stop):
        return window_cache_key(build_train_config(b, w, seed, st, 0.0, None, 100, 4), None)

    base_key = key_for(base)

    # Vary ONLY the live wfo_rolling threshold -> cache key invariant.
    b2 = deepcopy(base)
    b2.setdefault("live", {}).setdefault("wfo_rolling", {})["max_loss_flatten_frac"] = 0.99
    assert key_for(b2) == base_key

    # Training params change the key (re-optimization).
    assert key_for(base, seed=1) != base_key
    assert key_for(base, st={"patience": 10, "min_rel_improvement": 0.001, "max_evals": 0}) != base_key
