"""
Interactive Pareto ranking tool.

Scans optimize_results/ for completed runs, lets you pick one,
then displays a nicely formatted ranking table sorted by distance
to the ideal point.

Usage:
    python rank_pareto.py                  # interactive folder picker
    python rank_pareto.py <run_folder>     # skip picker, go straight to ranking
    python rank_pareto.py --top 10         # show top 10 (default: 20)
    python rank_pareto.py --sort gain      # sort by a specific metric instead of distance
"""

import os
import sys
import json
import glob
import math
import argparse

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimize_results")


def discover_runs(results_dir):
    """Return list of (folder_name, n_pareto_files) for all runs with pareto results."""
    runs = []
    if not os.path.isdir(results_dir):
        return runs
    for name in sorted(os.listdir(results_dir)):
        pareto_dir = os.path.join(results_dir, name, "pareto")
        if os.path.isdir(pareto_dir):
            n = len(glob.glob(os.path.join(pareto_dir, "*.json")))
            if n > 0:
                runs.append((name, n))
    return runs


def parse_run_name(name):
    """Parse folder name like '2026-02-19T22_53_38_bybit_778days_HYPE_fec11929' into display parts."""
    # Timestamp is always YYYY-MM-DDTHH_MM_SS (20 chars)
    if len(name) >= 19 and name[4] == "-" and name[7] == "-" and name[10] == "T":
        date_part = name[:10]
        time_part = name[11:19].replace("_", ":")
        rest = name[20:] if len(name) > 20 else ""
        # Remove trailing hash (last _xxxxxxxx)
        rest_parts = rest.rsplit("_", 1)
        desc = rest_parts[0] if len(rest_parts) > 1 and len(rest_parts[1]) == 8 else rest
        return f"{date_part} {time_part}", desc
    return name, ""


def pick_run(runs):
    """Interactive terminal picker. Returns the selected folder name."""
    print("\n  Available optimization runs:\n")
    for i, (name, n) in enumerate(runs, 1):
        date_str, desc = parse_run_name(name)
        print(f"    [{i}]  {date_str}  {desc}  ({n} pareto configs)")
    print()
    while True:
        try:
            choice = input("  Select a run [1-{}]: ".format(len(runs))).strip()
            if not choice:
                continue
            idx = int(choice) - 1
            if 0 <= idx < len(runs):
                return runs[idx][0]
            print(f"    Please enter a number between 1 and {len(runs)}")
        except (ValueError, EOFError):
            print(f"    Please enter a number between 1 and {len(runs)}")


def load_pareto_entries(pareto_dir):
    """Load all pareto JSON files, return list of (filename, entry_dict)."""
    entries = []
    for fp in sorted(glob.glob(os.path.join(pareto_dir, "*.json"))):
        try:
            with open(fp) as f:
                entry = json.load(f)
            entries.append((os.path.basename(fp), entry))
        except Exception as e:
            print(f"  Warning: skipping {fp}: {e}")
    return entries


