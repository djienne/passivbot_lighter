#!/usr/bin/env python3
"""Pure, dependency-light helpers for walk-forward optimization (WFO).

These functions contain no subprocess / data-loading side effects so they can be
unit tested directly:

- ``generate_windows``       roll train/test date windows across a history span
- ``select_best_from_pareto`` pick one config from an optimizer Pareto front
- ``normalized_distance``    scale-invariant distance between two param vectors
- ``param_drift``            per-parameter drift between two configs
- ``stitch_oos_equity``      splice consecutive OOS equity segments into one curve
"""

from __future__ import annotations

import glob
import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dateutil.relativedelta import relativedelta

# pareto_core lives one level up in src/; importers add src/ to sys.path.
try:
    from pareto_core import extract_objectives, extract_violation
except Exception:  # pragma: no cover - fallback when imported as a package
    from src.pareto_core import extract_objectives, extract_violation  # type: ignore


# ---------------------------------------------------------------------------
# Window generation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Window:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _advance(start: date, months: int, calendar_months: bool) -> date:
    if calendar_months:
        return start + relativedelta(months=months)
    return start + timedelta(days=30 * months)


def generate_windows(
    start_date: str,
    end_date: str,
    train_months: int = 6,
    test_months: int = 1,
    step_months: int = 1,
    calendar_months: bool = True,
    min_test_days: int = 7,
) -> List[Window]:
    """Generate rolling train/test windows.

    Each window trains on ``[train_start, train_end)`` and tests out-of-sample on
    ``[test_start=train_end, test_end)``. The window rolls forward by
    ``step_months`` (default == test length => non-overlapping OOS segments).

    The training window is always full length; a window is dropped once the
    training window would extend past ``end_date``. The final OOS test window is
    clamped to ``end_date`` and dropped if shorter than ``min_test_days``.
    """
    if train_months <= 0:
        raise ValueError("train_months must be positive")
    if test_months <= 0:
        raise ValueError("test_months must be positive")
    if step_months <= 0:
        raise ValueError("step_months must be positive")

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    windows: List[Window] = []
    idx = 0
    train_start = start
    while True:
        train_end = _advance(train_start, train_months, calendar_months)
        if train_end > end:
            break  # not enough data for a full training window
        test_start = train_end
        if test_start >= end:
            break  # no out-of-sample data left
        test_end = _advance(test_start, test_months, calendar_months)
        if test_end > end:
            test_end = end
        if (test_end - test_start).days < min_test_days:
            break  # trailing partial OOS too short to be meaningful
        windows.append(
            Window(
                index=idx,
                train_start=train_start.isoformat(),
                train_end=train_end.isoformat(),
                test_start=test_start.isoformat(),
                test_end=test_end.isoformat(),
            )
        )
        idx += 1
        train_start = _advance(train_start, step_months, calendar_months)
    return windows


