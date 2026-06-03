#!/usr/bin/env python3
"""Decoupled walk-forward live scheduler.

Runs SEPARATELY from the live trading bot (its own process / container). It never
touches the exchange. Each tick it:

  1. resolves the current live window from a FIXED anchor (deterministic, never "now"),
  2. replays the warm-start chain window-by-window through the shared content-addressed
     optimization cache (already-computed months are instant cache hits; only a new
     month actually optimizes), and
  3. atomically publishes the chosen config for the current month so the live bot can
     adopt it (active_config.json + active.json pointer + a dated history copy).

This honours the user's requirement that an optimization is computed once and reused by
any backtest or live rerun, and keeps the heavy multiprocessing optimizer out of the
memory-limited live container.

Usage (conda env per CLAUDE.md):
    python src/wfo_scheduler.py --config configs/wfo_hype.json --once
    python src/wfo_scheduler.py --config configs/wfo_hype.json           # loop forever
    python src/wfo_scheduler.py --config configs/wfo_hype.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

SRC_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config_utils import load_hjson_config, load_config, dump_config  # noqa: E402
from utils import ts_to_date, utc_ms  # noqa: E402
from logging_setup import configure_logging  # noqa: E402
from tools.wfo_utils import generate_windows, pareto_trade_rate, select_with_trade_guard  # noqa: E402
from tools.wfo_handoff import current_live_window  # noqa: E402
from tools.wfo_meta import WF_DEFAULTS, merge_wf_params, load_wf_meta  # noqa: E402,F401
from walkforward import (  # noqa: E402
    optimize_one_window,
    seed_choice_from_config,
    WindowOptimizeError,
    _abspath,
)

logger = logging.getLogger("wfo_scheduler")


# ---------------------------------------------------------------------------
# Param resolution (config block + a few CLI overrides; CLI > config > default)
# ---------------------------------------------------------------------------
def resolve_params(wf_block: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    wf = merge_wf_params(wf_block)
    if getattr(args, "initial_config", None):
        wf["initial_config"] = args.initial_config
    if getattr(args, "cache_dir", None):
        wf["live_rolling"]["cache_dir"] = args.cache_dir
    if getattr(args, "check_interval_minutes", None) is not None:
        wf["live_rolling"]["check_interval_minutes"] = args.check_interval_minutes
    return wf


# ---------------------------------------------------------------------------
# Atomic artifact publishing
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, text: str) -> None:
    """Write text to ``path`` atomically (tmp + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _publish(active_dir: Path, config: Dict[str, Any], pointer: Dict[str, Any]) -> None:
    """Publish the active config + metadata pointer atomically.

    The bot reads ``active_config.json`` (the deployable config) and ``active.json``
    (period/window metadata). A dated copy is kept under ``configs_history/``.
    """
    active_dir.mkdir(parents=True, exist_ok=True)
    # dump_config writes streamlined JSON; render to a string then write atomically.
    cfg_text = json.dumps(config, indent=2, sort_keys=True)
    _atomic_write(active_dir / "active_config.json", cfg_text)
    history_dir = active_dir / "configs_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    hist_name = f"window_{pointer['window_index']:02d}_{pointer['train_window']['train_end']}.json"
    _atomic_write(history_dir / hist_name, cfg_text)
    _atomic_write(active_dir / "active.json", json.dumps(pointer, indent=2, sort_keys=True))


def _save_state(active_dir: Path, state: Dict[str, Any]) -> None:
    _atomic_write(active_dir / "scheduler_state.json", json.dumps(state, indent=2, sort_keys=True))


def _load_state(active_dir: Path) -> Dict[str, Any]:
    path = active_dir / "scheduler_state.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# One scheduler tick
# ---------------------------------------------------------------------------
def run_once(
    base_config: Dict[str, Any],
    wf: Dict[str, Any],
    args: argparse.Namespace,
    today: Optional[str] = None,
) -> int:
    """Compute/publish the current month's config. Returns an exit code (0 = ok)."""
    anchor_start = str(wf["start_date"] or base_config["backtest"]["start_date"])
    train_months = int(wf["train_months"])
    test_months = int(wf["test_months"])
    step_months = int(wf["step_months"])
    calendar_months = bool(wf["calendar_months"])
    base_seed = int(wf["base_seed"])
    scoring_keys = list(base_config.get("optimize", {}).get("scoring", []))

    today = today or ts_to_date(utc_ms())[:10]
    current = current_live_window(
        anchor_start, train_months, test_months, step_months, today, calendar_months
    )
    if current is None:
        logger.info(
            "No live window yet: today=%s precedes first test period "
            "(anchor=%s + %dmo train). Live bot should use the initial config.",
            today, anchor_start, train_months,
        )
        return 0

    active_dir = Path(_abspath(wf["live_rolling"].get("active_dir", "runs/walkforward/live")))
    cache_dir: Optional[Path] = None
    if not args.no_cache:
        cache_dir = Path(_abspath(
            wf["live_rolling"].get("cache_dir") or os.path.join("runs", "walkforward", "_cache")
        ))
        cache_dir.mkdir(parents=True, exist_ok=True)

    # All windows from the anchor up to and including the current period. Window
    # generation is pure date math (no data), so building the chain is cheap.
    windows = generate_windows(
        start_date=anchor_start,
        end_date=current.test_end,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        calendar_months=calendar_months,
        min_test_days=int(wf["min_test_days"]),
    )
    if not windows:
        logger.error("No windows generated up to %s (anchor=%s)", current.test_end, anchor_start)
        return 1

    logger.info(
        "today=%s | current live window #%d | train %s->%s | live %s->%s | replaying %d window(s)",
        today, current.index, current.train_start, current.train_end,
        current.test_start, current.test_end, len(windows),
    )

    if args.dry_run:
        for w in windows:
            logger.info("  window %02d | train %s->%s | live %s->%s",
                        w.index, w.train_start, w.train_end, w.test_start, w.test_end)
        return 0

    work_dir = active_dir / "_work"
    # Trade-count overfit guard along the chain (same rule the backtest uses): reject a
    # window's chosen config if its in-sample trade rate dropped below min_trade_ratio of
    # the previous window's chosen config (walks down the Pareto front to the next-best).
    min_trade_ratio = float(wf.get("min_trade_ratio", 0.0) or 0.0)
    prev_trade_rate: Optional[float] = None
    prev_config_path: Optional[str] = None
    last_choice = None
    last_window = None
    last_cache_key = None
    for w in windows:
        seed = base_seed + w.index
        wdir = work_dir / f"window_{w.index:02d}"

        if w.index == 0:
            # Seed window: deploy the hand-tuned initial config as-is (no optimization).
            # Optimization begins at window 1, warm-started from this config. Mirrors the
            # backtest orchestrator (walkforward.run) so live and backtest stay in parity.
            choice = seed_choice_from_config(_abspath(wf["initial_config"]))
            cache_key, cache_hit = None, False
            trade_guard = {"applied": False, "seeded": True}
            logger.info("window 00 | seed: initial config as-is (no optimization)")
        else:
            warm_start = prev_config_path or _abspath(wf["initial_config"])
            if not os.path.exists(warm_start):
                logger.warning("Warm-start config not found: %s (continuing without)", warm_start)
                warm_start = None
            proximity_reference = prev_config_path

            try:
                candidates, cache_key, cache_hit = optimize_one_window(
                    base_config, w,
                    seed=seed,
                    stop_cfg=wf["stop"],
                    proximity_weight=float(wf["proximity_weight"]),
                    proximity_reference=proximity_reference,
                    warm_start=warm_start,
                    scoring_keys=scoring_keys,
                    train_cfg_path=str(wdir / "train_config.json"),
                    results_dir=str(wdir / "optimize_results"),
                    log_path=str(wdir / "optimize.log"),
                    iters=args.iters,
                    n_cpus=args.n_cpus,
                    cache_dir=cache_dir,
                    no_cache=args.no_cache,
                )
            except WindowOptimizeError as exc:
                logger.error("window %02d | %s", w.index, exc)
                return exc.code

            choice, trade_guard = select_with_trade_guard(candidates, prev_trade_rate, min_trade_ratio)
            if trade_guard.get("no_pass"):
                logger.warning("window %02d | trade-guard: no candidate >= %.2f x prev rate; "
                               "using highest-rate (rank %d)", w.index, min_trade_ratio,
                               trade_guard.get("chosen_rank", 0))
            elif trade_guard.get("applied") and trade_guard.get("chosen_rank"):
                logger.warning("window %02d | trade-guard: rejected %d higher-ranked candidate(s); "
                               "chose rank %d", w.index, len(trade_guard.get("rejected", [])),
                               trade_guard.get("chosen_rank", 0))

        # Persist this window's chosen config so the NEXT window warm-starts from it
        # (the deterministic chain → stable cache keys for already-done windows).
        chain_path = wdir / "chosen.json"
        chain_path.parent.mkdir(parents=True, exist_ok=True)
        dump_config(choice.config, str(chain_path))
        prev_config_path = str(chain_path)
        rate = pareto_trade_rate(choice)
        if rate is not None:
            prev_trade_rate = rate
        last_choice, last_window, last_cache_key = choice, w, cache_key
        _status = "seed" if trade_guard.get("seeded") else ("cache HIT" if cache_hit else "optimized")
        logger.info("window %02d | %s | chosen=%s | trade_rate=%s",
                    w.index, _status, choice.hash_id,
                    f"{rate:.4f}" if rate is not None else "n/a")

    # Publish the current (last) window as the active live config.
    pointer = {
        "period": f"{last_window.test_start}..{last_window.test_end}",
        "window_index": last_window.index,
        "train_window": last_window.to_dict(),
        # Relative, co-located name (not an absolute path) so artifacts generated on
        # one machine (e.g. Windows) are consumable on another (e.g. a Linux VPS).
        "config_path": "active_config.json",
        "cache_key": last_cache_key,
        "chosen_hash": last_choice.hash_id,
        "anchor_start": anchor_start,
        "generated_ts": int(utc_ms()),
        "params": {
            "train_months": train_months,
            "test_months": test_months,
            "step_months": step_months,
            "base_seed": base_seed,
            "calendar_months": calendar_months,
            "proximity_weight": float(wf["proximity_weight"]),
            # Single source of truth for the live month-boundary handoff threshold:
            # meta file -> scheduler -> active.json -> bot (see passivbot._wfo_wind_down).
            "max_loss_flatten_frac": float(wf["max_loss_flatten_frac"]),
        },
    }
    _publish(active_dir, last_choice.config, pointer)
    _save_state(active_dir, {
        "last_published_period": pointer["period"],
        "last_window_index": last_window.index,
        "last_cache_key": last_cache_key,
        "last_tick_ts": int(utc_ms()),
        "today": today,
    })
    logger.info("Published active config for window #%d (%s) -> %s",
                last_window.index, pointer["period"], active_dir / "active.json")
    return 0


def run(args: argparse.Namespace) -> int:
    if bool(args.meta) == bool(args.config):
        logger.error("Provide exactly one of --meta (stand-alone meta file) or --config "
                     "(config with an embedded walk_forward block).")
        return 1

    if args.meta:
        wf, config_path = load_wf_meta(args.meta)
        wf = resolve_params(wf, args)  # CLI overrides on top (re-merge is idempotent)
    else:
        config_path = _abspath(args.config)
        raw = load_hjson_config(config_path)
        wf_block = raw.get("walk_forward", {}) if isinstance(raw, dict) else {}
        wf = resolve_params(wf_block, args)
    base_config = load_config(config_path, verbose=False)

    if args.once or args.dry_run:
        return run_once(base_config, wf, args)

    interval_min = float(wf["live_rolling"].get("check_interval_minutes", 60.0) or 60.0)
    logger.info("Scheduler loop started (check every %.0f min). Ctrl-C to stop.", interval_min)
    while True:
        try:
            rc = run_once(base_config, wf, args)
            if rc != 0:
                logger.warning("Tick returned rc=%d; will retry next interval", rc)
        except Exception as exc:  # keep the scheduler alive across transient errors
            logger.exception("Scheduler tick failed: %s", exc)
        time.sleep(max(60.0, interval_min * 60.0))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wfo_scheduler", description="Walk-forward live scheduler")
    p.add_argument("--meta", type=str, default=None,
                   help="Stand-alone walk-forward meta file (configs/wfo_meta.json)")
    p.add_argument("--config", default=None,
                   help="WFO config with an embedded walk_forward block (legacy)")
    p.add_argument("--once", action="store_true", help="Run a single tick and exit")
    p.add_argument("--dry-run", action="store_true", help="Print the current window plan and exit")
    p.add_argument("--initial-config", type=str, default=None,
                   help="Override the window-0 warm-start config")
    p.add_argument("--iters", type=int, default=None, help="Override optimize.iters per window")
    p.add_argument("--n-cpus", type=int, default=None, help="Override optimize.n_cpus per window")
    p.add_argument("--check-interval-minutes", type=float, default=None,
                   help="Override loop check interval")
    p.add_argument("--cache-dir", type=str, default=None,
                   help="Shared optimization cache dir (default runs/walkforward/_cache)")
    p.add_argument("--no-cache", action="store_true", help="Disable the optimization cache")
    p.add_argument("--log-level", dest="log_level", default="info")
    return p


def main() -> int:
    args = build_parser().parse_args()
    level_map = {"warning": 0, "info": 1, "debug": 2, "trace": 3}
    configure_logging(debug=level_map.get(str(args.log_level).lower(), 1))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