def compute_ranking(entries):
    """
    Compute normalized distance to ideal for each entry.
    Returns list of dicts with all relevant metrics + rank info.
    """
    if not entries:
        return [], [], []

    # Extract scoring keys and w_ values
    sample = entries[0][1]
    scoring_keys = sample.get("optimize", {}).get("scoring", [])
    w_keys = sorted(k for k in sample.get("analyses_combined", {}) if k.startswith("w_"))
    metric_name_map = {f"w_{i}": name for i, name in enumerate(scoring_keys)}

    # Build values matrix
    rows = []
    for filename, entry in entries:
        ac = entry.get("analyses_combined", {})
        vals = [ac.get(k, 0.0) for k in w_keys]
        rows.append((filename, entry, vals))

    if not rows:
        return [], [], []

    # Compute ideal point (min of each objective since values are negated)
    n_obj = len(w_keys)
    mins = [min(r[2][i] for r in rows) for i in range(n_obj)]
    maxs = [max(r[2][i] for r in rows) for i in range(n_obj)]

    # Normalize and compute distance to ideal (ideal = mins)
    ranked = []
    for filename, entry, vals in rows:
        norm = []
        for i in range(n_obj):
            if maxs[i] > mins[i]:
                norm.append((vals[i] - mins[i]) / (maxs[i] - mins[i]))
            else:
                norm.append(0.0)
        dist = math.sqrt(sum(v * v for v in norm))

        ac = entry.get("analyses_combined", {})
        analyses = {}
        # Flatten per-exchange analyses for display
        for exch_data in entry.get("analyses", {}).values():
            if isinstance(exch_data, dict):
                analyses = exch_data
                break

        ranked.append({
            "filename": filename,
            "dist": dist,
            "entry": entry,
            "objectives": {metric_name_map.get(w_keys[i], w_keys[i]): -vals[i] for i in range(n_obj)},
            "adg": analyses.get("adg", 0) * 100,
            "adg_w": analyses.get("adg_w", 0) * 100,
            "gain": analyses.get("gain", 0),
            "sharpe": analyses.get("sharpe_ratio", 0),
            "sharpe_w": analyses.get("sharpe_ratio_w", 0),
            "sortino": analyses.get("sortino_ratio", 0),
            "drawdown_worst": analyses.get("drawdown_worst", 0) * 100,
            "loss_profit_ratio": analyses.get("loss_profit_ratio", 0) * 100,
            "exposure_max": analyses.get("total_wallet_exposure_max", 0),
            "exposure_mean": analyses.get("total_wallet_exposure_mean", 0),
            "pos_per_day": analyses.get("positions_held_per_day", 0),
            "pos_hours_mean": analyses.get("position_held_hours_mean", 0),
            "pos_hours_max": analyses.get("position_held_hours_max", 0),
        })

    ranked.sort(key=lambda r: r["dist"])
    return ranked, scoring_keys, w_keys


def fmt(val, width=8, decimals=4):
    """Format a number to fixed width."""
    if isinstance(val, float):
        s = f"{val:.{decimals}f}"
    else:
        s = str(val)
    return s.rjust(width)


def print_ranking(ranked, scoring_keys, top_n=20, sort_by=None):
    """Print a nicely formatted ranking table."""
    if not ranked:
        print("  No pareto results found.")
        return

    if sort_by:
        key_map = {
            "dist": "dist", "distance": "dist",
            "adg": "adg", "adg_w": "adg_w",
            "gain": "gain",
            "sharpe": "sharpe", "sharpe_ratio": "sharpe", "sharpe_w": "sharpe_w",
            "sortino": "sortino",
            "drawdown": "drawdown_worst", "dd": "drawdown_worst",
            "loss": "loss_profit_ratio", "lpr": "loss_profit_ratio",
        }
        sort_key = key_map.get(sort_by, sort_by)
        if sort_key in ranked[0]:
            # For dist, drawdown, loss: lower is better (ascending)
            # For everything else: higher is better (descending)
            reverse = sort_key not in ("dist", "drawdown_worst", "loss_profit_ratio")
            ranked = sorted(ranked, key=lambda r: r.get(sort_key, 0), reverse=reverse)

    shown = ranked[:top_n]

    # Header
    sep = "-" * 140
    print(f"\n  {'Rank':>4}  {'ADG%':>8}  {'ADGw%':>8}  {'Gain':>8}  {'Sharpe':>8}  {'SharpW':>8}  {'Sortino':>8}  {'DD%':>8}  {'LPR%':>8}  {'WE max':>8}  {'Pos/day':>8}  {'Dist':>8}  Filename")
    print(f"  {sep}")

    for i, r in enumerate(shown, 1):
        # Color hint: mark the best one
        marker = " *" if i == 1 else "  "
        print(
            f"{marker}{i:>4}"
            f"  {fmt(r['adg'], 8, 4)}"
            f"  {fmt(r['adg_w'], 8, 4)}"
            f"  {fmt(r['gain'], 8, 3)}"
            f"  {fmt(r['sharpe'], 8, 4)}"
            f"  {fmt(r['sharpe_w'], 8, 4)}"
            f"  {fmt(r['sortino'], 8, 4)}"
            f"  {fmt(r['drawdown_worst'], 8, 2)}"
            f"  {fmt(r['loss_profit_ratio'], 8, 3)}"
            f"  {fmt(r['exposure_max'], 8, 3)}"
            f"  {fmt(r['pos_per_day'], 8, 2)}"
            f"  {fmt(r['dist'], 8, 4)}"
            f"  {r['filename'][:50]}"
        )

    if len(ranked) > top_n:
        print(f"\n  ... and {len(ranked) - top_n} more. Use --top {len(ranked)} to see all.\n")

    # Summary
    print(f"\n  Scoring objectives: {', '.join(scoring_keys)}")
    print(f"  Total pareto configs: {len(ranked)}")
    print(f"  Sorted by: {'distance to ideal' if not sort_by else sort_by}\n")

    # Best config details
    best = ranked[0]
    print(f"  Best config: {best['filename']}")
    print(f"    ADG={best['adg']:.4f}%  Gain={best['gain']:.3f}x  Sharpe={best['sharpe']:.4f}  DD={best['drawdown_worst']:.2f}%\n")


