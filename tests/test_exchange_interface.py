"""Cross-exchange interface compliance tests.

Verifies that exchange implementations fulfill the ExchangeInterface contract
by testing real code paths with mocked network boundaries.
"""
import sys
import os
import types
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from exchanges.exchange_interface import ExchangeInterface


# ===========================================================================
# ABC contract tests: verify all abstract methods are defined
# ===========================================================================

EXPECTED_METHODS = [
    "create_ccxt_sessions",
    "close",
    "set_market_specific_settings",
    "symbol_is_eligible",
    "fetch_positions",
    "fetch_open_orders",
    "fetch_tickers",
    "fetch_ohlcv",
    "fetch_ohlcvs_1m",
    "fetch_pnls",
    "execute_order",
    "execute_cancellation",
    "did_create_order",
    "did_cancel_order",
    "get_order_execution_params",
    "update_exchange_config",
    "update_exchange_config_by_symbols",
    "watch_orders",
]


class TestExchangeInterfaceContract:
    """Verify the ABC itself is well-formed."""

    def test_all_expected_methods_are_abstract(self):
        abstract_names = {
            name
            for name, _ in inspect.getmembers(ExchangeInterface, predicate=inspect.isfunction)
            if getattr(getattr(ExchangeInterface, name), "__isabstractmethod__", False)
        }
        for method_name in EXPECTED_METHODS:
            assert method_name in abstract_names, f"{method_name} should be abstract"

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            ExchangeInterface()


class TestPassivbotImplementsInterface:
    """Verify that Passivbot base class implements all interface methods."""

    def test_passivbot_has_all_methods(self):
        from passivbot import Passivbot
        for method_name in EXPECTED_METHODS:
            assert hasattr(Passivbot, method_name), (
                f"Passivbot missing interface method: {method_name}"
            )

    def test_passivbot_is_subclass(self):
        from passivbot import Passivbot
        assert issubclass(Passivbot, ExchangeInterface)


# ===========================================================================
# Lighter-specific interface compliance
# ===========================================================================


