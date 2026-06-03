#!/usr/bin/env python3
"""Walk-forward optimization (WFO) orchestrator.

Rolls a train/test window across history: optimize on the training window, pick the
single best config, evaluate it out-of-sample (OOS) on the following test window,
then roll forward and warm-start the next window from the previous window's chosen
config. Each window's chosen config is a complete passivbot config, saved
chronologically so it can be backtested or deployed live.

The optimizer and backtester run as isolated subprocesses (clean DEAP / pool /
shared-memory state, reproducible via per-window seeds + PYTHONHASHSEED=0).

Usage (conda env per CLAUDE.md):
    python src/walkforward.py --config configs/wfo_hype.json
    python src/walkforward.py --config configs/wfo_hype.json --dry-run
    python src/walkforward.py --config configs/wfo_hype.json --train-months 6 \
        --test-months 1 --base-seed 0 --proximity-weight 0.05 --patience 20 \
        --min-rel-improvement 0.001

See configs/wfo_hype.json for the `walk_forward` config block and its defaults.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make src/ importable whether launched as `python src/walkforward.py` or `-m`.
SRC_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config_utils import load_hjson_config, load_config, dump_config  # noqa: E402
from utils import format_end_date, ts_to_date, utc_ms  # noqa: E402
from pure_funcs import calc_hash  # noqa: E402
from logging_setup import configure_logging  # noqa: E402
from optimization.config_adapter import extract_bounds_tuple_list_from_config  # noqa: E402
from tools.wfo_utils import (  # noqa: E402
    ParetoChoice,
    generate_windows,
    select_best_from_pareto,  # noqa: F401 (kept for back-compat / external callers)
    rank_pareto_candidates,
    pareto_trade_rate,
    select_with_trade_guard,
    param_drift,
    stitch_oos_equity,
)
from tools.wfo_handoff import advance_carry  # noqa: E402
from tools.wfo_meta import WF_DEFAULTS, merge_wf_params, load_wf_meta  # noqa: E402,F401

logger = logging.getLogger("walkforward")

# Shared, repo-level lighter 1m data cache. Per-window child configs would
# otherwise anchor the lighter data dir to their own (window) directory via
# live.base_config_path, causing a re-download per window. Pinning an absolute
# shared path makes every window, OOS backtest, and future rerun reuse one cache.
SHARED_LIGHTER_DATA_DIR = str((REPO_ROOT / "caches" / "ohlcv" / "lighter" / "1m").resolve())

# WF_DEFAULTS now lives in tools/wfo_meta.py (single home, shared with the scheduler
# and the stand-alone meta-file loader); it is re-imported above for back-compat.


# ---------------------------------------------------------------------------
# Config / parameter resolution
# ---------------------------------------------------------------------------
def resolve_wf_params(wf_block: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """Merge precedence: CLI > walk_forward block (or meta file) > defaults.

    ``wf_block`` may be a raw ``walk_forward`` config block or an already-merged
    meta dict (re-merging a full dict is idempotent), so this also applies CLI
    overrides on top of a ``--meta``-loaded param set.
    """
    wf = merge_wf_params(wf_block)

    cli_map = {
        "train_months": args.train_months,
        "test_months": args.test_months,
        "step_months": args.step_months,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "base_seed": args.base_seed,
        "proximity_weight": args.proximity_weight,
        "initial_config": args.initial_config,
        "min_trade_ratio": getattr(args, "min_trade_ratio", None),
        "run_id": args.run_id,
    }
    for key, value in cli_map.items():
        if value is not None:
            wf[key] = value
    if args.calendar_months is not None:
        wf["calendar_months"] = args.calendar_months
    if args.patience is not None:
        wf["stop"]["patience"] = args.patience
    if args.min_rel_improvement is not None:
        wf["stop"]["min_rel_improvement"] = args.min_rel_improvement
    if args.max_evals is not None:
        wf["stop"]["max_evals"] = args.max_evals
    return wf


def _abspath(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return str(p.resolve())


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC_ROOT) + (os.pathsep + existing if existing else "")
    env["PYTHONHASHSEED"] = "0"
    env["SKIP_RUST_COMPILE"] = "1"
    return env


def _run_subprocess(cmd: List[str], log_path: Path) -> int:
    logger.info("running: %s", " ".join(cmd))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write("# " + " ".join(cmd) + "\n")
        log_file.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=_child_env(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    return proc.returncode


# ---------------------------------------------------------------------------
# Per-window steps
# ---------------------------------------------------------------------------
def build_train_config(
    base_config: Dict[str, Any],
    window,
    seed: int,
    stop_cfg: Dict[str, Any],
    proximity_weight: float,
    reference_config: Optional[str],
    iters: Optional[int],
    n_cpus: Optional[int],
) -> Dict[str, Any]:
    cfg = deepcopy(base_config)
    cfg.setdefault("backtest", {})
    cfg["backtest"]["start_date"] = window.train_start
    cfg["backtest"]["end_date"] = window.train_end
    cfg["backtest"]["lighter_data_dir"] = SHARED_LIGHTER_DATA_DIR
    cfg.setdefault("optimize", {})
    cfg["optimize"]["seed"] = int(seed)
    cfg["optimize"]["stop"] = {
        "patience": int(stop_cfg.get("patience", 0) or 0),
        "min_rel_improvement": float(stop_cfg.get("min_rel_improvement", 0.0) or 0.0),
        "max_evals": int(stop_cfg.get("max_evals", 0) or 0),
    }
    if proximity_weight and proximity_weight > 0 and reference_config:
        cfg["optimize"]["proximity"] = {
            "weight": float(proximity_weight),
            "reference_config": _abspath(reference_config),
        }
    else:
        cfg["optimize"]["proximity"] = {"weight": 0.0, "reference_config": ""}
    if iters is not None:
        cfg["optimize"]["iters"] = int(iters)
    if n_cpus is not None:
        cfg["optimize"]["n_cpus"] = int(n_cpus)
    return cfg


def build_test_config(
    base_config: Dict[str, Any],
    chosen_config: Dict[str, Any],
    window,
    test_base_dir: str,
    starting_balance: Optional[float] = None,
    initial_positions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = deepcopy(base_config)
    cfg.setdefault("backtest", {})
    cfg["backtest"]["start_date"] = window.test_start
    cfg["backtest"]["end_date"] = window.test_end
    cfg["backtest"]["base_dir"] = test_base_dir
    cfg["backtest"]["lighter_data_dir"] = SHARED_LIGHTER_DATA_DIR
    # Inject the optimized strategy parameters; keep everything else from the base.
    cfg["bot"] = deepcopy(chosen_config.get("bot", cfg.get("bot", {})))
    cfg["disable_plotting"] = True
    # Stateful carry-over (walk_forward.stateful_oos): seed this OOS segment with the
    # previous segment's carried balance + open positions and ask the engine to emit
    # end_state.json for the next segment. None => flat start (historical behavior).
    if starting_balance is not None:
        cfg["backtest"]["starting_balance"] = float(starting_balance)
    if initial_positions is not None:
        cfg["backtest"]["initial_positions"] = initial_positions
        cfg["backtest"]["wfo_write_end_state"] = True
    return cfg


def _strip_config_metadata(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Drop non-deterministic config metadata (e.g. _transform_log with per-run
    timestamps, _raw) so hashes depend only on meaningful content."""
    return {k: v for k, v in cfg.items() if not (isinstance(k, str) and k.startswith("_"))}