def print_run_info(entry):
    """Print run metadata."""
    bt = entry.get("backtest", {})
    opt = entry.get("optimize", {})
    coins = []
    for exch_coins in bt.get("coins", {}).values():
        if isinstance(exch_coins, list):
            coins.extend(exch_coins)

    print(f"\n  Run info:")
    print(f"    Exchange:    {', '.join(bt.get('exchanges', []))}")
    print(f"    Period:      {bt.get('start_date', '?')} to {bt.get('end_date', '?')}")
    print(f"    Coins:       {', '.join(coins[:10])}{'...' if len(coins) > 10 else ''} ({len(coins)} total)")
    print(f"    Balance:     ${bt.get('starting_balance', '?')}")
    print(f"    Population:  {opt.get('population_size', '?')}")
    print(f"    Iterations:  {opt.get('iters', '?')}")
    print(f"    Scoring:     {', '.join(opt.get('scoring', []))}")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive Pareto ranking tool for passivbot optimization results"
    )
    parser.add_argument(
        "run_folder", nargs="?", default=None,
        help="Path to a specific run folder (skip interactive picker)"
    )
    parser.add_argument(
        "--top", "-n", type=int, default=20,
        help="Number of top results to show (default: 20)"
    )
    parser.add_argument(
        "--sort", "-s", type=str, default=None,
        help="Sort by metric: dist, adg, adg_w, gain, sharpe, sharpe_w, sortino, drawdown, loss"
    )
    parser.add_argument(
        "--dir", "-d", type=str, default=RESULTS_DIR,
        help="Path to optimize_results directory"
    )
    args = parser.parse_args()

    results_dir = args.dir

    if args.run_folder:
        # Direct path provided
        run_path = args.run_folder
        if not os.path.isdir(run_path):
            # Try as a subfolder of optimize_results
            run_path = os.path.join(results_dir, args.run_folder)
        if not os.path.isdir(run_path):
            print(f"  Error: folder not found: {args.run_folder}")
            sys.exit(1)
    else:
        # Interactive picker
        runs = discover_runs(results_dir)
        if not runs:
            print(f"  No optimization runs found in {results_dir}")
            sys.exit(1)
        folder_name = pick_run(runs)
        run_path = os.path.join(results_dir, folder_name)

    pareto_dir = os.path.join(run_path, "pareto")
    if not os.path.isdir(pareto_dir):
        pareto_dir = run_path  # maybe they pointed directly at pareto/

    entries = load_pareto_entries(pareto_dir)
    if not entries:
        print(f"  No pareto JSON files found in {pareto_dir}")
        sys.exit(1)

    # Show run info from the first entry
    print_run_info(entries[0][1])

    # Compute ranking
    ranked, scoring_keys, w_keys = compute_ranking(entries)

    # Display
    print_ranking(ranked, scoring_keys, top_n=args.top, sort_by=args.sort)


if __name__ == "__main__":
    main()