def _make_lighter_bot():
    """Helper to create a minimal LighterBot for compliance testing."""
    lighter_mock = MagicMock()
    lighter_mock.Configuration.return_value = MagicMock()
    lighter_mock.ApiClient.return_value = MagicMock()
    lighter_mock.OrderApi.return_value = MagicMock()
    lighter_mock.AccountApi.return_value = MagicMock()
    lighter_mock.CandlestickApi.return_value = MagicMock()
    lighter_mock.RootApi.return_value = MagicMock()

    signer_mock = MagicMock()
    signer_mock.ORDER_TYPE_LIMIT = 0
    signer_mock.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
    signer_mock.ORDER_TIME_IN_FORCE_POST_ONLY = 2
    signer_mock.CROSS_MARGIN_MODE = 0
    signer_mock.create_auth_token_with_expiry.return_value = ("token", None)
    signer_mock.create_order = AsyncMock(return_value=("tx", "0xhash", None))
    nonce_mock = MagicMock()
    nonce_mock.next_nonce.return_value = (0, 1)
    signer_mock.nonce_manager = nonce_mock
    signer_mock.sign_cancel_order.return_value = (1, {}, "0xhash", None)
    signer_mock.send_tx = AsyncMock(return_value=types.SimpleNamespace(code=0))
    lighter_mock.SignerClient.return_value = signer_mock
    lighter_mock.SignerClient.ORDER_TYPE_LIMIT = 0
    lighter_mock.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
    lighter_mock.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY = 2
    lighter_mock.SignerClient.CROSS_MARGIN_MODE = 0

    config = {
        "live": {
            "user": "lighter_test",
            "approved_coins": {"long": ["HYPE"], "short": []},
            "ignored_coins": {"long": [], "short": []},
            "empty_means_all_approved": False,
            "minimum_coin_age_days": 0,
            "max_memory_candles_per_symbol": 1000,
            "max_disk_candles_per_symbol_per_tf": 1000,
            "inactive_coin_candle_ttl_minutes": 10,
            "memory_snapshot_interval_minutes": 30,
            "auto_gs": True,
            "leverage": 10,
            "time_in_force": "good_till_cancelled",
            "pnls_max_lookback_days": 30,
            "dry_run": False,
            "dry_run_wallet": 10000.0,
            "execution_delay_seconds": 2,
            "filter_by_min_effective_cost": True,
            "forced_mode_long": "",
            "forced_mode_short": "",
            "market_orders_allowed": True,
            "max_n_cancellations_per_batch": 5,
            "max_n_creations_per_batch": 3,
            "max_n_restarts_per_day": 10,
            "max_warmup_minutes": 0,
            "price_distance_threshold": 0.002,
            "warmup_ratio": 0.2,
        },
        "bot": {
            "long": {
                "n_positions": 1,
                "total_wallet_exposure_limit": 1.0,
                "ema_span_0": 60, "ema_span_1": 60,
                "entry_initial_qty_pct": 0.1, "entry_initial_ema_dist": 0.01,
                "entry_grid_spacing_pct": 0.01, "entry_grid_spacing_we_weight": 0,
                "entry_grid_spacing_log_weight": 0, "entry_grid_spacing_log_span_hours": 24,
                "entry_grid_double_down_factor": 0.1,
                "entry_trailing_grid_ratio": -1, "entry_trailing_retracement_pct": 0,
                "entry_trailing_threshold_pct": -0.1, "entry_trailing_double_down_factor": 0.1,
                "close_grid_markup_start": 0.01, "close_grid_markup_end": 0.01,
                "close_grid_qty_pct": 0.1,
                "close_trailing_grid_ratio": -1, "close_trailing_qty_pct": 0.1,
                "close_trailing_retracement_pct": 0, "close_trailing_threshold_pct": -0.1,
                "unstuck_close_pct": 0.005, "unstuck_ema_dist": -0.1,
                "unstuck_loss_allowance_pct": 0.01, "unstuck_threshold": 0.7,
                "enforce_exposure_limit": True,
                "filter_log_range_ema_span": 60, "filter_volume_drop_pct": 0,
                "filter_volume_ema_span": 10,
            },
            "short": {
                "n_positions": 0, "total_wallet_exposure_limit": 0,
                "ema_span_0": 200, "ema_span_1": 200,
                "entry_initial_qty_pct": 0.025, "entry_initial_ema_dist": -0.1,
                "entry_grid_spacing_pct": 0.01, "entry_grid_spacing_we_weight": 0,
                "entry_grid_spacing_log_weight": 0, "entry_grid_spacing_log_span_hours": 24,
                "entry_grid_double_down_factor": 0.1,
                "entry_trailing_grid_ratio": -1, "entry_trailing_retracement_pct": 0,
                "entry_trailing_threshold_pct": -0.1, "entry_trailing_double_down_factor": 0.1,
                "close_grid_markup_start": 0.01, "close_grid_markup_end": 0.01,
                "close_grid_qty_pct": 0.1,
                "close_trailing_grid_ratio": -1, "close_trailing_qty_pct": 0.1,
                "close_trailing_retracement_pct": 0, "close_trailing_threshold_pct": -0.1,
                "unstuck_close_pct": 0.005, "unstuck_ema_dist": -0.1,
                "unstuck_loss_allowance_pct": 0.01, "unstuck_threshold": 0.7,
                "enforce_exposure_limit": True,
                "filter_log_range_ema_span": 10, "filter_volume_drop_pct": 0,
                "filter_volume_ema_span": 10,
            },
        },
        "logging": {"level": 0},
    }

    user_info = {
        "exchange": "lighter",
        "private_key": "0xdeadbeef",
        "account_index": 0,
        "api_key_index": 0,
    }

    with patch.dict("sys.modules", {"lighter": lighter_mock, "lighter.exceptions": MagicMock()}):
        with patch("passivbot.load_user_info", return_value=user_info), \
             patch("procedures.load_user_info", return_value=user_info), \
             patch("passivbot.load_broker_code", return_value=""), \
             patch("passivbot.normalize_exchange_name", return_value="lighter"), \
             patch("passivbot.resolve_custom_endpoint_override", return_value=None), \
             patch("passivbot.CandlestickManager"):
            from exchanges.lighter import LighterBot
            bot = LighterBot(config)

    bot.lighter_client = signer_mock

    # Set up one market for testing
    symbol = "HYPE/USDC:USDC"
    bot.market_id_map[symbol] = 5
    bot.market_index_to_symbol[5] = symbol
    bot.price_tick_sizes[symbol] = 0.0001
    bot.amount_tick_sizes[symbol] = 0.01
    bot.markets_dict = {
        symbol: {
            "symbol": symbol, "id": "5", "base": "HYPE", "quote": "USDC",
            "active": True, "swap": True, "linear": True, "type": "swap",
            "precision": {"amount": 0.01, "price": 0.0001},
            "limits": {"amount": {"min": 0.01}, "cost": {"min": 1.0}},
            "contractSize": 1.0,
            "info": {"market_id": 5, "maxLeverage": 50},
        }
    }
    bot.set_market_specific_settings()
    bot._build_coin_symbol_caches()
    bot.positions = {}
    bot.open_orders = {}
    bot.active_symbols = [symbol]

    return bot