def _hash_config_file(path: Optional[str]) -> Optional[str]:
    """Hash a warm-start config by its strategy parameters only.

    Only the bot section affects optimization (it seeds the initial population), so
    hashing just that makes the key invariant to run-specific metadata/paths (e.g.
    live.base_config_path) and therefore reusable across runs/backtests/live.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("bot"), dict):
            return calc_hash(data["bot"])
        if isinstance(data, dict):
            return calc_hash(_strip_config_metadata(data))
        return calc_hash(data)
    except Exception:
        return None


def window_cache_key(train_cfg: Dict[str, Any], warm_start_path: Optional[str]) -> str:
    """Deterministic content-addressed key for a window's optimization.

    The optimization result is fully determined by the meta-parameters, so the key
    hashes the cleaned training config (bot bounds, scoring, limits, seed, stop
    criteria, proximity weight, training dates, exchanges, starting balance, iters,
    population) plus the *content* of the warm-start config. Volatile, result-
    irrelevant fields (output dirs, cpu count, plotting, paths) are excluded so the
    same window reuses a cached result across runs, backtests, and live reruns.
    """
    cfg = _strip_config_metadata(deepcopy(train_cfg))  # drop _transform_log/_raw (per-run timestamps)
    cfg.pop("walk_forward", None)
    cfg.pop("results_dir", None)
    cfg.pop("results_filename", None)
    cfg.pop("disable_plotting", None)
    cfg.pop("analysis", None)
    cfg.pop("logging", None)
    bt = cfg.get("backtest", {}) or {}
    for key in ("base_dir", "cache_dir", "lighter_data_dir"):
        bt.pop(key, None)  # machine-specific data location, not a meta-parameter
    live = cfg.get("live", {}) or {}
    live.pop("base_config_path", None)
    # Live-only rolling/operational knobs (incl. the month-boundary loss threshold)
    # affect OOS carry and live behavior, never the training result, so they must not
    # invalidate the optimization cache ("Smart" reuse: editing the meta-file threshold
    # re-runs only the cheap OOS evaluation, not the optimization).
    live.pop("wfo_rolling", None)
    opt = cfg.get("optimize", {}) or {}
    opt.pop("n_cpus", None)  # cpu count does not affect the (index-assigned) result
    prox = opt.get("proximity", {}) or {}
    # The reference path is volatile; the warm-start content hash captures its effect.
    if "reference_config" in prox:
        prox = dict(prox)
        prox.pop("reference_config", None)
        opt["proximity"] = prox
    key_payload = {
        "train_config": cfg,
        "warm_start": _hash_config_file(warm_start_path),
    }
    return calc_hash(key_payload)


def _choice_to_dict(choice: ParetoChoice) -> Dict[str, Any]:
    return {
        "hash_id": choice.hash_id,
        "objectives": list(choice.objectives),
        "violation": choice.violation,
        "distance": choice.distance,
        "n_candidates": choice.n_candidates,
        "metrics": choice.metrics,
        "config": choice.config,
    }


def _choice_from_dict(d: Dict[str, Any]) -> ParetoChoice:
    return ParetoChoice(
        hash_id=d["hash_id"],
        config=d["config"],
        objectives=tuple(d.get("objectives", [])),
        violation=float(d.get("violation", 0.0)),
        distance=float(d.get("distance", 0.0)),
        metrics=d.get("metrics", {}) or {},
        n_candidates=int(d.get("n_candidates", 0)),
    )


def seed_choice_from_config(config_path: str) -> ParetoChoice:
    """Non-optimized choice for the first walk-forward window.

    The first window deploys the hand-tuned initial config (e.g. configs/hype_top.json)
    as-is -- no optimization; optimization begins at window 1, warm-started from it. The
    choice carries empty metrics, so its in-sample trade rate is unknown (``None``) and the
    trade-count overfit guard naturally activates from window 2 onward. The live scheduler
    applies the identical rule so live and backtest stay in parity.

    ``hash_id`` hashes the strategy parameters only (the ``bot`` section), matching how
    warm-start configs are keyed (see :func:`_hash_config_file`).

    The initial config is a finished, deployable config, so it is loaded faithfully with
    ``load_hjson_config`` (no ``format_config`` flavor transform) and used as-is.
    """
    cfg = load_hjson_config(config_path)
    bot = cfg.get("bot", {}) or {}
    hash_id = calc_hash(bot) if bot else calc_hash(_strip_config_metadata(cfg))
    return ParetoChoice(
        hash_id=hash_id,
        config=cfg,
        objectives=(),
        violation=0.0,
        distance=0.0,
        metrics={},
        n_candidates=0,
    )


def _cache_header_ok(d: Dict[str, Any], expected_key, expected_window, expected_seed) -> bool:
    if expected_key is not None and d.get("cache_key") != expected_key:
        return False
    if expected_window is not None and d.get("window") != expected_window:
        return False
    if expected_seed is not None and int(d.get("seed", -1)) != int(expected_seed):
        return False
    return True


def _save_cached_choice(path: Path, choice: ParetoChoice, key: str, window, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cache_key": key, "window": window.to_dict(), "seed": seed, **_choice_to_dict(choice)}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def _load_cached_choice(
    path: Path,
    expected_key: Optional[str] = None,
    expected_window: Optional[Dict[str, Any]] = None,
    expected_seed: Optional[int] = None,
) -> Optional[ParetoChoice]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if not _cache_header_ok(d, expected_key, expected_window, expected_seed):
            return None
        return _choice_from_dict(d)
    except Exception:
        return None


def _save_cached_candidates(
    path: Path, candidates: List[ParetoChoice], key: str, window, seed: int
) -> None:
    """Cache the full ranked Pareto candidate list, so the trade-count overfit guard
    (which depends on the previous window's chosen config) can re-select on a cache
    hit without re-optimizing the front."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cache_key": key,
        "window": window.to_dict(),
        "seed": seed,
        "candidates": [_choice_to_dict(c) for c in candidates],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def _load_cached_candidates(
    path: Path,
    expected_key: Optional[str] = None,
    expected_window: Optional[Dict[str, Any]] = None,
    expected_seed: Optional[int] = None,
) -> Optional[List[ParetoChoice]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if not _cache_header_ok(d, expected_key, expected_window, expected_seed):
            return None
        cands = d.get("candidates")
        if not isinstance(cands, list) or not cands:
            return None
        return [_choice_from_dict(c) for c in cands]
    except Exception:
        return None


class WindowOptimizeError(Exception):
    """A single window's optimization could not produce a config.

    ``code`` mirrors the orchestrator's historical exit codes: 2 => optimizer
    subprocess failed; 3 => no usable Pareto front.
    """

    def __init__(self, code: int, message: str):
        self.code = int(code)
        super().__init__(message)


def optimize_one_window(
    base_config: Dict[str, Any],
    window,
    *,
    seed: int,
    stop_cfg: Dict[str, Any],
    proximity_weight: float,
    proximity_reference: Optional[str],
    warm_start: Optional[str],
    scoring_keys: List[str],
    train_cfg_path: str,
    results_dir: str,
    log_path: str,
    iters: Optional[int] = None,
    n_cpus: Optional[int] = None,
    cache_dir: Optional[Path] = None,
    no_cache: bool = False,
    optimize_script: Optional[str] = None,
):
    """Optimize one walk-forward window: build train cfg → cache → optimize → select.

    Shared by the backtest orchestrator (:func:`run`) and the live scheduler
    (``wfo_scheduler``) so both use one implementation and the same content-addressed
    cache. Writes the train config to ``train_cfg_path``; on a cache miss runs the
    optimizer subprocess into ``results_dir`` (logging to ``log_path``) and stores the
    chosen result in the cache.

    Returns ``(candidates: List[ParetoChoice], cache_key: str, cache_hit: bool)`` — the
    full Pareto front ranked best-first (``candidates[0]`` is the scalarized winner; the
    caller applies the trade-count overfit guard to pick among them). Raises
    :class:`WindowOptimizeError` (code 2 optimizer failed, 3 empty Pareto front).
    """
    optimize_script = optimize_script or str(SRC_ROOT / "optimize.py")
    train_cfg = build_train_config(
        base_config, window, seed, stop_cfg, float(proximity_weight),
        proximity_reference, iters, n_cpus,
    )
    Path(train_cfg_path).parent.mkdir(parents=True, exist_ok=True)
    dump_config(train_cfg, str(train_cfg_path))

    cache_key = window_cache_key(train_cfg, warm_start)
    key_dir = (cache_dir / cache_key) if cache_dir else None
    cand_entry = (key_dir / "candidates.json") if key_dir else None
    legacy_entry = (key_dir / "choice.json") if key_dir else None
    candidates: Optional[List[ParetoChoice]] = None
    cache_hit = False
    if cand_entry is not None and not no_cache and cand_entry.exists():
        candidates = _load_cached_candidates(
            cand_entry, expected_key=cache_key,
            expected_window=window.to_dict(), expected_seed=seed,
        )
        if candidates:
            cache_hit = True
            logger.info("window %02d | cache HIT %s -> reusing %d candidate(s)",
                        window.index, cache_key[:12], len(candidates))
        else:
            logger.info("window %02d | cache entry invalid for %s -> recomputing",
                        window.index, cache_key[:12])
    if candidates is None and legacy_entry is not None and not no_cache and legacy_entry.exists():
        # Legacy single-choice cache entry: usable, but with no fall-back candidates.
        legacy = _load_cached_choice(
            legacy_entry, expected_key=cache_key,
            expected_window=window.to_dict(), expected_seed=seed,
        )
        if legacy is not None:
            candidates = [legacy]
            cache_hit = True
            logger.info("window %02d | cache HIT %s (legacy single choice)",
                        window.index, cache_key[:12])

    if not candidates:
        opt_cmd = [
            sys.executable, optimize_script, str(train_cfg_path),
            "--results-dir", str(results_dir),
            "--seed", str(seed),
            "--skip-rust-compile",
        ]
        if warm_start:
            opt_cmd += ["--start", warm_start]
        rc = _run_subprocess(opt_cmd, Path(log_path))
        if rc != 0:
            raise WindowOptimizeError(2, f"optimizer failed (rc={rc}); see {log_path}")
        candidates = rank_pareto_candidates(str(Path(results_dir) / "pareto"), scoring_keys)
        if not candidates:
            raise WindowOptimizeError(3, f"no usable Pareto front; see {log_path}")
        if cand_entry is not None and not no_cache:
            _save_cached_candidates(cand_entry, candidates, cache_key, window, seed)

    return candidates, cache_key, cache_hit


def _find_latest(base_dir: str, filename: str) -> Optional[str]:
    matches = glob.glob(os.path.join(base_dir, "**", filename), recursive=True)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_equity_segment(bal_eq_path: str) -> Dict[str, List[Any]]:
    import pandas as pd

    df = pd.read_csv(bal_eq_path)
    equity_col = None
    for col in ("usd_total_equity", "btc_total_equity"):
        if col in df.columns:
            equity_col = col
            break
    # Fallback: last numeric column.
    if equity_col is None:
        numeric_cols = [c for c in df.columns if df[c].dtype.kind in "fi"]
        if numeric_cols:
            equity_col = numeric_cols[-1]
    if equity_col is None:
        return {"equity": []}

    equity = [float(x) for x in df[equity_col].tolist()]
    timestamp_col = _find_timestamp_column(df, equity_col)
    if not timestamp_col:
        return {"equity": equity}
    timestamps = _parse_timestamp_values(df[timestamp_col])
    if not timestamps or len(timestamps) != len(equity):
        return {"equity": equity}
    return {"timestamps": timestamps, "equity": equity}


def _find_timestamp_column(df, equity_col: str) -> Optional[str]:
    import pandas as pd

    preferred = ["timestamp", "date", "datetime", "time", "Unnamed: 0"]
    for col in preferred:
        if col not in df.columns or col == equity_col:
            continue
        parsed = pd.to_datetime(df[col], errors="coerce", utc=True)
        if parsed.notna().any():
            return col
    for col in df.columns:
        if col in preferred or col == equity_col or df[col].dtype.kind in "fiub":
            continue
        parsed = pd.to_datetime(df[col], errors="coerce", utc=True)
        if parsed.notna().any():
            return col
    return None


def _parse_timestamp_values(values) -> List[int]:
    import pandas as pd

    parsed = pd.to_datetime(values, errors="coerce", utc=True)
    if not parsed.notna().all():
        return []
    return [int(ts.value // 1_000_000) for ts in parsed]


def _resolve_metric(analysis: Dict[str, Any], key: str) -> Optional[float]:
    """Best-effort lookup of a scoring key in a flat analysis dict."""
    for candidate in (key, f"{key}_usd", f"{key}_btc"):
        if candidate in analysis:
            try:
                return float(analysis[candidate])
            except (TypeError, ValueError):
                return None
    return None


def _is_metric_from_pareto(metrics: Dict[str, Any], key: str) -> Optional[float]:
    stats = (metrics or {}).get("stats", {}) or {}
    for candidate in (key, f"{key}_usd", f"{key}_btc"):
        entry = stats.get(candidate)
        if isinstance(entry, dict) and "mean" in entry:
            try:
                return float(entry["mean"])
            except (TypeError, ValueError):
                return None
    return None


def _summarize_trade_guards(window_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll the per-window trade-count guard decisions into one at-a-glance summary.

    Reads each record's ``trade_guard`` dict (produced by
    :func:`tools.wfo_utils.select_with_trade_guard`) and reports, across the whole run,
    in which windows the guard had to walk down the Pareto front (``chosen_rank > 0``),
    where it fell back because no candidate cleared the threshold (``no_pass``), and how
    many candidates were rejected in total. Tolerant of missing keys (legacy records).
    """
    min_trade_ratio: Optional[float] = None
    walked: List[int] = []
    no_pass: List[int] = []
    seeded: List[int] = []
    rejected_total = 0
    for rec in window_records:
        tg = rec.get("trade_guard") or {}
        if min_trade_ratio is None and tg.get("min_trade_ratio") is not None:
            min_trade_ratio = tg.get("min_trade_ratio")
        idx = rec.get("index")
        if tg.get("seeded"):  # window 0: initial config used as-is, not optimized
            seeded.append(idx)
            continue
        if tg.get("no_pass"):
            no_pass.append(idx)
        elif tg.get("chosen_rank"):  # rank 0 / None => guard left the top pick alone
            walked.append(idx)
        rejected_total += len(tg.get("rejected", []) or [])
    return {
        "min_trade_ratio": min_trade_ratio,
        "windows_total": len(window_records),
        "windows_seeded": seeded,
        "windows_guard_walked": walked,
        "windows_no_pass": no_pass,
        "n_candidates_rejected_total": rejected_total,
    }


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    if bool(args.meta) == bool(args.config):
        logger.error("Provide exactly one of --meta (stand-alone meta file) or --config "
                     "(config with an embedded walk_forward block).")
        return 1

    if args.meta:
        # Stand-alone meta file: knobs from the meta, bounds/optimize/universe from base_config.
        wf, config_path = load_wf_meta(args.meta)
        wf = resolve_wf_params(wf, args)  # CLI overrides on top (re-merge is idempotent)
    else:
        config_path = _abspath(args.config)
        raw = load_hjson_config(config_path)
        wf_block = raw.get("walk_forward", {}) if isinstance(raw, dict) else {}
        wf = resolve_wf_params(wf_block, args)

    # Clean, formatted base config for children (walk_forward stripped by format_config).
    base_config = load_config(config_path, verbose=False)
    scoring_keys = list(base_config.get("optimize", {}).get("scoring", []))

    # Overall span: walk_forward overrides, else the base backtest dates.
    start_date = wf["start_date"] or base_config["backtest"]["start_date"]
    end_raw = wf["end_date"] or base_config["backtest"]["end_date"]
    start_date = format_end_date(start_date) if str(start_date) in ("now", "today", "") else start_date
    end_date = format_end_date(end_raw)  # resolves "now" once and pins it

    windows = generate_windows(
        start_date=str(start_date),
        end_date=str(end_date),
        train_months=int(wf["train_months"]),
        test_months=int(wf["test_months"]),
        step_months=int(wf["step_months"]),
        calendar_months=bool(wf["calendar_months"]),
        min_test_days=int(wf["min_test_days"]),
    )
    if not windows:
        logger.error(
            "No windows generated for span %s -> %s with train=%dmo test=%dmo step=%dmo",
            start_date, end_date, wf["train_months"], wf["test_months"], wf["step_months"],
        )
        return 1

    run_id = wf["run_id"] or ts_to_date(utc_ms())[:19].replace(":", "_")
    run_dir = Path(_abspath(os.path.join("runs", "walkforward", str(run_id))))
    run_dir.mkdir(parents=True, exist_ok=True)
    history_dir = run_dir / "configs_history"
    history_dir.mkdir(parents=True, exist_ok=True)

    # Shared, content-addressed cache of per-window optimizations (keyed by
    # meta-parameters). Shared across run_ids so reruns/backtests/live reuse results.
    cache_dir: Optional[Path] = None
    if not args.no_cache:
        cache_dir = Path(_abspath(args.cache_dir or os.path.join("runs", "walkforward", "_cache")))
        cache_dir.mkdir(parents=True, exist_ok=True)

    windows_payload = {
        "run_id": run_id,
        "span": {"start_date": str(start_date), "end_date": str(end_date)},
        "params": {k: v for k, v in wf.items()},
        "windows": [w.to_dict() for w in windows],
    }
    with open(run_dir / "windows.json", "w", encoding="utf-8") as fh:
        json.dump(windows_payload, fh, indent=2, sort_keys=True)
    with open(run_dir / "walkforward_config.json", "w", encoding="utf-8") as fh:
        json.dump({"config_path": config_path, "walk_forward": wf}, fh, indent=2, sort_keys=True)

    logger.info("Generated %d window(s) for run '%s'", len(windows), run_id)
    for w in windows:
        logger.info(
            "  window %02d | train %s -> %s | test %s -> %s",
            w.index, w.train_start, w.train_end, w.test_start, w.test_end,
        )

    if args.dry_run:
        print(json.dumps(windows_payload, indent=2, sort_keys=True))
        logger.info("Dry run: wrote %s", run_dir / "windows.json")
        return 0

    # Best-effort Rust build once; children skip the compile check.
    try:
        from rust_utils import check_and_maybe_compile

        check_and_maybe_compile(skip=False, force=False, fail_on_stale=False)
    except Exception as exc:
        logger.warning("Rust pre-build check failed (%s); children will assume a built extension", exc)

    optimize_script = str(SRC_ROOT / "optimize.py")
    backtest_script = str(SRC_ROOT / "backtest.py")
    bounds = extract_bounds_tuple_list_from_config(base_config)
    bot_keys = [
        f"{pside}.{k}"
        for pside in sorted(base_config.get("bot", {}))
        for k in sorted(base_config["bot"][pside])
    ]
    bounds_ranges = {
        key: (b.high - b.low) for key, b in zip(bot_keys, bounds)
    }

    window_records: List[Dict[str, Any]] = []
    oos_segments: List[Any] = []
    prev_config_path: Optional[str] = None
    prev_config_dict: Optional[Dict[str, Any]] = None

    # Stateful OOS carry-over: when enabled, each OOS segment is seeded with the
    # previous segment's carried balance + open positions (handoff rule applied in
    # between), yielding one continuous equity curve instead of flat-restart segments.
    stateful_oos = bool(wf.get("stateful_oos"))
    max_loss_flatten_frac = float(wf.get("max_loss_flatten_frac", 0.02) or 0.02)
    base_starting_balance = float(base_config.get("backtest", {}).get("starting_balance", 1.0) or 1.0)
    carry: Dict[str, Any] = {"balance": base_starting_balance, "positions": {}}

    # Trade-count overfit guard: reject a window's chosen config if its in-sample trade
    # rate dropped below min_trade_ratio of the previous window's chosen config, walking
    # down the Pareto front to the next-best that holds up (see select_with_trade_guard).
    min_trade_ratio = float(wf.get("min_trade_ratio", 0.0) or 0.0)
    prev_trade_rate: Optional[float] = None

    for w in windows:
        wdir = run_dir / f"window_{w.index:02d}"
        train_dir = wdir / "train"
        test_dir = wdir / "test"
        train_dir.mkdir(parents=True, exist_ok=True)
        test_dir.mkdir(parents=True, exist_ok=True)

        seed = int(wf["base_seed"]) + w.index

        if w.index == 0:
            # Seed window: deploy the hand-tuned initial config as-is (no optimization).
            # Optimization begins at window 1, warm-started from this config. The live
            # scheduler applies the identical rule, so live and backtest stay in parity.
            init_path = _abspath(wf["initial_config"])
            choice = seed_choice_from_config(init_path)
            candidates = [choice]
            cache_key, cache_hit = None, False
            warm_start = init_path if os.path.exists(init_path) else None
            trade_guard = {"applied": False, "seeded": True}
            logger.info(
                "window 00 | seed: using initial config as-is (no optimization) | %s",
                init_path,
            )
        else:
            # Warm-start source: previous window's chosen config (the initial config for
            # window 1, since the seed window saved it as train_best.json).
            warm_start = prev_config_path or _abspath(wf["initial_config"])
            if not os.path.exists(warm_start):
                logger.warning("Warm-start config not found: %s (continuing without)", warm_start)
                warm_start = None

            # Proximity reference is the PREVIOUS window's chosen config (the initial config
            # for window 1). The "don't drift far per slide" bias applies between consecutive
            # windows along the chain.
            proximity_reference = prev_config_path

            # Deterministic content-addressed cache: identical meta-parameters
            # (initial config, seed, period, stop criteria, base config) => identical
            # result, reused by any backtest or live rerun (see optimize_one_window).
            try:
                candidates, cache_key, cache_hit = optimize_one_window(
                    base_config, w,
                    seed=seed,
                    stop_cfg=wf["stop"],
                    proximity_weight=float(wf["proximity_weight"]),
                    proximity_reference=proximity_reference,
                    warm_start=warm_start,
                    scoring_keys=scoring_keys,
                    train_cfg_path=str(train_dir / "train_config.json"),
                    results_dir=str(train_dir / "optimize_results"),
                    log_path=str(train_dir / "optimize.log"),
                    iters=args.iters,
                    n_cpus=args.n_cpus,
                    cache_dir=cache_dir,
                    no_cache=args.no_cache,
                    optimize_script=optimize_script,
                )
            except WindowOptimizeError as exc:
                logger.error("window %02d | %s", w.index, exc)
                if not args.keep_going:
                    return exc.code
                continue

            # Trade-count overfit guard: pick the best-ranked candidate that does not trade
            # far less than last month (falls back down the front). Inactive on window 1 --
            # the seed window has no in-sample trade rate, so prev_trade_rate is still None;
            # the guard begins comparing at window 2.
            choice, trade_guard = select_with_trade_guard(candidates, prev_trade_rate, min_trade_ratio)
            if trade_guard.get("no_pass"):
                _r = trade_guard.get("chosen_trade_rate")
                logger.warning(
                    "window %02d | trade-guard: NO candidate >= %.2f x prev rate %.4f; "
                    "falling back to highest-rate (rank %d, rate=%s)",
                    w.index, min_trade_ratio, prev_trade_rate or 0.0,
                    trade_guard.get("chosen_rank", 0),
                    f"{_r:.4f}" if _r is not None else "n/a",
                )
            elif trade_guard.get("applied") and trade_guard.get("chosen_rank"):
                _r = trade_guard.get("chosen_trade_rate")
                logger.warning(
                    "window %02d | trade-guard: rejected %d higher-ranked candidate(s) "
                    "(trade rate < %.2f x prev %.4f); chose rank %d (rate=%s)",
                    w.index, len(trade_guard.get("rejected", [])), min_trade_ratio,
                    prev_trade_rate or 0.0, trade_guard.get("chosen_rank", 0),
                    f"{_r:.4f}" if _r is not None else "n/a",
                )

        # Save the chosen config (a complete config: backtest- and live-ready).
        train_best_path = wdir / "train_best.json"
        dump_config(choice.config, str(train_best_path))
        # Chronological history copy.
        history_name = f"window_{w.index:02d}_{w.train_end}.json"
        dump_config(choice.config, str(history_dir / history_name))

        # --- OOS evaluation ---
        test_base_dir = str(test_dir / "bt")
        if stateful_oos:
            test_cfg = build_test_config(
                base_config, choice.config, w, test_base_dir,
                starting_balance=carry["balance"],
                initial_positions=carry["positions"],
            )
        else:
            test_cfg = build_test_config(base_config, choice.config, w, test_base_dir)
        test_cfg_path = test_dir / "test_config.json"
        dump_config(test_cfg, str(test_cfg_path))

        bt_cmd = [sys.executable, backtest_script, str(test_cfg_path), "-dp", "--skip-rust-compile"]
        rc = _run_subprocess(bt_cmd, test_dir / "backtest.log")
        oos_analysis: Dict[str, Any] = {}
        oos_segment: Dict[str, List[Any]] = {"equity": []}
        if rc != 0:
            logger.error("Backtest failed for window %02d (rc=%d); see %s",
                         w.index, rc, test_dir / "backtest.log")
            if not args.keep_going:
                return 4
        else:
            analysis_path = _find_latest(test_base_dir, "analysis.json")
            if analysis_path:
                try:
                    with open(analysis_path, "r", encoding="utf-8") as fh:
                        oos_analysis = json.load(fh)
                except Exception as exc:
                    logger.warning("Failed to read OOS analysis for window %02d: %s", w.index, exc)
            bal_eq_path = _find_latest(test_base_dir, "balance_and_equity.csv.gz")
            if bal_eq_path:
                try:
                    oos_segment = _load_equity_segment(bal_eq_path)
                except Exception as exc:
                    logger.warning("Failed to read OOS equity for window %02d: %s", w.index, exc)
            # Stateful carry: advance balance + open positions for the next segment.
            if stateful_oos:
                es_path = _find_latest(test_base_dir, "end_state.json")
                if es_path:
                    try:
                        with open(es_path, "r", encoding="utf-8") as fh:
                            end_state = json.load(fh)
                        carry = advance_carry(end_state, max_loss_flatten_frac)
                        logger.info(
                            "window %02d | carry -> balance=%.4f | kept positions=%d",
                            w.index, carry["balance"], len(carry["positions"]),
                        )
                    except Exception as exc:
                        logger.warning("Failed to advance carry for window %02d: %s", w.index, exc)
                else:
                    logger.warning("window %02d | stateful_oos on but no end_state.json found", w.index)

        if oos_segment.get("equity"):
            oos_segments.append(oos_segment)

        # --- per-window overfit (IS vs OOS) on the scoring keys ---
        overfit = {}
        for key in scoring_keys:
            is_val = _is_metric_from_pareto(choice.metrics, key)
            oos_val = _resolve_metric(oos_analysis, key)
            ratio = None
            if is_val not in (None, 0) and oos_val is not None:
                ratio = oos_val / is_val
            overfit[key] = {"in_sample": is_val, "out_of_sample": oos_val, "oos_is_ratio": ratio}

        drift = {}
        if prev_config_dict is not None:
            drift = param_drift(prev_config_dict, choice.config, bounds_ranges)

        chosen_trade_rate = pareto_trade_rate(choice)
        record = {
            "index": w.index,
            "window": w.to_dict(),
            "seed": seed,
            "warm_start": warm_start,
            "cache_key": cache_key,
            "cache_hit": cache_hit,
            "chosen_hash": choice.hash_id,
            "pareto_candidates": choice.n_candidates,
            "objectives": list(choice.objectives),
            "constraint_violation": choice.violation,
            "train_best_config": str(train_best_path),
            "trade_rate": chosen_trade_rate,
            "trade_guard": trade_guard,
            "seeded": bool(trade_guard.get("seeded")),
            "overfit": overfit,
            "oos_analysis": oos_analysis,
            "param_drift": drift,
        }
        window_records.append(record)
        with open(wdir / "window_summary.json", "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)

        prev_config_path = str(train_best_path)
        prev_config_dict = choice.config
        # Baseline for next window's trade-count guard (the chosen config's in-sample rate).
        if chosen_trade_rate is not None:
            prev_trade_rate = chosen_trade_rate
        logger.info("window %02d done | chosen=%s | trade_rate=%s | OOS points=%d",
                    w.index, choice.hash_id,
                    f"{chosen_trade_rate:.4f}" if chosen_trade_rate is not None else "n/a",
                    len(oos_segment.get("equity", [])))

    # --- aggregation ---
    summary_dir = run_dir / "walkforward_summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    starting_balance = float(base_config.get("backtest", {}).get("starting_balance", 1.0) or 1.0)
    stitched = stitch_oos_equity(oos_segments, starting_balance=starting_balance, stateful=stateful_oos)

    # stitched equity CSV
    with open(summary_dir / "stitched_equity.csv", "w", encoding="utf-8") as fh:
        timestamps = stitched.get("timestamps")
        if timestamps and len(timestamps) == len(stitched["equity"]):
            fh.write("timestamp,equity\n")
            for ts, value in zip(timestamps, stitched["equity"]):
                fh.write(f"{ts},{value}\n")
        else:
            fh.write("index,equity\n")
            for i, value in enumerate(stitched["equity"]):
                fh.write(f"{i},{value}\n")

    # mean overfit ratio per scoring key
    overfit_means: Dict[str, Any] = {}
    for key in scoring_keys:
        ratios = [
            r["overfit"][key]["oos_is_ratio"]
            for r in window_records
            if r["overfit"].get(key, {}).get("oos_is_ratio") is not None
        ]
        overfit_means[key] = (sum(ratios) / len(ratios)) if ratios else None

    trade_guard_summary = _summarize_trade_guards(window_records)
    summary = {
        "run_id": run_id,
        "span": {"start_date": str(start_date), "end_date": str(end_date)},
        "params": wf,
        "n_windows": len(windows),
        "n_windows_completed": len(window_records),
        "results": {
            "oos_stitched_metrics": stitched["metrics"],
            "overfit_mean_oos_is_ratio": overfit_means,
            "trade_guard": trade_guard_summary,
            "windows": window_records,
        },
    }
    with open(summary_dir / "walkforward_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)

    # Latest config = current deployable strategy (backtest- and live-ready).
    if prev_config_path and os.path.exists(prev_config_path):
        latest = load_config(prev_config_path, verbose=False)
        dump_config(latest, str(run_dir / "latest_config.json"))

    _maybe_plot(stitched["equity"], summary_dir / "stitched_equity.png")

    logger.info("Walk-forward complete | windows=%d/%d | OOS total_return=%.4f | "
                "guard: walked=%d no_pass=%d | summary=%s",
                len(window_records), len(windows),
                stitched["metrics"].get("total_return", 0.0),
                len(trade_guard_summary["windows_guard_walked"]),
                len(trade_guard_summary["windows_no_pass"]),
                summary_dir / "walkforward_summary.json")
    return 0


def _maybe_plot(equity: List[float], out_path: Path) -> None:
    if not equity:
        return
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 1, figsize=(12, 8))
        axes[0].plot(equity, color="tab:blue")
        axes[0].set_title("Stitched out-of-sample equity")
        axes[0].set_ylabel("equity")
        axes[1].plot(equity, color="tab:blue")
        axes[1].set_yscale("log")
        axes[1].set_ylabel("equity (log)")
        axes[1].set_xlabel("minute index (concatenated OOS windows)")
        fig.tight_layout()
        fig.savefig(str(out_path), dpi=100)
        plt.close(fig)
    except Exception as exc:
        logger.warning("Could not render stitched equity plot: %s", exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="walkforward", description="Walk-forward optimization")
    parser.add_argument("--meta", type=str, default=None,
                        help="Path to a stand-alone walk-forward meta file (configs/wfo_meta.json); "
                             "references a base_config for bot bounds / optimize / universe")
    parser.add_argument("--config", default=None,
                        help="Path to WFO config with an embedded walk_forward block "
                             "(legacy; use --meta or --config)")
    parser.add_argument("--train-months", type=int, default=None)
    parser.add_argument("--test-months", type=int, default=None)
    parser.add_argument("--step-months", type=int, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--base-seed", type=int, default=None)
    parser.add_argument("--proximity-weight", type=float, default=None)
    parser.add_argument("--min-trade-ratio", dest="min_trade_ratio", type=float, default=None,
                        help="Overfit guard: reject a chosen config whose in-sample trade rate "
                             "drops below this fraction of the previous window (0 disables)")
    parser.add_argument("--initial-config", type=str, default=None,
                        help="Starting config to warm-start the first window (default hype_top.json)")
    parser.add_argument("--patience", type=int, default=None)
    parser.add_argument("--min-rel-improvement", type=float, default=None)
    parser.add_argument("--max-evals", dest="max_evals", type=int, default=None,
                        help="Hard cap on total evaluations per window (0 = no cap)")
    parser.add_argument("--iters", type=int, default=None, help="Override optimize.iters per window")
    parser.add_argument("--n-cpus", type=int, default=None, help="Override optimize.n_cpus per window")
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument("--calendar-months", dest="calendar_months", action="store_true", default=None)
    parser.add_argument("--fixed-30day", dest="calendar_months", action="store_false")
    parser.add_argument("--cache-dir", type=str, default=None,
                        help="Directory for the content-addressed optimization cache "
                             "(default runs/walkforward/_cache, shared across runs)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Disable the optimization result cache (always recompute)")
    parser.add_argument("--dry-run", action="store_true", help="Print windows and exit")
    parser.add_argument("--keep-going", action="store_true",
                        help="Continue to the next window if one fails")
    parser.add_argument("--log-level", dest="log_level", default="info")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    level_map = {"warning": 0, "info": 1, "debug": 2, "trace": 3}
    configure_logging(debug=level_map.get(str(args.log_level).lower(), 1))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
