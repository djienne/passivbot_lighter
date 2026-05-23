"""Sweep TWEL from 1.0 to 2.5 by 0.1 and report Sharpe ratios."""
import sys, os, json, asyncio, copy
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from rust_utils import check_and_maybe_compile
check_and_maybe_compile(skip=True)

from config_utils import load_config, format_config, require_config_value
from utils import load_markets, format_approved_ignored_coins, format_end_date
from backtest import prepare_hlcvs_mss, run_backtest
from copy import deepcopy
from tools.event_loop_policy import set_windows_event_loop_policy

set_windows_event_loop_policy()

async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/config_hype.json"
    base_config = load_config(config_path)
    base_config = format_config(base_config, verbose=False)

    backtest_exchanges = require_config_value(base_config, "backtest.exchanges")
    for ex in backtest_exchanges:
        await load_markets(ex)
    await format_approved_ignored_coins(base_config, backtest_exchanges)

    exchange = backtest_exchanges[0]

    # Prepare data once
    base_config["disable_plotting"] = True
    base_config["backtest"]["cache_dir"] = {}
    base_config["backtest"]["coins"] = {}
    coins, hlcvs, mss, results_path, cache_dir, btc_usd_prices, timestamps = (
        await prepare_hlcvs_mss(base_config, exchange)
    )

    twels = [round(x * 0.1, 1) for x in range(10, 26)]
    results = []

    for twel in twels:
        cfg = deepcopy(base_config)
        cfg["bot"]["long"]["total_wallet_exposure_limit"] = twel
        cfg["disable_plotting"] = True
        cfg["backtest"]["cache_dir"] = {exchange: str(cache_dir)}
        cfg["backtest"]["coins"] = {exchange: coins}

        fills, equities_array, analysis = run_backtest(
            hlcvs, mss, cfg, exchange, btc_usd_prices, timestamps
        )
        sharpe = analysis.get("sharpe_ratio_w_usd", analysis.get("sharpe_ratio_w", 0))
        adg = analysis.get("adg_w_usd", analysis.get("adg_w", 0))
        dd = analysis.get("drawdown_worst_usd", analysis.get("drawdown_worst", 0))
        gain = analysis.get("gain_usd", analysis.get("gain", 0))
        calmar = analysis.get("calmar_ratio_w_usd", analysis.get("calmar_ratio_w", 0))

        results.append({
            "twel": twel, "sharpe_w": sharpe, "adg_w": adg,
            "drawdown": dd, "gain": gain, "calmar_w": calmar,
        })
        print(f"TWEL={twel:.1f}  sharpe_w={sharpe:.4f}  adg_w={adg:.6f}  dd={dd:.4f}  gain={gain:.4f}  calmar_w={calmar:.4f}")

    print("\n" + "="*80)
    best = max(results, key=lambda r: r["sharpe_w"])
    print(f"BEST SHARPE: TWEL={best['twel']:.1f}  sharpe_w={best['sharpe_w']:.4f}  "
          f"adg_w={best['adg_w']:.6f}  dd={best['drawdown']:.4f}  gain={best['gain']:.4f}")

asyncio.run(main())
