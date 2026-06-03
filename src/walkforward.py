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
    select_best_from_pareto,
    param_drift,
    stitch_oos_equity,
)

logger = logging.getLogger("walkforward")

# Shared, repo-level lighter 1m data cache. Per-window child configs would
# otherwise anchor the lighter data dir to their own (window) directory via
# live.base_config_path, causing a re-download per window. Pinning an absolute
# shared path makes every window, OOS backtest, and future rerun reuse one cache.
SHARED_LIGHTER_DATA_DIR = str((REPO_ROOT / "caches" / "ohlcv" / "lighter" / "1m").resolve())

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
}


# ---------------------------------------------------------------------------
# Config / parameter resolution
# ---------------------------------------------------------------------------
def resolve_wf_params(wf_block: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """Merge precedence: CLI > walk_forward block > defaults."""
    wf = deepcopy(WF_DEFAULTS)
    if isinstance(wf_block, dict):
        for key, value in wf_block.items():
            if key == "stop" and isinstance(value, dict):
                wf["stop"].update(value)
            elif value is not None:
                wf[key] = value

    cli_map = {
        "train_months": args.train_months,
        "test_months": args.test_months,
        "step_months": args.step_months,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "base_seed": args.base_seed,
        "proximity_weight": args.proximity_weight,
        "initial_config": args.initial_config,
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
    cfg.pop("results_dir", None)
    cfg.pop("results_filename", None)
    cfg.pop("disable_plotting", None)
    cfg.pop("analysis", None)
    cfg.pop("logging", None)
    bt = cfg.get("backtest", {}) or {}
    for key in ("base_dir", "cache_dir", "coins", "lighter_data_dir"):
        bt.pop(key, None)  # machine-specific data location, not a meta-parameter
    live = cfg.get("live", {}) or {}
    live.pop("base_config_path", None)
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


def _save_cached_choice(path: Path, choice: ParetoChoice, key: str, window, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cache_key": key,
        "window": window.to_dict(),
        "seed": seed,
        "hash_id": choice.hash_id,
        "objectives": list(choice.objectives),
        "violation": choice.violation,
        "distance": choice.distance,
        "n_candidates": choice.n_candidates,
        "metrics": choice.metrics,
        "config": choice.config,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)


def _load_cached_choice(path: Path) -> Optional[ParetoChoice]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return ParetoChoice(
            hash_id=d["hash_id"],
            config=d["config"],
            objectives=tuple(d.get("objectives", [])),
            violation=float(d.get("violation", 0.0)),
            distance=float(d.get("distance", 0.0)),
            metrics=d.get("metrics", {}) or {},
            n_candidates=int(d.get("n_candidates", 0)),
        )
    except Exception:
        return None


def _find_latest(base_dir: str, filename: str) -> Optional[str]:
    matches = glob.glob(os.path.join(base_dir, "**", filename), recursive=True)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _load_equity_segment(bal_eq_path: str) -> List[float]:
    import pandas as pd

    df = pd.read_csv(bal_eq_path)
    for col in ("usd_total_equity", "btc_total_equity"):
        if col in df.columns:
            return [float(x) for x in df[col].tolist()]
    # Fallback: last numeric column.
    numeric_cols = [c for c in df.columns if df[c].dtype.kind in "fi"]
    if numeric_cols:
        return [float(x) for x in df[numeric_cols[-1]].tolist()]
    return []


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


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
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
    oos_segments: List[List[float]] = []
    prev_config_path: Optional[str] = None
    prev_config_dict: Optional[Dict[str, Any]] = None

    for w in windows:
        wdir = run_dir / f"window_{w.index:02d}"
        train_dir = wdir / "train"
        test_dir = wdir / "test"
        train_dir.mkdir(parents=True, exist_ok=True)
        test_dir.mkdir(parents=True, exist_ok=True)

        seed = int(wf["base_seed"]) + w.index
        # Warm-start source: previous window's chosen config, else the initial config.
        warm_start = prev_config_path or _abspath(wf["initial_config"])
        if not os.path.exists(warm_start):
            logger.warning("Warm-start config not found: %s (continuing without)", warm_start)
            warm_start = None

        train_cfg = build_train_config(
            base_config, w, seed, wf["stop"], float(wf["proximity_weight"]),
            warm_start, args.iters, args.n_cpus,
        )
        train_cfg_path = train_dir / "train_config.json"
        dump_config(train_cfg, str(train_cfg_path))

        # Deterministic content-addressed cache: identical meta-parameters
        # (initial config, seed, period, stop criteria, base config) => identical
        # result, so reuse a prior optimization instead of recomputing. The same
        # key is reproduced by any backtest or live rerun.
        cache_key = window_cache_key(train_cfg, warm_start)
        cache_entry = (cache_dir / cache_key / "choice.json") if cache_dir else None
        choice = None
        cache_hit = False
        if cache_entry is not None and not args.no_cache and cache_entry.exists():
            choice = _load_cached_choice(cache_entry)
            if choice is not None:
                cache_hit = True
                logger.info("window %02d | cache HIT %s -> reusing optimization",
                            w.index, cache_key[:12])

        if choice is None:
            results_dir = train_dir / "optimize_results"
            opt_cmd = [
                sys.executable, optimize_script, str(train_cfg_path),
                "--results-dir", str(results_dir),
                "--seed", str(seed),
                "--skip-rust-compile",
            ]
            if warm_start:
                opt_cmd += ["--start", warm_start]
            rc = _run_subprocess(opt_cmd, train_dir / "optimize.log")
            if rc != 0:
                logger.error("Optimizer failed for window %02d (rc=%d); see %s",
                             w.index, rc, train_dir / "optimize.log")
                if not args.keep_going:
                    return 2
                continue

            choice = select_best_from_pareto(str(results_dir / "pareto"), scoring_keys)
            if choice is None:
                logger.error("No usable Pareto front for window %02d; see %s",
                             w.index, train_dir / "optimize.log")
                if not args.keep_going:
                    return 3
                continue

            if cache_entry is not None and not args.no_cache:
                _save_cached_choice(cache_entry, choice, cache_key, w, seed)

        # Save the chosen config (a complete config: backtest- and live-ready).
        train_best_path = wdir / "train_best.json"
        dump_config(choice.config, str(train_best_path))
        # Chronological history copy.
        history_name = f"window_{w.index:02d}_{w.train_end}.json"
        dump_config(choice.config, str(history_dir / history_name))

        # --- OOS evaluation ---
        test_base_dir = str(test_dir / "bt")
        test_cfg = build_test_config(base_config, choice.config, w, test_base_dir)
        test_cfg_path = test_dir / "test_config.json"
        dump_config(test_cfg, str(test_cfg_path))

        bt_cmd = [sys.executable, backtest_script, str(test_cfg_path), "-dp", "--skip-rust-compile"]
        rc = _run_subprocess(bt_cmd, test_dir / "backtest.log")
        oos_analysis: Dict[str, Any] = {}
        oos_segment: List[float] = []
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

        if oos_segment:
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
            "overfit": overfit,
            "oos_analysis": oos_analysis,
            "param_drift": drift,
        }
        window_records.append(record)
        with open(wdir / "window_summary.json", "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)

        prev_config_path = str(train_best_path)
        prev_config_dict = choice.config
        logger.info("window %02d done | chosen=%s | OOS points=%d",
                    w.index, choice.hash_id, len(oos_segment))

    # --- aggregation ---
    summary_dir = run_dir / "walkforward_summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    starting_balance = float(base_config.get("backtest", {}).get("starting_balance", 1.0) or 1.0)
    stitched = stitch_oos_equity(oos_segments, starting_balance=starting_balance)

    # stitched equity CSV
    with open(summary_dir / "stitched_equity.csv", "w", encoding="utf-8") as fh:
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

    summary = {
        "run_id": run_id,
        "span": {"start_date": str(start_date), "end_date": str(end_date)},
        "params": wf,
        "n_windows": len(windows),
        "n_windows_completed": len(window_records),
        "results": {
            "oos_stitched_metrics": stitched["metrics"],
            "overfit_mean_oos_is_ratio": overfit_means,
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

    logger.info("Walk-forward complete | windows=%d/%d | OOS total_return=%.4f | summary=%s",
                len(window_records), len(windows),
                stitched["metrics"].get("total_return", 0.0),
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
    parser.add_argument("--config", required=True, help="Path to WFO config (with walk_forward block)")
    parser.add_argument("--train-months", type=int, default=None)
    parser.add_argument("--test-months", type=int, default=None)
    parser.add_argument("--step-months", type=int, default=None)
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--end-date", type=str, default=None)
    parser.add_argument("--base-seed", type=int, default=None)
    parser.add_argument("--proximity-weight", type=float, default=None)
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
