"""TWEL sweep for hype_top_lighter.json — finds best Sharpe."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent
RESULTS_DIR = REPO / "backtests" / "lighter_hype_top" / "lighter"
TWEL_VALUES = [round(1.0 + 0.1 * i, 1) for i in range(11)]


def list_runs():
    return {p.name for p in RESULTS_DIR.iterdir() if p.is_dir()}


def run_backtest(twel: float) -> Path:
    before = list_runs()
    cmd = [
        sys.executable,
        "src/backtest.py",
        "configs/hype_top_lighter.json",
        "--bot_long_total_wallet_exposure_limit",
        str(twel),
        "-dp",
    ]
    print(f"\n=== TWEL={twel} ===", flush=True)
    result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if result.returncode != 0:
        print("STDERR:", result.stderr[-2000:])
        raise RuntimeError(f"backtest failed for TWEL={twel}")
    after = list_runs()
    new = sorted(after - before)
    if not new:
        raise RuntimeError(f"no new run dir for TWEL={twel}")
    return RESULTS_DIR / new[-1]


def main():
    rows = []
    for twel in TWEL_VALUES:
        run_dir = run_backtest(twel)
        analysis = json.loads((run_dir / "analysis.json").read_text())
        row = {
            "twel": twel,
            "run_dir": run_dir.name,
            "sharpe_usd": analysis.get("sharpe_ratio_usd"),
            "sharpe_pnl": analysis.get("sharpe_ratio_pnl"),
            "sharpe_w_usd": analysis.get("sharpe_ratio_w_usd"),
            "sortino_usd": analysis.get("sortino_ratio_usd"),
            "adg_usd": analysis.get("adg_usd"),
            "mdg_usd": analysis.get("mdg_usd"),
            "gain_usd": analysis.get("gain_usd"),
            "drawdown_worst": analysis.get("drawdown_worst_usd"),
            "twe_max": analysis.get("total_wallet_exposure_max"),
            "twe_mean": analysis.get("total_wallet_exposure_mean"),
            "loss_profit_ratio": analysis.get("loss_profit_ratio"),
        }
        rows.append(row)
        print(
            f"TWEL={twel}  sharpe_usd={row['sharpe_usd']:.4f}  "
            f"sharpe_pnl={row['sharpe_pnl']:.4f}  "
            f"adg={row['adg_usd']:.5f}  dd={row['drawdown_worst']:.4f}",
            flush=True,
        )

    out = REPO / "twel_sweep_results.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nSaved {out}")

    best = max(rows, key=lambda r: r["sharpe_usd"])
    print("\n=== BEST sharpe_ratio_usd ===")
    print(json.dumps(best, indent=2))


if __name__ == "__main__":
    main()
