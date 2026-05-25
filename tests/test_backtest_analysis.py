import numpy as np

from backtest import expand_analysis


def _make_analysis_entry(value):
    base_keys = [
        "adg",
        "adg_w",
        "mdg",
        "mdg_w",
        "gain",
        "positions_held_per_day",
        "position_held_hours_mean",
        "position_held_hours_max",
        "position_held_hours_median",
        "position_unchanged_hours_max",
        "loss_profit_ratio",
        "loss_profit_ratio_w",
        "volume_pct_per_day_avg",
        "volume_pct_per_day_avg_w",
        "peak_recovery_hours_pnl",
        "total_wallet_exposure_max",
        "total_wallet_exposure_mean",
        "total_wallet_exposure_median",
        "entry_initial_balance_pct_long",
        "entry_initial_balance_pct_short",
    ]
    analysis = {key: value for key in base_keys}
    analysis.update(
        {
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "omega_ratio": 0.0,
            "expected_shortfall_1pct": 0.0,
            "calmar_ratio": 0.0,
            "sterling_ratio": 0.0,
            "drawdown_worst": 0.0,
            "drawdown_worst_mean_1pct": 0.0,
            "equity_balance_diff_neg_max": 0.0,
            "equity_balance_diff_neg_mean": 0.0,
            "equity_balance_diff_pos_max": 0.0,
            "equity_balance_diff_pos_mean": 0.0,
            "peak_recovery_hours_equity": 0.0,
            "equity_choppiness": 0.0,
            "equity_jerkiness": 0.0,
            "exponential_fit_error": 0.0,
            "equity_choppiness_w": 0.0,
            "equity_jerkiness_w": 0.0,
            "exponential_fit_error_w": 0.0,
            "adg_per_exposure_long": 0.0,
            "adg_per_exposure_short": 0.0,
        }
    )
    return analysis


def test_expand_analysis_includes_entry_balance_pct():
    analysis_usd = _make_analysis_entry(0.123)
    analysis_btc = _make_analysis_entry(0.456)
    config = {
        "bot": {
            "long": {"total_wallet_exposure_limit": 1.0},
            "short": {"total_wallet_exposure_limit": 1.0},
        }
    }
    result = expand_analysis(
        analysis_usd,
        analysis_btc,
        fills=np.empty((0, 0)),
        equities_array=np.empty((0, 3)),
        config=config,
    )
    assert "entry_initial_balance_pct_long" in result
    assert "entry_initial_balance_pct_short" in result
    assert result["entry_initial_balance_pct_long"] == 0.123
    assert result["entry_initial_balance_pct_short"] == 0.123


def test_expand_analysis_keeps_strategy_trade_metrics_shared():
    analysis_usd = _make_analysis_entry(0.25)
    analysis_btc = _make_analysis_entry(0.75)
    for key in [
        "win_rate",
        "win_rate_w",
        "trade_loss_max",
        "trade_loss_mean",
        "trade_loss_median",
        "gain_strategy_eq",
        "adg_strategy_eq",
        "drawdown_worst_strategy_eq",
        "peak_recovery_hours_strategy_eq",
        "loss_profit_ratio_long",
        "loss_profit_ratio_short",
    ]:
        analysis_usd[key] = 0.25
        analysis_btc[key] = 0.25
    config = {
        "bot": {
            "long": {"total_wallet_exposure_limit": 1.0},
            "short": {"total_wallet_exposure_limit": 1.0},
        }
    }

    result = expand_analysis(
        analysis_usd,
        analysis_btc,
        fills=np.empty((0, 0)),
        equities_array=np.empty((0, 3)),
        config=config,
    )

    assert result["win_rate"] == 0.25
    assert result["trade_loss_max"] == 0.25
    assert result["gain_strategy_eq"] == 0.25
    assert result["drawdown_worst_strategy_eq"] == 0.25
    assert result["loss_profit_ratio_long"] == 0.25
    assert "win_rate_usd" not in result
    assert "gain_strategy_eq_usd" not in result