class TestLighterInterfaceCompliance:
    """Verify LighterBot fulfills ExchangeInterface."""

    def test_is_subclass_of_interface(self):
        lighter_mock = MagicMock()
        lighter_mock.SignerClient = MagicMock
        with patch.dict("sys.modules", {"lighter": lighter_mock, "lighter.exceptions": MagicMock()}):
            from exchanges.lighter import LighterBot
            assert issubclass(LighterBot, ExchangeInterface)

    def test_has_all_interface_methods(self):
        lighter_mock = MagicMock()
        lighter_mock.SignerClient = MagicMock
        with patch.dict("sys.modules", {"lighter": lighter_mock, "lighter.exceptions": MagicMock()}):
            from exchanges.lighter import LighterBot
            for method_name in EXPECTED_METHODS:
                assert hasattr(LighterBot, method_name), (
                    f"LighterBot missing interface method: {method_name}"
                )

    @pytest.mark.asyncio
    async def test_execute_order_returns_dict(self):
        bot = _make_lighter_bot()
        order = {
            "symbol": "HYPE/USDC:USDC",
            "side": "buy",
            "qty": 1.0,
            "price": 15.0,
            "reduce_only": False,
            "custom_id": "test",
        }
        result = await bot.execute_order(order)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_execute_cancellation_returns_dict(self):
        bot = _make_lighter_bot()
        order = {"id": "12345", "symbol": "HYPE/USDC:USDC"}
        result = await bot.execute_cancellation(order)
        assert isinstance(result, dict)

    def test_did_create_order_validates(self):
        bot = _make_lighter_bot()
        assert bot.did_create_order({"id": "123", "status": "open"})
        assert not bot.did_create_order({})
        assert not bot.did_create_order(None)

    def test_did_cancel_order_validates(self):
        bot = _make_lighter_bot()
        assert bot.did_cancel_order({"status": "success"})
        assert not bot.did_cancel_order({})
        assert not bot.did_cancel_order({"status": "failed"})

    def test_set_market_specific_settings_populates(self):
        bot = _make_lighter_bot()
        for symbol in bot.markets_dict:
            assert symbol in bot.symbol_ids
            assert symbol in bot.min_costs
            assert symbol in bot.min_qtys
            assert symbol in bot.qty_steps
            assert symbol in bot.price_steps
            assert symbol in bot.c_mults
            assert symbol in bot.max_leverage
            assert bot.min_costs[symbol] > 0
            assert bot.qty_steps[symbol] > 0
            assert bot.price_steps[symbol] > 0
            assert bot.c_mults[symbol] > 0
            assert bot.max_leverage[symbol] > 0

    def test_get_order_execution_params_returns_dict(self):
        bot = _make_lighter_bot()
        params = bot.get_order_execution_params({"reduce_only": False})
        assert isinstance(params, dict)

    def test_symbol_is_eligible(self):
        bot = _make_lighter_bot()
        assert bot.symbol_is_eligible("HYPE/USDC:USDC")
        assert not bot.symbol_is_eligible("NONEXIST/USDC:USDC")

    @pytest.mark.asyncio
    async def test_update_exchange_config_no_crash(self):
        bot = _make_lighter_bot()
        await bot.update_exchange_config()  # Should be no-op, no crash
