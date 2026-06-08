#!/usr/bin/env python3
"""Peek at per-window stateful EQUITY for a walk-forward run (in-progress or done).

Reads each window's OOS ``balance_and_equity.csv.gz`` and reports the honest
mark-to-market equity (start / end / min / max + intra-window drawdown), NOT the
realized 'balance' (which hides carried-position uPnL). Safe to run mid-flight:
windows still optimizing simply print "(no OOS yet)".

Usage:
    python src/tools/wfo_peek_equity.py <run_dir>      # defaults to cwd
"""
from __future__ import annotations

import glob
import gzip
import os
import sys

import pandas as pd


def find_be(test_dir: str) -> str | None:
    hits = glob.glob(os.path.join(test_dir, "bt", "**", "balance_and_equity.csv.gz"), recursive=True)
    return max(hits, key=os.path.getmtime) if hits else None


def eq_col(df: pd.DataFrame) -> str:
    for c in ("usd_total_equity", "equity", "usd_total_balance"):
        if c in df.columns:
            return c
    return df.columns[-1]


def peek(run_dir: str) -> None:
    print(f"== {os.path.abspath(run_dir)} ==")
    print(f"{'win':>3} {'eq_start':>10} {'eq_end':>10} {'eq_min':>10} {'eq_max':>10} {'ddown%':>8} {'mret%':>8}")
    for w in sorted(glob.glob(os.path.join(run_dir, "window_*"))):
        idx = os.path.basename(w).split("_")[-1]
        be = find_be(os.path.join(w, "test"))
        if not be:
            print(f"{idx:>3}  (no OOS yet / optimizing)")
            continue
        with gzip.open(be, "rt") as fh:
            df = pd.read_csv(fh)
        col = eq_col(df)
        eq = df[col].astype(float)
        if len(eq) == 0:
            print(f"{idx:>3}  (empty equity)")
            continue
        e0, e1, emin, emax = eq.iloc[0], eq.iloc[-1], eq.min(), eq.max()
        ddown = ((eq - eq.cummax()) / eq.cummax()).min() * 100.0
        mret = (e1 / e0 - 1.0) * 100.0 if e0 else float("nan")
        print(f"{idx:>3} {e0:>10.3f} {e1:>10.3f} {emin:>10.3f} {emax:>10.3f} {ddown:>8.2f} {mret:>8.2f}  [{col}]")


if __name__ == "__main__":
    peek(sys.argv[1] if len(sys.argv) > 1 else ".")
