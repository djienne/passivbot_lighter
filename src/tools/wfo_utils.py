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


def select_best_from_pareto(
    pareto_dir: str,
    scoring_keys: Optional[Sequence[str]] = None,
) -> Optional[ParetoChoice]:
    """Deterministically pick the single best config from a Pareto front.

    Strategy: among feasible members (constraint_violation == 0 when any exist),
    min-max normalize each objective column to [0, 1] and pick the member with the
    smallest Euclidean distance to the component-wise ideal (all-minimum) point.
    Ties broken by (violation, distance, hash_id). Files are read in sorted order
    so the result is reproducible.

    Returns ``None`` if the Pareto directory has no usable entries.
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
        return None

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
    violation, distance, hash_id, objectives, entry = scored[0]
    return ParetoChoice(
        hash_id=hash_id,
        config=_strip_metrics(entry),
        objectives=objectives,
        violation=violation,
        distance=distance,
        metrics=(entry.get("metrics") or {}),
        n_candidates=len(parsed),
    )


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
) -> Dict[str, Any]:
    """Splice consecutive OOS equity segments into one continuous curve.

    Each segment is either a 1-D sequence of equity values, or a mapping with
    ``timestamps`` and ``equity`` sequences. Timestamped segments are sorted and
    duplicate timestamps are skipped, preserving the return from the last
    overlapping point into the first new point. Untimestamped segments keep the
    historical behavior: each segment's return is chained onto the running
    equity.

    Returns the stitched curve plus aggregate metrics derived from it.
    """
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
