#!/usr/bin/env python3
"""Single source of truth for walk-forward meta-parameters.

The walk-forward "meta-parameters" — how the rolling cross-validation is set up
(train/test/step lengths, stop criteria, the warm-start starting config, the
month-boundary loss-cut threshold, …) — are the knobs a user changes when
*experimenting* with the strategy schedule. They are consumed by three programs:

- ``src/walkforward.py``   (the backtest walk-forward orchestrator),
- ``src/wfo_scheduler.py`` (the live monthly-rolling scheduler), and
- ``src/passivbot.py``     (the live bot, via the published ``active.json``).

Historically they were embedded (and partly duplicated) inside the large WFO
config. This module centralizes the **defaults** (:data:`WF_DEFAULTS`), the
**merge rule** (:func:`merge_wf_params`) those three programs share, and a small
**stand-alone meta file** loader (:func:`load_wf_meta`) so the schedule can be
edited in one tiny JSON file (``configs/wfo_meta.json``) that *references* a base
config for the bot bounds / optimize budget / trading universe rather than
duplicating them.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Tuple

# tools/wfo_meta.py -> tools -> src -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

try:
    from config_utils import load_hjson_config  # noqa: E402
except Exception:  # pragma: no cover - package-style import fallback
    from src.config_utils import load_hjson_config  # type: ignore


# Defaults for every walk-forward meta-parameter. Inert defaults (patience 0,
# proximity 0.0, stateful_oos off) reproduce the legacy single-shot behavior so
# existing optimize/backtest runs are unchanged when the feature is unused.
WF_DEFAULTS: Dict[str, Any] = {
    "train_months": 6,
    "test_months": 1,
    "step_months": 1,
    "start_date": None,
    "end_date": None,
    "base_seed": 0,
    "calendar_months": True,
    "min_test_days": 7,
    "proximity_weight": 0.0,
    "initial_config": "configs/hype_top.json",
    "stop": {"patience": 0, "min_rel_improvement": 0.0, "max_evals": 0},
    "run_id": None,
    # Backtest fidelity: carry balance + open position across OOS windows and apply
    # the same boundary handoff rule live uses (see tools/wfo_handoff.should_flatten).
    "stateful_oos": False,
    "max_loss_flatten_frac": 0.02,
    "retrain_delay_days": 0,
    # Decoupled live scheduler / live-bot rolling parameters.
    "live_rolling": {
        "enabled": False,
        "active_dir": "runs/walkforward/live",
        "max_loss_flatten_frac": 0.02,
        "retrain_delay_days": 0,
        "check_interval_minutes": 60.0,
    },
}

# Keys whose values are dicts that should be *merged* (not replaced) over the
# defaults, so a partial override (e.g. only stop.patience) keeps the other keys.
_NESTED_KEYS = ("stop", "live_rolling")


def merge_wf_params(wf_block: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a walk-forward param block over :data:`WF_DEFAULTS`.

    Shared by ``walkforward.resolve_wf_params`` and ``wfo_scheduler.resolve_params``
    (CLI overrides are applied by each caller afterwards). ``None`` values are
    ignored (keep the default); nested ``stop`` / ``live_rolling`` dicts are merged.
    """
    wf = deepcopy(WF_DEFAULTS)
    if isinstance(wf_block, dict):
        for key, value in wf_block.items():
            if key in _NESTED_KEYS and isinstance(value, dict):
                wf[key].update(value)
            elif value is not None:
                wf[key] = value
    return wf


def _abspath(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return str(p.resolve())


def load_wf_meta(meta_path: str) -> Tuple[Dict[str, Any], str]:
    """Load a stand-alone meta file -> ``(wf_params, base_config_path)``.

    The meta file is a small JSON/HJSON document holding the walk-forward knobs
    plus a ``base_config`` pointer (where the bot bounds, optimize budget and the
    trading universe come from). ``base_config`` is resolved to an absolute path;
    every other key is merged over :data:`WF_DEFAULTS` via :func:`merge_wf_params`.
    ``initial_config`` is left as given (repo-relative) — callers resolve it with
    their own ``_abspath`` exactly as for the embedded-block path.
    """
    raw = load_hjson_config(_abspath(meta_path))
    if not isinstance(raw, dict):
        raise ValueError(f"meta file {meta_path} did not parse to an object")
    block = dict(raw)
    base_config = block.pop("base_config", None)
    if not base_config:
        raise ValueError(f"meta file {meta_path} must define 'base_config'")
    wf = merge_wf_params(block)
    return wf, _abspath(str(base_config))
