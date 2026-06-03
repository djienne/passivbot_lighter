"""Run HYPE TWEL/unstuck backtest sweep for selected configs.

This prepares market data once per config, then runs in-memory backtests for
TWEL 1.0..2.0 with unstuck off/on. Results are written incrementally to
``results/`` as CSV, JSON, and a markdown summary.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("SKIP_RUST_COMPILE", "1")

from config_utils import format_config, load_config, require_config_value  # noqa: E402
from utils import format_approved_ignored_coins, load_markets  # noqa: E402
from backtest import prepare_hlcvs_mss, run_backtest  # noqa: E402
from tools.event_loop_policy import set_windows_event_loop_policy  # noqa: E402


CONFIG_PATHS = [
    ROOT / "configs" / "hype_top.json",
    ROOT / "configs" / "config_hype.json",
]
TWELS = [round(i / 10, 1) for i in range(10, 21)]
UNSTUCK_OFF = {
    "unstuck_close_pct": 0.0,
    "unstuck_ema_dist": 0.0,
    "unstuck_loss_allowance_pct": 0.0,
    "unstuck_threshold": 1.0,
}
UNSTUCK_ON = {
    "unstuck_close_pct": 0.0050246,
    "unstuck_ema_dist": 0.0069327,
    "unstuck_loss_allowance_pct": 0.18987,
    "unstuck_threshold": 0.79883,
}
METRIC_FIELDS = [
    "config",
    "twel",
    "unstuck",
    "liquidated",
    "adg_w_usd",
    "adg_usd",
    "gain_usd",
    "drawdown_worst_usd",
    "sharpe_ratio_w_usd",
    "sharpe_ratio_usd",
    "loss_profit_ratio",
    "loss_profit_ratio_w",
    "calmar_ratio_w_usd",
    "sterling_ratio_w_usd",
    "total_wallet_exposure_max",
    "total_wallet_exposure_mean",
    "position_held_hours_max",
    "positions_held_per_day",
    "n_fills",
]


def metric(analysis: dict[str, Any], key: str) -> float | None:
    if key in analysis:
        return analysis[key]
    if key.endswith("_usd"):
        return analysis.get(key.removesuffix("_usd"))
    return None


def row_from_analysis(
    config_name: str,
    twel: float,
    unstuck_state: str,
    analysis: dict[str, Any],
    fills: Any,
) -> dict[str, Any]:
    row = {"config": config_name, "twel": twel, "unstuck": unstuck_state}
    for key in METRIC_FIELDS:
        if key in row:
            continue
        if key == "n_fills":
            row[key] = int(len(fills)) if fills is not None else 0
        else:
            row[key] = metric(analysis, key)
    return row


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METRIC_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")


def fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def best_by(rows: list[dict[str, Any]], key: str, reverse: bool = True) -> dict[str, Any] | None:
    valid = [row for row in rows if row.get(key) is not None]
    if not valid:
        return None
    return sorted(valid, key=lambda row: row[key], reverse=reverse)[0]


def make_markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# HYPE TWEL/Unstuck Sweep",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Unstuck-on profile:",
        "",
        "| key | value |",
        "|---|---:|",
    ]
    for key, value in UNSTUCK_ON.items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend(
        [
            "",
            "## Best By Config",
            "",
            "| config | best sharpe TWEL | unstuck | liquidated | sharpe_w | adg_w | gain | drawdown | LPR | max TWEL | fills |",
            "|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for config_name in sorted({row["config"] for row in rows}):
        subset = [row for row in rows if row["config"] == config_name]
        best = best_by(subset, "sharpe_ratio_w_usd")
        if not best:
            continue
        lines.append(
            "| {config} | {twel:.1f} | {unstuck} | {liquidated} | {sharpe} | {adg} | {gain} | {dd} | {lpr} | {max_twel} | {fills} |".format(
                config=best["config"],
                twel=best["twel"],
                unstuck=best["unstuck"],
                liquidated=best.get("liquidated", ""),
                sharpe=fmt(best.get("sharpe_ratio_w_usd"), 4),
                adg=fmt(best.get("adg_w_usd"), 6),
                gain=fmt(best.get("gain_usd"), 4),
                dd=fmt(best.get("drawdown_worst_usd"), 4),
                lpr=fmt(best.get("loss_profit_ratio"), 4),
                max_twel=fmt(best.get("total_wallet_exposure_max"), 4),
                fills=best.get("n_fills", ""),
            )
        )
    lines.extend(
        [
            "",
            "## Full Results",
            "",
            "| config | TWEL | unstuck | liquidated | sharpe_w | adg_w | gain | drawdown | LPR | max TWEL | mean TWEL | fills |",
            "|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(rows, key=lambda r: (r["config"], r["unstuck"], r["twel"])):
        lines.append(
            "| {config} | {twel:.1f} | {unstuck} | {liquidated} | {sharpe} | {adg} | {gain} | {dd} | {lpr} | {max_twel} | {mean_twel} | {fills} |".format(
                config=row["config"],
                twel=row["twel"],
                unstuck=row["unstuck"],
                liquidated=row.get("liquidated", ""),
                sharpe=fmt(row.get("sharpe_ratio_w_usd"), 4),
                adg=fmt(row.get("adg_w_usd"), 6),
                gain=fmt(row.get("gain_usd"), 4),
                dd=fmt(row.get("drawdown_worst_usd"), 4),
                lpr=fmt(row.get("loss_profit_ratio"), 4),
                max_twel=fmt(row.get("total_wallet_exposure_max"), 4),
                mean_twel=fmt(row.get("total_wallet_exposure_mean"), 4),
                fills=row.get("n_fills", ""),
            )
        )
    return "\n".join(lines) + "\n"


def scenario_config(base_config: dict[str, Any], twel: float, unstuck: dict[str, float]) -> dict[str, Any]:
    cfg = deepcopy(base_config)
    cfg["disable_plotting"] = True
    cfg["bot"]["long"]["total_wallet_exposure_limit"] = twel
    cfg["bot"]["long"].update(unstuck)
    return cfg


async def run_for_config(
    config_path: Path,
    rows: list[dict[str, Any]],
    csv_path: Path,
    json_path: Path,
    md_path: Path,
) -> None:
    config_name = config_path.stem
    print(f"\n=== Preparing {config_name} ===", flush=True)
    base_config = format_config(load_config(str(config_path)), verbose=False)
    exchanges = require_config_value(base_config, "backtest.exchanges")
    for exchange in exchanges:
        await load_markets(exchange)
    await format_approved_ignored_coins(base_config, exchanges)

    exchange = exchanges[0]
    prep_config = deepcopy(base_config)
    prep_config["disable_plotting"] = True
    prep_config["backtest"]["cache_dir"] = {}
    prep_config["backtest"]["coins"] = {}
    coins, hlcvs, mss, _results_path, cache_dir, btc_usd_prices, timestamps = (
        await prepare_hlcvs_mss(prep_config, exchange)
    )

    for unstuck_state, unstuck_values in [("off", UNSTUCK_OFF), ("on", UNSTUCK_ON)]:
        for twel in TWELS:
            cfg = scenario_config(base_config, twel, unstuck_values)
            cfg["backtest"]["cache_dir"] = {exchange: str(cache_dir)}
            cfg["backtest"]["coins"] = {exchange: coins}
            print(
                f"Running {config_name} | unstuck={unstuck_state} | TWEL={twel:.1f}",
                flush=True,
            )
            fills, equities_array, analysis = run_backtest(
                hlcvs, mss, cfg, exchange, btc_usd_prices, timestamps
            )
            del equities_array
            row = row_from_analysis(config_name, twel, unstuck_state, analysis, fills)
            rows.append(row)
            write_csv(csv_path, rows)
            write_json(json_path, rows)
            md_path.write_text(make_markdown(rows), encoding="utf-8")
            print(
                "Done {config} | {unstuck} | TWEL={twel:.1f} | sharpe_w={sharpe} | adg_w={adg} | dd={dd} | gain={gain}".format(
                    config=config_name,
                    unstuck=unstuck_state,
                    twel=twel,
                    sharpe=fmt(row.get("sharpe_ratio_w_usd"), 4),
                    adg=fmt(row.get("adg_w_usd"), 6),
                    dd=fmt(row.get("drawdown_worst_usd"), 4),
                    gain=fmt(row.get("gain_usd"), 4),
                ),
                flush=True,
            )


async def main() -> None:
    set_windows_event_loop_policy()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    csv_path = results_dir / f"hype_twel_unstuck_sweep_{timestamp}.csv"
    json_path = results_dir / f"hype_twel_unstuck_sweep_{timestamp}.json"
    md_path = results_dir / f"hype_twel_unstuck_sweep_{timestamp}.md"
    print(f"Writing results to {csv_path}", flush=True)
    rows: list[dict[str, Any]] = []
    for config_path in CONFIG_PATHS:
        await run_for_config(config_path, rows, csv_path, json_path, md_path)
    print("\n=== Complete ===", flush=True)
    print(md_path, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
