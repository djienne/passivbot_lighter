#!/usr/bin/env python3
"""Extend a completed stateful WFO run's FINAL window to a later ``--end-date`` and
re-stitch the full equity curve. No retrain: only the final window's OOS backtest
is re-run with the later end_date; every earlier window is value-continuous and
reused as-is. Writes an archive JSON + an equity/drawdown PNG to
``runs/walkforward/_comparison``.

This is the repeatable form of the "bring the final OOS window up to today" chore
(use it instead of hand-writing a one-off extend script per run/date).

Usage:
    python src/tools/wfo_extend_restitch.py <run_id> --end-date 2026-06-09 [--label t12]

``--end-date`` is exclusive (e.g. 2026-06-09 includes through June 8). Requires the
underlying 1m data to already cover the new range.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "runs" / "walkforward" / "_comparison"


def load_seg(bal_eq_path: str) -> pd.DataFrame:
    df = pd.read_csv(bal_eq_path)
    return pd.DataFrame({
        "ts": pd.to_datetime(df["Unnamed: 0"]),
        "equity": df["usd_total_equity"].astype(float).values,
        "balance": df["usd_total_balance"].astype(float).values,
    })


def newest_bt(win_dir: str) -> str | None:
    g = glob.glob(os.path.join(win_dir, "test", "bt", "**", "balance_and_equity.csv.gz"), recursive=True)
    return sorted(g, key=os.path.getmtime)[-1] if g else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id", help="walk-forward run dir name under runs/walkforward/")
    ap.add_argument("--end-date", required=True, help="new (exclusive) end_date, e.g. 2026-06-09")
    ap.add_argument("--label", default=None, help="display label (default = run_id)")
    args = ap.parse_args()

    new_end = args.end_date
    label = args.label or args.run_id
    suffix = "ext" + new_end.replace("-", "")[2:]  # 2026-06-09 -> ext260609
    run_dir = ROOT / "runs" / "walkforward" / args.run_id
    if not run_dir.is_dir():
        print(f"run dir not found: {run_dir}", file=sys.stderr)
        return 1

    win_dirs = sorted(d for d in glob.glob(os.path.join(run_dir, "window_*"))
                      if os.path.isdir(d) and os.path.basename(d)[7:].isdigit())
    if not win_dirs:
        print(f"no window_* dirs in {run_dir}", file=sys.stderr)
        return 1
    final_idx = len(win_dirs) - 1
    print(f"== {args.run_id}  ({len(win_dirs)} windows, final = window_{final_idx:02d}) ==")

    # --- 1. extend the final window's OOS to new_end ---
    final_dir = win_dirs[final_idx]
    src_cfg = os.path.join(final_dir, "test", "test_config.json")
    ext_cfg = os.path.join(final_dir, "test", f"test_config_{suffix}.json")
    with open(src_cfg, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    old_end = cfg["backtest"]["end_date"]
    cfg["backtest"]["end_date"] = new_end
    with open(ext_cfg, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"final window end_date {old_end} -> {new_end}; running OOS backtest...")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONHASHSEED="0")
    r = subprocess.run([sys.executable, str(ROOT / "src" / "backtest.py"),
                        ext_cfg, "-dp", "--skip-rust-compile"],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print("BACKTEST FAILED:\n", r.stdout[-2000:], r.stderr[-2000:], file=sys.stderr)
        return 1

    final_segs = glob.glob(os.path.join(final_dir, "test", "bt", "**",
                                         "balance_and_equity.csv.gz"), recursive=True)
    ext_path = max(final_segs, key=lambda p: load_seg(p)["ts"].iloc[-1])

    # --- 2. gather all window segments (final = extended) ---
    segs, rows = [], []
    for i, wd in enumerate(win_dirs):
        path = ext_path if i == final_idx else newest_bt(wd)
        seg = load_seg(path)
        with open(os.path.join(wd, "window_summary.json"), "r", encoding="utf-8") as fh:
            ws = json.load(fh)
        seeded = bool(ws.get("seeded") or (ws.get("trade_guard") or {}).get("seeded"))
        s_eq, e_eq = seg["equity"].iloc[0], seg["equity"].iloc[-1]
        dd = (seg["equity"] / seg["equity"].cummax() - 1.0) * 100.0
        rows.append({"index": i, "test_start": ws["window"]["test_start"],
                     "test_end": ws["window"]["test_end"] if i != final_idx else new_end,
                     "seeded": seeded, "seg_start_eq": float(s_eq), "seg_end_eq": float(e_eq),
                     "seg_gain_pct": float((e_eq / s_eq - 1) * 100), "seg_maxdd_pct": float(dd.min()),
                     "seg_end_balance": float(seg["balance"].iloc[-1])})
        segs.append(seg)

    full = pd.concat(segs, ignore_index=True).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    dd = (full["equity"] / full["equity"].cummax() - 1.0) * 100.0
    start_eq, end_eq = full["equity"].iloc[0], full["equity"].iloc[-1]
    end_bal = full["balance"].iloc[-1]
    total_eq = (end_eq / start_eq - 1) * 100
    total_bal = (end_bal / start_eq - 1) * 100
    max_dd = dd.min()

    print(f"\nstateful equity: ${start_eq:.2f} -> ${end_eq:.2f}  ({total_eq:+.2f}%)   "
          f"realized balance ${end_bal:.2f} ({total_bal:+.2f}%)   stitched maxDD {max_dd:.2f}%")
    for r_ in rows:
        tag = " [seed]" if r_["seeded"] else ""
        print(f"  w{r_['index']:02d} {r_['test_start'][:10]}->{r_['test_end'][:10]}  "
              f"gain={r_['seg_gain_pct']:+.2f}%  maxDD={r_['seg_maxdd_pct']:.2f}%{tag}")

    # --- 3. write archive json ---
    archive = {"run_id": args.run_id, "label": label, "end_date": new_end,
               "n_windows": len(win_dirs), "stateful_equity_total_pct": total_eq,
               "realized_balance_total_pct": total_bal, "final_equity": float(end_eq),
               "final_balance": float(end_bal), "stitched_maxdd_pct": float(max_dd),
               "windows": rows}
    COMP.mkdir(parents=True, exist_ok=True)
    arch_path = COMP / f"{args.run_id}_{suffix}.json"
    with open(arch_path, "w", encoding="utf-8") as fh:
        json.dump(archive, fh, indent=2)
    print(f"saved archive -> {arch_path}")

    # --- 4. plot (matplotlib wants numpy arrays, not pandas Series) ---
    ts_arr = full["ts"].to_numpy()
    eq_arr = full["equity"].to_numpy()
    bal_arr = full["balance"].to_numpy()
    dd_arr = dd.to_numpy()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(ts_arr, eq_arr, color="#1565c0", lw=1.2, label="Equity (mark-to-market)")
    ax1.plot(ts_arr, bal_arr, color="#2e7d32", lw=0.8, ls="--", alpha=0.7,
             label="Balance (realized)")
    ax1.set_ylabel("USD (start = $100)")
    ax1.set_title(f"WFO {label} — STATEFUL equity through {new_end} (exclusive)")
    ax1.grid(alpha=0.3)
    for r_, seg in zip(rows, segs):
        ts0 = seg["ts"].iloc[0]
        if r_["seeded"]:
            ts_end = segs[r_["index"] + 1]["ts"].iloc[0] if r_["index"] + 1 < len(segs) else full["ts"].iloc[-1]
            ax1.axvspan(ts0, ts_end, color="orange", alpha=0.12, zorder=0)
        ax1.axvline(ts0, color="grey", ls=":", lw=0.7, alpha=0.6)
    ax1.legend(loc="upper left")
    ax1.text(0.99, 0.02,
             f"Equity {total_eq:+.1f}% (${end_eq:.0f})\nBalance {total_bal:+.1f}% (${end_bal:.0f})\n"
             f"Stitched max DD {max_dd:.1f}%",
             transform=ax1.transAxes, ha="right", va="bottom", fontsize=10,
             bbox=dict(boxstyle="round", fc="white", ec="grey", alpha=0.85))
    ax2.fill_between(ts_arr, dd_arr, 0, color="#c62828", alpha=0.4)
    ax2.plot(ts_arr, dd_arr, color="#c62828", lw=0.7)
    ax2.set_ylabel("Drawdown\nfrom peak (%)")
    ax2.set_xlabel("Date")
    ax2.grid(alpha=0.3)
    for seg in segs:
        ax2.axvline(seg["ts"].iloc[0], color="grey", ls=":", lw=0.7, alpha=0.6)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    png = COMP / f"{args.run_id}_{suffix}_equity.png"
    fig.savefig(png, dpi=120)
    print(f"saved plot -> {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