# ---------------------------------------------------------------------------
# Pareto front -> single config selection
# ---------------------------------------------------------------------------
def _strip_metrics(entry: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(entry)
    cfg.pop("metrics", None)
    cfg.pop("suite_metrics", None)
    return cfg


@dataclass
class ParetoChoice:
    hash_id: str
    config: Dict[str, Any]
    objectives: Tuple[float, ...]
    violation: float
    distance: float
    metrics: Dict[str, Any]
    n_candidates: int


def rank_pareto_candidates(
    pareto_dir: str,
    scoring_keys: Optional[Sequence[str]] = None,
) -> List[ParetoChoice]:
    """Deterministically rank all configs on a Pareto front, best first.

    Strategy: among feasible members (constraint_violation == 0 when any exist),
    min-max normalize each objective column to [0, 1] and order by Euclidean
    distance to the component-wise ideal (all-minimum) point. Ties broken by
    (violation, distance, hash_id). Files are read in sorted order so the order is
    reproducible. ``ranked[0]`` is the single best (what ``select_best_from_pareto``
    returns); later entries are the fall-back candidates used by the trade-count
    overfit guard (see :func:`select_with_trade_guard`).

    Returns ``[]`` if the Pareto directory has no usable entries. Every returned
    ``ParetoChoice`` carries ``n_candidates`` = the total parsed (front size).
    """
    paths = sorted(glob.glob(os.path.join(pareto_dir, "*.json")))
    parsed: List[Tuple[str, Tuple[float, ...], float, Dict[str, Any]]] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except Exception:
            continue
        objectives, _ = extract_objectives(entry, scoring_keys=scoring_keys)
        if not objectives or any(o is None for o in objectives):
            continue
        try:
            objectives = tuple(float(o) for o in objectives)
        except (TypeError, ValueError):
            continue
        if any(not math.isfinite(o) for o in objectives):
            continue
        violation = extract_violation(entry)
        hash_id = os.path.splitext(os.path.basename(path))[0]
        parsed.append((hash_id, objectives, violation, entry))

    if not parsed:
        return []

    feasible = [p for p in parsed if p[2] <= 0.0]
    pool = feasible if feasible else parsed

    n_obj = len(pool[0][1])
    mins = [min(p[1][j] for p in pool) for j in range(n_obj)]
    maxs = [max(p[1][j] for p in pool) for j in range(n_obj)]
    ranges = [(maxs[j] - mins[j]) for j in range(n_obj)]

    scored = []
    for hash_id, objectives, violation, entry in pool:
        dist_sq = 0.0
        for j in range(n_obj):
            if ranges[j] > 0:
                dist_sq += ((objectives[j] - mins[j]) / ranges[j]) ** 2
        distance = dist_sq ** 0.5
        scored.append((violation, distance, hash_id, objectives, entry))

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    n_candidates = len(parsed)
    return [
        ParetoChoice(
            hash_id=hash_id,
            config=_strip_metrics(entry),
            objectives=objectives,
            violation=violation,
            distance=distance,
            metrics=(entry.get("metrics") or {}),
            n_candidates=n_candidates,
        )
        for violation, distance, hash_id, objectives, entry in scored
    ]


def select_best_from_pareto(
    pareto_dir: str,
    scoring_keys: Optional[Sequence[str]] = None,
) -> Optional[ParetoChoice]:
    """Deterministically pick the single best config from a Pareto front.

    Thin wrapper over :func:`rank_pareto_candidates` returning the top-ranked
    candidate (or ``None`` if the front has no usable entries).
    """
    ranked = rank_pareto_candidates(pareto_dir, scoring_keys)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# Trade-count overfit guard
# ---------------------------------------------------------------------------
def pareto_trade_rate(choice: "ParetoChoice") -> Optional[float]:
    """In-sample trade frequency of a candidate as positions held per day.

    Reads ``metrics.stats.positions_held_per_day.mean`` (the only per-candidate
    trade-frequency metric the optimizer emits), falling back to the weighted
    ``positions_held_per_day_w`` variant, else ``None`` when neither is present.
    A per-day rate is directly comparable across windows of different lengths.
    """
    stats = (getattr(choice, "metrics", {}) or {}).get("stats", {}) or {}
    for key in ("positions_held_per_day", "positions_held_per_day_w"):
        entry = stats.get(key)
        if isinstance(entry, dict) and entry.get("mean") is not None:
            try:
                return float(entry["mean"])
            except (TypeError, ValueError):
                continue
        elif isinstance(entry, (int, float)):
            return float(entry)
    return None


def select_with_trade_guard(
    candidates: Sequence["ParetoChoice"],
    prev_trade_rate: Optional[float],
    min_trade_ratio: float = 0.5,
) -> Tuple["ParetoChoice", Dict[str, Any]]:
    """Pick a window's config, rejecting candidates that trade far less than last month.

    Walks the ranked ``candidates`` (best first) and returns the first whose in-sample
    trade rate is at least ``min_trade_ratio`` of the previous window's chosen config's
    rate — an overfit guard against configs that profit from only a few trades.

    - ``prev_trade_rate`` falsy/≤0 (window 0, or unknown) or ``min_trade_ratio`` ≤0
      disables the guard: the top-ranked candidate is returned unchanged.
    - If no candidate clears the threshold, the candidate with the **highest** trade
      rate is returned (closest to passing), flagged ``no_pass``.

    Returns ``(choice, info)`` where ``info`` records the decision for auditing
    (``window_summary.json``).
    """
    candidates = list(candidates)
    if not candidates:
        raise ValueError("select_with_trade_guard called with no candidates")

    # Trade rate per candidate, computed once and reused by both the walk below and the
    # no-pass fallback (avoids recomputing pareto_trade_rate for the same candidate).
    rates = [pareto_trade_rate(c) for c in candidates]

    top = candidates[0]
    if not prev_trade_rate or prev_trade_rate <= 0 or min_trade_ratio <= 0:
        return top, {
            "applied": False,
            "prev_trade_rate": prev_trade_rate,
            "chosen_rank": 0,
            "chosen_trade_rate": rates[0],
        }

    threshold = float(min_trade_ratio) * float(prev_trade_rate)
    rejected: List[Dict[str, Any]] = []
    for rank, cand in enumerate(candidates):
        rate = rates[rank]
        # A candidate with no trade-rate metric cannot be judged; accept it (rank order).
        if rate is None or rate >= threshold:
            return cand, {
                "applied": True,
                "prev_trade_rate": float(prev_trade_rate),
                "threshold": threshold,
                "min_trade_ratio": float(min_trade_ratio),
                "chosen_rank": rank,
                "chosen_trade_rate": rate,
                "rejected": rejected,
            }
        rejected.append({"rank": rank, "hash_id": cand.hash_id, "trade_rate": rate})

    # No candidate clears the threshold: fall back to the highest-trade-rate one
    # (ties broken toward the better-ranked / lower-index candidate).
    best_rank = max(range(len(candidates)), key=lambda i: (rates[i] or 0.0, -i))
    best_choice = candidates[best_rank]
    return best_choice, {
        "applied": True,
        "no_pass": True,
        "fallback": "highest_trade_rate",
        "prev_trade_rate": float(prev_trade_rate),
        "threshold": threshold,
        "min_trade_ratio": float(min_trade_ratio),
        "chosen_rank": best_rank,
        "chosen_trade_rate": rates[best_rank],
        "rejected": rejected,
    }


# ---------------------------------------------------------------------------
# Parameter drift
# ---------------------------------------------------------------------------
def _flatten_bot(config: Dict[str, Any]) -> Dict[str, float]:
    flat: Dict[str, float] = {}
    bot = config.get("bot", {}) or {}
    for pside in sorted(bot):
        side = bot[pside] or {}
        for key in sorted(side):
            val = side[key]
            if isinstance(val, bool):
                continue
            if isinstance(val, (int, float)):
                flat[f"{pside}.{key}"] = float(val)
    return flat


def param_drift(
    prev_config: Dict[str, Any],
    cur_config: Dict[str, Any],
    bounds_ranges: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Per-parameter absolute change between two configs.

    When ``bounds_ranges`` (param name -> (high-low)) is provided, also reports a
    normalized drift so changes are comparable across parameters with different
    scales, plus an overall L2 norm of the normalized drift vector.
    """
    prev_flat = _flatten_bot(prev_config)
    cur_flat = _flatten_bot(cur_config)
    keys = sorted(set(prev_flat) | set(cur_flat))
    per_param: Dict[str, Dict[str, float]] = {}
    norm_sq = 0.0
    for key in keys:
        prev_val = prev_flat.get(key)
        cur_val = cur_flat.get(key)
        if prev_val is None or cur_val is None:
            continue
        delta = cur_val - prev_val
        record = {"previous": prev_val, "current": cur_val, "delta": delta}
        if bounds_ranges and key in bounds_ranges and bounds_ranges[key] > 0:
            norm = abs(delta) / bounds_ranges[key]
            record["normalized"] = norm
            norm_sq += norm * norm
        per_param[key] = record
    return {"per_param": per_param, "l2_normalized": norm_sq ** 0.5}


def normalized_distance(
    vec_a: Sequence[float],
    vec_b: Sequence[float],
    ranges: Sequence[float],
) -> float:
    """RMS of per-component distance, each normalized by its range."""
    total = 0.0
    count = 0
    for a, b, rng in zip(vec_a, vec_b, ranges):
        if rng and rng > 0:
            total += ((a - b) / rng) ** 2
            count += 1
    if count == 0:
        return 0.0
    return (total / count) ** 0.5


# ---------------------------------------------------------------------------
# OOS equity stitching
# ---------------------------------------------------------------------------
def stitch_oos_equity(
    segments: Sequence[Any],
    starting_balance: float = 1.0,
    stateful: bool = False,
) -> Dict[str, Any]:
    """Splice consecutive OOS equity segments into one continuous curve.

    Each segment is either a 1-D sequence of equity values, or a mapping with
    ``timestamps`` and ``equity`` sequences. Timestamped segments are sorted and
    duplicate timestamps are skipped, preserving the return from the last
    overlapping point into the first new point. Untimestamped segments keep the
    historical behavior: each segment's return is chained onto the running
    equity.

    When ``stateful=True`` the segments are already value-continuous (each backtest
    started from the previous segment's carried balance + open positions), so their
    raw equity values are simply **concatenated** (deduping overlapping timestamps)
    rather than rebased — see :func:`tools.wfo_handoff.advance_carry`.

    Returns the stitched curve plus aggregate metrics derived from it.
    """
    if stateful:
        return _concat_oos_equity(segments, starting_balance)
    stitched: List[float] = []
    timestamps: List[Any] = []
    timestamp_to_equity: Dict[Any, float] = {}
    seen_timestamps = set()
    running = float(starting_balance)
    segment_returns: List[float] = []
    for seg in segments:
        points = _normalise_oos_segment(seg)
        if len(points) < 2:
            continue
        if points[0][0] is None:
            values = [value for _, value in points]
            if values[0] == 0:
                continue
            base = values[0]
            start_running = running
            for value in values:
                stitched.append(start_running * (value / base))
            seg_ret = values[-1] / base
            segment_returns.append(seg_ret)
            running = start_running * seg_ret
            continue

        anchor_value: Optional[float] = None
        anchor_running: Optional[float] = None
        segment_start_running = running
        first_value = points[0][1]
        last_new_value: Optional[float] = None
        last_new_equity: Optional[float] = None
        for ts, value in points:
            if ts in seen_timestamps:
                anchor_value = value
                anchor_running = timestamp_to_equity[ts]
                continue
            if anchor_value is None:
                anchor_value = value
                anchor_running = running
            if anchor_value == 0:
                continue
            stitched_value = float(anchor_running) * (value / anchor_value)
            stitched.append(stitched_value)
            timestamps.append(ts)
            seen_timestamps.add(ts)
            timestamp_to_equity[ts] = stitched_value
            running = stitched_value
            last_new_value = value
            last_new_equity = stitched_value
        if last_new_value is not None and first_value != 0:
            segment_returns.append(last_new_value / first_value)
        elif last_new_equity is not None and segment_start_running != 0:
            segment_returns.append(last_new_equity / segment_start_running)
    metrics = _equity_metrics(stitched)
    metrics["segment_returns"] = segment_returns
    metrics["final_equity"] = stitched[-1] if stitched else float(starting_balance)
    metrics["starting_balance"] = float(starting_balance)
    result = {"equity": stitched, "metrics": metrics}
    if timestamps and len(timestamps) == len(stitched):
        result["timestamps"] = timestamps
    return result


def _concat_oos_equity(
    segments: Sequence[Any],
    starting_balance: float = 1.0,
) -> Dict[str, Any]:
    """Concatenate value-continuous OOS segments (stateful carry), deduping
    overlapping timestamps. No rebasing: the segments already share one balance line."""
    stitched: List[float] = []
    timestamps: List[Any] = []
    seen_timestamps = set()
    have_ts = True
    for seg in segments:
        for ts, value in _normalise_oos_segment(seg):
            if ts is None:
                have_ts = False
                stitched.append(value)
                continue
            if ts in seen_timestamps:
                continue
            seen_timestamps.add(ts)
            timestamps.append(ts)
            stitched.append(value)
    metrics = _equity_metrics(stitched)
    metrics["final_equity"] = stitched[-1] if stitched else float(starting_balance)
    metrics["starting_balance"] = float(starting_balance)
    result: Dict[str, Any] = {"equity": stitched, "metrics": metrics}
    if have_ts and timestamps and len(timestamps) == len(stitched):
        result["timestamps"] = timestamps
    return result


def _normalise_oos_segment(segment: Any) -> List[Tuple[Optional[Any], float]]:
    if isinstance(segment, dict):
        values = segment.get("equity") or segment.get("values") or []
        raw_timestamps = segment.get("timestamps") or segment.get("timestamp") or []
        if not raw_timestamps:
            points = []
            for value in values:
                try:
                    value_float = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value_float):
                    points.append((None, value_float))
            return points
        points = []
        for ts, value in zip(raw_timestamps, values):
            try:
                value_float = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value_float):
                points.append((ts, value_float))
        return sorted(points, key=lambda item: item[0])

    points: List[Tuple[Optional[Any], float]] = []
    for value in segment or []:
        try:
            value_float = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value_float):
            points.append((None, value_float))
    return points


def _equity_metrics(equity: Sequence[float]) -> Dict[str, Any]:
    if not equity or len(equity) < 2:
        return {
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "n_points": len(equity),
        }
    eq = [float(x) for x in equity]
    total_return = eq[-1] / eq[0] - 1.0
    peak = eq[0]
    max_dd = 0.0
    for value in eq:
        if value > peak:
            peak = value
        if peak > 0:
            dd = 1.0 - value / peak
            if dd > max_dd:
                max_dd = dd
    return {
        "total_return": total_return,
        "max_drawdown": max_dd,
        "n_points": len(eq),
    }
