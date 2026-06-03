"""Tests for the decoupled walk-forward live scheduler (src/wfo_scheduler.py).

The optimizer itself is stubbed so these tests validate the scheduler's orchestration:
current-window resolution, warm-start chain replay, atomic publishing, and state
persistence — with no exchange, no subprocess, and no real optimization.
"""

import json
import types

import pytest

import wfo_scheduler
from tools.wfo_utils import ParetoChoice


def _args(**over):
    base = dict(
        config="x", once=True, dry_run=False, initial_config=None,
        iters=None, n_cpus=None, check_interval_minutes=None,
        cache_dir=None, no_cache=True, log_level="info",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _base_config():
    return {
        "backtest": {"start_date": "2025-01-01", "starting_balance": 100.0},
        "optimize": {"scoring": ["adg"]},
        "bot": {"long": {}, "short": {}},
    }


def _wf(active_dir, initial_config=""):
    wf = wfo_scheduler.resolve_params({}, _args())
    wf["train_months"] = 6
    wf["test_months"] = 1
    wf["step_months"] = 1
    wf["start_date"] = None  # fall back to base backtest.start_date as the anchor
    wf["initial_config"] = initial_config
    wf["live_rolling"]["active_dir"] = str(active_dir)
    return wf


def test_resolve_params_merges_block_and_cli():
    args = _args(initial_config="foo.json", check_interval_minutes=15.0)
    wf = wfo_scheduler.resolve_params(
        {"train_months": 3, "live_rolling": {"active_dir": "x"}}, args
    )
    assert wf["train_months"] == 3
    assert wf["initial_config"] == "foo.json"
    assert wf["live_rolling"]["active_dir"] == "x"
    assert wf["live_rolling"]["check_interval_minutes"] == 15.0


def test_state_roundtrip(tmp_path):
    active = tmp_path / "live"
    wfo_scheduler._save_state(active, {"a": 1, "b": "x"})
    assert wfo_scheduler._load_state(active) == {"a": 1, "b": "x"}
    assert wfo_scheduler._load_state(tmp_path / "nope") == {}


def test_no_window_before_first_test_period(tmp_path):
    active = tmp_path / "live"
    wf = _wf(active)
    rc = wfo_scheduler.run_once(_base_config(), wf, _args(), today="2025-06-30")
    assert rc == 0
    assert not (active / "active.json").exists()


def test_dry_run_does_not_optimize(tmp_path, monkeypatch):
    active = tmp_path / "live"
    wf = _wf(active)
    called = []
    monkeypatch.setattr(wfo_scheduler, "optimize_one_window",
                        lambda *a, **k: called.append(1))
    rc = wfo_scheduler.run_once(_base_config(), wf, _args(dry_run=True), today="2025-08-15")
    assert rc == 0
    assert called == []
    assert not (active / "active.json").exists()


def test_publishes_current_window_via_chain(tmp_path, monkeypatch):
    active = tmp_path / "live"
    init = tmp_path / "init.json"
    init.write_text(json.dumps({"bot": {"long": {}, "short": {}}}), encoding="utf-8")
    wf = _wf(active, initial_config=str(init))

    calls = []

    def stub(base_config, window, **kw):
        calls.append(window.index)
        cfg = {"bot": {"long": {"x": float(window.index)}, "short": {}},
               "backtest": {}, "optimize": {}}
        return (
            ParetoChoice(f"h{window.index}", cfg, (1.0,), 0.0, 0.0, {}, 1),
            f"key{window.index}",
            False,
        )

    monkeypatch.setattr(wfo_scheduler, "optimize_one_window", stub)

    rc = wfo_scheduler.run_once(_base_config(), wf, _args(), today="2025-08-15")
    assert rc == 0
    # today in window 1 (live 2025-08-01..2025-09-01); chain replays windows 0 and 1.
    assert calls == [0, 1]

    pointer = json.loads((active / "active.json").read_text(encoding="utf-8"))
    assert pointer["window_index"] == 1
    assert pointer["period"] == "2025-08-01..2025-09-01"
    assert pointer["chosen_hash"] == "h1"
    assert pointer["anchor_start"] == "2025-01-01"

    cfg = json.loads((active / "active_config.json").read_text(encoding="utf-8"))
    assert cfg["bot"]["long"]["x"] == 1.0  # last window's config is the active one

    assert (active / "configs_history" / "window_01_2025-08-01.json").exists()

    state = json.loads((active / "scheduler_state.json").read_text(encoding="utf-8"))
    assert state["last_window_index"] == 1
    assert state["last_published_period"] == "2025-08-01..2025-09-01"
