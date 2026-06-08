#!/usr/bin/env python3
"""Extract per-window OOS results from a walk-forward run into the durable
``runs/walkforward/_comparison`` archive, so results survive even if the run dir
is later cleaned or overwritten.

For each <run_dir> it writes ``_comparison/<run_id>.json`` (idempotent, fully
overwritten each call) and merges a one-line pointer into ``_comparison/index.json``
(keyed by run_id). Safe to re-run mid-flight: it reads whatever ``window_NN/
window_summary.json`` files exist so far.

Usage:
    python src/tools/wfo_extract_oos.py <run_dir> [<run_dir> ...] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = REPO_ROOT / "runs" / "walkforward" / "_comparison"


def _load(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def extract(run_dir: str, out_dir: Path) -> None:
    run_dir = os.path.abspath(run_dir)
    run_id = os.path.basename(run_dir.rstrip("/\\"))
    out_dir.mkdir(parents=True, exist_ok=True)

    windows = []
    for wdir in sorted(glob.glob(os.path.join(run_dir, "window_*"))):
        summ = _load(os.path.join(wdir, "window_summary.json"))
        if not summ:
            continue
        oa = summ.get("oos_analysis") or {}
        w = summ.get("window") or {}
        gain = oa.get("gain_usd")
        windows.append({
            "index": summ.get("index"),
            "train_start": w.get("train_start"),
            "train_end": w.get("train_end"),
            "test_start": w.get("test_start"),
            "test_end": w.get("test_end"),
            "seeded": bool(summ.get("seeded")),
            "cache_hit": summ.get("cache_hit"),
            "trade_rate": summ.get("trade_rate"),
            "gain_usd": gain,
            "gain_pct": (gain - 1.0) * 100.0 if isinstance(gain, (int, float)) else None,
            "adg_w_usd": oa.get("adg_w_usd"),
            "sharpe_ratio_usd": oa.get("sharpe_ratio_usd"),
            "drawdown_worst_usd": oa.get("drawdown_worst_usd"),
            "win_rate": oa.get("win_rate"),
        })

    run_summ = _load(os.path.join(run_dir, "walkforward_summary", "walkforward_summary.json"))
    stitched_metrics = train_months = n_windows = None
    if run_summ:
        stitched_metrics = (run_summ.get("results") or {}).get("oos_stitched_metrics")
        train_months = (run_summ.get("params") or {}).get("train_months")
        n_windows = run_summ.get("n_windows")

    record = {
        "run_id": run_id,
        "run_dir": run_dir,
        "train_months": train_months,
        "n_windows": n_windows,
        "n_windows_done": len(windows),
        "stitched_metrics": stitched_metrics,
        "windows": windows,
    }
    out_path = out_dir / f"{run_id}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)

    index_path = out_dir / "index.json"
    index = _load(str(index_path)) or {}
    index[run_id] = {
        "train_months": train_months,
        "n_windows": n_windows,
        "n_windows_done": len(windows),
        "total_return": (stitched_metrics or {}).get("total_return") if stitched_metrics else None,
    }
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, sort_keys=True)

    tm = train_months if train_months is not None else "?"
    print(f"== {run_id}  (train_months={tm}, windows_done={len(windows)}) ==")
    for w in windows:
        seed = " [seed]" if w["seeded"] else ""
        gp = f"{w['gain_pct']:+.2f}%" if w["gain_pct"] is not None else "   n/a"
        print(f"  w{w['index']:02d} test {w['test_start']}->{w['test_end']}  {gp}{seed}")
    if stitched_metrics and isinstance(stitched_metrics.get("total_return"), (int, float)):
        tr = stitched_metrics["total_return"]
        print(f"  STITCHED total_return = {tr:+.4f} ({tr*100:+.2f}%)")
    print(f"  saved -> {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", nargs="+", help="walk-forward run dir(s)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help=f"archive dir (default {DEFAULT_OUT_DIR})")
    args = ap.parse_args()
    out_dir = Path(args.out_dir).resolve()
    for rd in args.run_dir:
        extract(rd, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
