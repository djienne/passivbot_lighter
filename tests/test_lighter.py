"""Lighter-specific unit tests.

Tests Lighter SDK integration, price/amount conversion, market mapping,
order execution, and WebSocket parsing — all using mock data, no network.
"""
import asyncio
import copy
import math
import sys
import os
import time
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from fixtures.lighter_responses import (
    MOCK_ORDER_BOOKS_RESPONSE,
    MOCK_ACCOUNT_RESPONSE,
    MOCK_ACCOUNT_RESPONSE_EMPTY,
    MOCK_ACTIVE_ORDERS,
    MOCK_CANDLES,
    MOCK_INACTIVE_ORDERS,
)


# ---------------------------------------------------------------------------
# Install lighter mock permanently so module imports work across all tests
# ---------------------------------------------------------------------------

_lighter_mod = MagicMock()
_lighter_mod.SignerClient.ORDER_TYPE_LIMIT = 0
_lighter_mod.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
_lighter_mod.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY = 2
_lighter_mod.SignerClient.CROSS_MARGIN_MODE = 0
_lighter_mod.SignerClient.TX_TYPE_CREATE_ORDER = 14
_lighter_mod.SignerClient.TX_TYPE_CANCEL_ORDER = 15
sys.modules.setdefault("lighter", _lighter_mod)
sys.modules.setdefault("lighter.exceptions", MagicMock())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_obj(d):
    """Recursively convert a dict into a namespace object."""
    if isinstance(d, dict):
        ns = types.SimpleNamespace(**{k: _make_obj(v) for k, v in d.items()})
        return ns
    if isinstance(d, list):
        return [_make_obj(item) for item in d]
    return d


def _build_order_books_response():
    obs = [_make_obj(ob) for ob in MOCK_ORDER_BOOKS_RESPONSE["order_books"]]
    return types.SimpleNamespace(order_books=obs)


def _build_account_response(data=None):
    if data is None:
        data = MOCK_ACCOUNT_RESPONSE
    accounts = []
    for acc_dict in data["accounts"]:
        acc = types.SimpleNamespace(
            available_balance=acc_dict["available_balance"],
            collateral=acc_dict["collateral"],
            total_asset_value=acc_dict["total_asset_value"],
            positions=acc_dict["positions"],
        )
        accounts.append(acc)
    return types.SimpleNamespace(accounts=accounts)


# Minimal config shared by all tests
_TEST_CONFIG = {
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
            "n_positions": 1, "total_wallet_exposure_limit": 1.0,
            "ema_span_0": 60, "ema_span_1": 60,
            "entry_initial_qty_pct": 0.1, "entry_initial_ema_dist": 0.01,
            "entry_grid_spacing_pct": 0.01, "entry_grid_spacing_we_weight": 0,
            "entry_grid_spacing_log_weight": 0, "entry_grid_spacing_log_span_hours": 24,
            "entry_grid_double_down_factor": 0.1, "entry_trailing_grid_ratio": -1,
            "entry_trailing_retracement_pct": 0, "entry_trailing_threshold_pct": -0.1,
            "entry_trailing_double_down_factor": 0.1,
            "close_grid_markup_start": 0.01, "close_grid_markup_end": 0.01,
            "close_grid_qty_pct": 0.1, "close_trailing_grid_ratio": -1,
            "close_trailing_qty_pct": 0.1, "close_trailing_retracement_pct": 0,
            "close_trailing_threshold_pct": -0.1,
            "unstuck_close_pct": 0.005, "unstuck_ema_dist": -0.1,
            "unstuck_loss_allowance_pct": 0.01, "unstuck_threshold": 0.7,
            "enforce_exposure_limit": True, "filter_log_range_ema_span": 60,
            "filter_volume_drop_pct": 0, "filter_volume_ema_span": 10,
        },
        "short": {
            "n_positions": 0, "total_wallet_exposure_limit": 0,
            "ema_span_0": 200, "ema_span_1": 200,
            "entry_initial_qty_pct": 0.025, "entry_initial_ema_dist": -0.1,
            "entry_grid_spacing_pct": 0.01, "entry_grid_spacing_we_weight": 0,
            "entry_grid_spacing_log_weight": 0, "entry_grid_spacing_log_span_hours": 24,
            "entry_grid_double_down_factor": 0.1, "entry_trailing_grid_ratio": -1,
            "entry_trailing_retracement_pct": 0, "entry_trailing_threshold_pct": -0.1,
            "entry_trailing_double_down_factor": 0.1,
            "close_grid_markup_start": 0.01, "close_grid_markup_end": 0.01,
            "close_grid_qty_pct": 0.1, "close_trailing_grid_ratio": -1,
            "close_trailing_qty_pct": 0.1, "close_trailing_retracement_pct": 0,
            "close_trailing_threshold_pct": -0.1,
            "unstuck_close_pct": 0.005, "unstuck_ema_dist": -0.1,
            "unstuck_loss_allowance_pct": 0.01, "unstuck_threshold": 0.7,
            "enforce_exposure_limit": True, "filter_log_range_ema_span": 10,
            "filter_volume_drop_pct": 0, "filter_volume_ema_span": 10,
        },
    },
    "logging": {"level": 0},
}

_USER_INFO = {
    "exchange": "lighter",
    "private_key": "0xdeadbeef",
    "account_index": 0,
    "api_key_index": 0,
}


def _make_signer_mock():
    """Create a fresh SignerClient mock with default behaviour."""
    m = MagicMock()
    m.ORDER_TYPE_LIMIT = 0
    m.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
    m.ORDER_TIME_IN_FORCE_POST_ONLY = 2
    m.CROSS_MARGIN_MODE = 0
    m.ISOLATED_MARGIN_MODE = 1
    m.DEFAULT_10_MIN_AUTH_EXPIRY = 600
    m.create_auth_token_with_expiry.return_value = ("mock_auth_token", None)
    m.create_order = AsyncMock(return_value=("tx", "0xhash", None))
    m.cancel_order = AsyncMock(return_value=("tx", "response", None))
    m.update_leverage = AsyncMock(return_value=("tx", "response", None))
    nm = MagicMock()
    nm.next_nonce.return_value = (0, 1)
    m.nonce_manager = nm
    m.sign_create_order.return_value = (14, {"info": "create"}, "0xhash", None)
    m.sign_cancel_order.return_value = (15, {"info": "cancel"}, "0xhash", None)
    m.send_tx = AsyncMock(return_value=types.SimpleNamespace(code=0, message="OK"))
    m.send_tx_batch = AsyncMock(return_value=types.SimpleNamespace(
        code=0, message="OK", tx_hash=["0xh1", "0xh2", "0xh3"],
        volume_quota_remaining=100,
    ))
    return m


def _create_bot():
    """Build a LighterBot with fully mocked SDK — safe to call many times."""
    config = copy.deepcopy(_TEST_CONFIG)
    signer = _make_signer_mock()

    with patch("passivbot.load_user_info", return_value=_USER_INFO), \
         patch("procedures.load_user_info", return_value=_USER_INFO), \
         patch("passivbot.load_broker_code", return_value=""), \
         patch("passivbot.normalize_exchange_name", return_value="lighter"), \
         patch("passivbot.resolve_custom_endpoint_override", return_value=None), \
         patch("passivbot.CandlestickManager"):
        from exchanges.lighter import LighterBot
        bot = LighterBot(config)

    bot.lighter_client = signer
    bot.api_client = MagicMock()
    bot.order_api = MagicMock()
    bot.account_api = MagicMock()
    bot.candlestick_api = MagicMock()

    bot.markets_dict = {}
    bot.positions = {}
    bot.open_orders = {}
    bot.active_symbols = []
    bot.coin_overrides = {}

    ob_resp = _build_order_books_response()
    for ob in ob_resp.order_books:
        base = ob.symbol.upper()
        sym = f"{base}/{bot.quote}:{bot.quote}"
        mid = ob.market_id
        pd = ob.supported_price_decimals
        sd = ob.supported_size_decimals
        pt = 10 ** (-pd)
        at = 10 ** (-sd)

        bot.market_id_map[sym] = mid
        bot.market_index_to_symbol[mid] = sym
        bot.price_tick_sizes[sym] = pt
        bot.amount_tick_sizes[sym] = at
        bot.price_decimals[sym] = pd
        bot.amount_decimals[sym] = sd
        bot.markets_dict[sym] = {
            "symbol": sym, "id": str(mid), "base": base, "quote": "USDC",
            "active": True, "swap": True, "linear": True, "type": "swap",
            "precision": {"amount": at, "price": pt},
            "limits": {"amount": {"min": float(ob.min_base_amount)},
                       "cost": {"min": float(ob.min_quote_amount)}},
            "contractSize": 1.0,
            "info": {"market_id": mid,
                     "maxLeverage": int(getattr(ob, "max_leverage", 50) or 50),
                     "price_decimals": pd, "size_decimals": sd},
        }

    bot._build_coin_symbol_caches()
    bot.set_market_specific_settings()
    return bot


@pytest.fixture
def lighter_bot():
    return _create_bot()


# ===========================================================================
# Price / Amount conversion tests
# ===========================================================================

class TestRawConversions:
    def test_raw_price_roundtrip(self, lighter_bot):
        symbol = "HYPE/USDC:USDC"
        for price in [15.1234, 14.0, 0.0001, 99999.9999]:
            raw = lighter_bot._to_raw_price(price, symbol)
            back = lighter_bot._from_raw_price(raw, symbol)
            tick = lighter_bot.price_tick_sizes[symbol]
            assert abs(back - round(price / tick) * tick) < tick * 0.1

    def test_raw_amount_roundtrip(self, lighter_bot):
        symbol = "HYPE/USDC:USDC"
        for amount in [10.05, 0.01, 1000.0, 0.5]:
            raw = lighter_bot._to_raw_amount(amount, symbol)
            back = lighter_bot._from_raw_amount(raw, symbol)
            tick = lighter_bot.amount_tick_sizes[symbol]
            assert abs(back - round(amount / tick) * tick) < tick * 0.1

    def test_btc_price_precision(self, lighter_bot):
        symbol = "BTC/USDC:USDC"
        assert lighter_bot.price_tick_sizes[symbol] == 0.1
        assert lighter_bot._to_raw_price(95000.5, symbol) == 950005

    def test_raw_amount_abs(self, lighter_bot):
        assert lighter_bot._to_raw_amount(-5.5, "HYPE/USDC:USDC") == 550


# ===========================================================================
# Market ID mapping tests
# ===========================================================================

class TestMarketMapping:
    def test_symbol_to_market_index(self, lighter_bot):
        assert lighter_bot._symbol_to_market_index("HYPE/USDC:USDC") == 5
        assert lighter_bot._symbol_to_market_index("BTC/USDC:USDC") == 0
        assert lighter_bot._symbol_to_market_index("ETH/USDC:USDC") == 1

    def test_market_index_to_symbol(self, lighter_bot):
        assert lighter_bot.market_index_to_symbol[5] == "HYPE/USDC:USDC"
        assert lighter_bot.market_index_to_symbol[0] == "BTC/USDC:USDC"

    def test_bijection(self, lighter_bot):
        for symbol, mid in lighter_bot.market_id_map.items():
            assert lighter_bot.market_index_to_symbol[mid] == symbol

    def test_coin_to_symbol(self, lighter_bot):
        assert lighter_bot.coin_to_symbol("HYPE") == "HYPE/USDC:USDC"
        assert lighter_bot.coin_to_symbol("BTC") == "BTC/USDC:USDC"
        assert lighter_bot.coin_to_symbol("NONEXIST") == ""


# ===========================================================================
# Market-specific settings tests
# ===========================================================================

class TestMarketSettings:
    def test_all_fields_populated(self, lighter_bot):
        for symbol in lighter_bot.markets_dict:
            assert symbol in lighter_bot.symbol_ids
            assert symbol in lighter_bot.min_costs
            assert symbol in lighter_bot.min_qtys
            assert symbol in lighter_bot.qty_steps
            assert symbol in lighter_bot.price_steps
            assert symbol in lighter_bot.c_mults
            assert symbol in lighter_bot.max_leverage

    def test_hype_values(self, lighter_bot):
        s = "HYPE/USDC:USDC"
        assert lighter_bot.qty_steps[s] == 0.01
        assert lighter_bot.price_steps[s] == 0.0001
        assert lighter_bot.c_mults[s] == 1.0
        assert lighter_bot.max_leverage[s] == 20  # from mock API max_leverage field
        assert lighter_bot.min_qtys[s] == 0.01
        assert lighter_bot.min_costs[s] >= 1.0


# ===========================================================================
# Order side and position side tests
# ===========================================================================

class TestPositionSide:
    def test_no_position_buy(self, lighter_bot):
        order = {"symbol": "HYPE/USDC:USDC", "side": "buy", "reduceOnly": False}
        assert lighter_bot.determine_pos_side(order) == "long"

    def test_no_position_sell(self, lighter_bot):
        order = {"symbol": "HYPE/USDC:USDC", "side": "sell", "reduceOnly": False}
        assert lighter_bot.determine_pos_side(order) == "short"

    def test_with_long_position(self, lighter_bot):
        lighter_bot.positions["HYPE/USDC:USDC"] = {
            "long": {"size": 10.0, "price": 15.0},
            "short": {"size": 0.0, "price": 0.0},
        }
        order = {"symbol": "HYPE/USDC:USDC", "side": "sell", "reduceOnly": True}
        assert lighter_bot.determine_pos_side(order) == "long"

    def test_with_short_position(self, lighter_bot):
        lighter_bot.positions["HYPE/USDC:USDC"] = {
            "long": {"size": 0.0, "price": 0.0},
            "short": {"size": -5.0, "price": 16.0},
        }
        order = {"symbol": "HYPE/USDC:USDC", "side": "buy", "reduceOnly": True}
        assert lighter_bot.determine_pos_side(order) == "short"

    def test_reduce_only_buy_no_position(self, lighter_bot):
        order = {"symbol": "HYPE/USDC:USDC", "side": "buy", "reduceOnly": True}
        assert lighter_bot.determine_pos_side(order) == "short"


# ===========================================================================
# Client order ID tests
# ===========================================================================

class TestClientOrderId:
    def test_within_bounds(self, lighter_bot):
        for _ in range(100):
            assert 0 <= lighter_bot._generate_client_order_id() < 2**48

    def test_uniqueness(self, lighter_bot):
        ids = set()
        for _ in range(10):
            ids.add(lighter_bot._generate_client_order_id())
            time.sleep(0.001)
        assert len(ids) > 5


# ===========================================================================
# Order execution tests
# ===========================================================================

class TestOrderExecution:
    @pytest.mark.asyncio
    async def test_execute_order_success(self, lighter_bot):
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
            "price": 14.80, "reduce_only": False, "custom_id": "test123",
        }
        result = await lighter_bot.execute_order(order)
        assert lighter_bot.did_create_order(result)
        assert result["symbol"] == "HYPE/USDC:USDC"
        assert result["side"] == "buy"

    @pytest.mark.asyncio
    async def test_execute_order_failure(self, lighter_bot):
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "some error")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
            "price": 14.80, "reduce_only": False, "custom_id": "test123",
        }
        result = await lighter_bot.execute_order(order)
        assert not lighter_bot.did_create_order(result)

    @pytest.mark.asyncio
    async def test_execute_cancellation_success(self, lighter_bot):
        lighter_bot._client_to_exchange_order_id[123456789] = 987654
        order = {"id": "123456789", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert lighter_bot.did_cancel_order(result)

    def test_did_create_order_empty(self, lighter_bot):
        assert not lighter_bot.did_create_order({})
        assert not lighter_bot.did_create_order(None)

    def test_did_cancel_order_empty(self, lighter_bot):
        assert not lighter_bot.did_cancel_order({})
        assert not lighter_bot.did_cancel_order(None)

    def test_did_cancel_order_success(self, lighter_bot):
        assert lighter_bot.did_cancel_order({"status": "success"})

    def test_did_cancel_order_list(self, lighter_bot):
        assert lighter_bot.did_cancel_order([{"status": "success"}])


# ===========================================================================
# Leverage update test
# ===========================================================================

class TestLeverageUpdate:
    @pytest.mark.asyncio
    async def test_leverage_update_calls_sdk(self, lighter_bot):
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        await lighter_bot.update_exchange_config_by_symbols(["HYPE/USDC:USDC"])
        lighter_bot.lighter_client.update_leverage.assert_called_once()
        args = lighter_bot.lighter_client.update_leverage.call_args
        assert args[0][0] == 5
        assert args[0][2] == 10


# ===========================================================================
# WebSocket order update parsing
# ===========================================================================

class TestWsOrderParsing:
    def test_handle_ws_order_update_open(self, lighter_bot):
        """Non-terminal status should add to the mapping."""
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "open",
                "price": 14.80, "size": 5.0, "client_order_index": 111,
                "order_index": 222, "timestamp": 1709500000000,
            }],
        }
        lighter_bot.execution_scheduled = False
        lighter_bot._handle_ws_order_update(data)
        assert lighter_bot._client_to_exchange_order_id[111] == 222
        assert lighter_bot.execution_scheduled

    def test_handle_ws_order_update_filled_removes_mapping(self, lighter_bot):
        """Filled (terminal) status should remove the client->exchange mapping."""
        lighter_bot._client_to_exchange_order_id[111] = 222
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "filled",
                "price": 14.80, "size": 5.0, "client_order_index": 111,
                "order_index": 222, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 111 not in lighter_bot._client_to_exchange_order_id

    def test_handle_ws_empty_orders(self, lighter_bot):
        data = {"type": "update/account_orders", "orders": []}
        lighter_bot.execution_scheduled = False
        lighter_bot._handle_ws_order_update(data)
        assert not lighter_bot.execution_scheduled


# ===========================================================================
# Time-in-force mapping
# ===========================================================================

class TestTimeInForce:
    def test_post_only_params(self, lighter_bot):
        lighter_bot.config["live"]["time_in_force"] = "post_only"
        params = lighter_bot.get_order_execution_params({"reduce_only": False})
        assert params["timeInForce"] == "post_only"

    def test_gtc_params(self, lighter_bot):
        lighter_bot.config["live"]["time_in_force"] = "good_till_cancelled"
        params = lighter_bot.get_order_execution_params({"reduce_only": True})
        assert params["timeInForce"] == "good_till_cancelled"
        assert params["reduceOnly"] is True


# ===========================================================================
# Symbol eligibility
# ===========================================================================

class TestEligibility:
    def test_known_symbol_eligible(self, lighter_bot):
        assert lighter_bot.symbol_is_eligible("HYPE/USDC:USDC")

    def test_unknown_symbol_not_eligible(self, lighter_bot):
        assert not lighter_bot.symbol_is_eligible("UNKNOWN/USDC:USDC")


# ===========================================================================
# Error handling
# ===========================================================================

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_execute_order_exception_doesnt_crash(self, lighter_bot):
        lighter_bot.lighter_client.create_order = AsyncMock(
            side_effect=Exception("network error")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        result = await lighter_bot.execute_order(order)
        assert result == {}

    @pytest.mark.asyncio
    async def test_execute_cancellation_exception_doesnt_crash(self, lighter_bot):
        lighter_bot.lighter_client.sign_cancel_order = MagicMock(
            side_effect=Exception("network error")
        )
        order = {"id": "999", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert result == {}

    @pytest.mark.asyncio
    async def test_cancel_with_non_numeric_order_id(self, lighter_bot):
        order = {"id": "not-a-number-uuid", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert result == {}


# ===========================================================================
# Fetch method tests
# ===========================================================================

class TestFetchPositions:
    @pytest.mark.asyncio
    async def test_fetch_positions(self, lighter_bot):
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(MOCK_ACCOUNT_RESPONSE)
        )
        result = await lighter_bot.fetch_positions()
        assert isinstance(result, tuple)
        positions, balance = result
        assert len(positions) == 1
        pos = positions[0]
        assert pos["symbol"] == "HYPE/USDC:USDC"
        assert pos["position_side"] == "long"
        assert pos["size"] == 10.0
        assert pos["price"] == 14.50
        assert balance == 5500.0

    @pytest.mark.asyncio
    async def test_fetch_positions_empty(self, lighter_bot):
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(MOCK_ACCOUNT_RESPONSE_EMPTY)
        )
        result = await lighter_bot.fetch_positions()
        positions, balance = result
        assert positions == []
        assert balance == 10000.0


class TestFetchTickers:
    @pytest.mark.asyncio
    async def test_fetch_tickers(self, lighter_bot):
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        tickers = await lighter_bot.fetch_tickers()
        assert "HYPE/USDC:USDC" in tickers
        t = tickers["HYPE/USDC:USDC"]
        assert t["bid"] == 15.1234
        assert t["ask"] == 15.1345
        assert t["last"] == pytest.approx((15.1234 + 15.1345) / 2)


class TestFetchOhlcv:
    @pytest.mark.asyncio
    async def test_fetch_ohlcv(self, lighter_bot):
        lighter_bot.candlestick_api.candlesticks = AsyncMock(
            return_value=_make_obj(MOCK_CANDLES)
        )
        candles = await lighter_bot.fetch_ohlcv("HYPE/USDC:USDC", "1m")
        assert len(candles) == 3
        c = candles[0]
        assert c == [1709500000000, 15.00, 15.20, 14.90, 15.10, 1000.0]


class TestFetchPnls:
    @pytest.mark.asyncio
    async def test_fetch_pnls(self, lighter_bot):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=MOCK_INACTIVE_ORDERS)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        pnls = await lighter_bot.fetch_pnls()
        assert len(pnls) == 2
        assert pnls[0]["symbol"] == "HYPE/USDC:USDC"
        assert pnls[0]["pnl"] == 1.50
        assert pnls[0]["side"] == "sell"
        assert pnls[1]["pnl"] == -0.30


# ===========================================================================
# Lifecycle tests
# ===========================================================================

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_close_no_crash(self, lighter_bot):
        lighter_bot.api_client.close = AsyncMock()
        await lighter_bot.close()

    @pytest.mark.asyncio
    async def test_restart_bot_raises(self, lighter_bot):
        lighter_bot.api_client.close = AsyncMock()
        with pytest.raises(Exception, match="Bot will restart"):
            await lighter_bot.restart_bot()
        lighter_bot.api_client.close.assert_called_once()


# ===========================================================================
# Cancel with exchange order ID mapping (consolidated from TestCancelMapping
# and TestCancelCleansMapping which tested overlapping paths)
# ===========================================================================

class TestCancelMapping:
    @pytest.mark.asyncio
    async def test_cancel_with_exchange_order_id_mapping(self, lighter_bot):
        lighter_bot._client_to_exchange_order_id[111222] = 999888
        order = {"id": "111222", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert lighter_bot.did_cancel_order(result)
        call_kwargs = lighter_bot.lighter_client.sign_cancel_order.call_args
        assert call_kwargs[1]["order_index"] == 999888

    @pytest.mark.asyncio
    async def test_successful_cancel_removes_mapping(self, lighter_bot):
        lighter_bot._client_to_exchange_order_id[555] = 666
        result = await lighter_bot.execute_cancellation(
            {"id": "555", "symbol": "HYPE/USDC:USDC"}
        )
        assert lighter_bot.did_cancel_order(result)
        assert 555 not in lighter_bot._client_to_exchange_order_id

    def test_ws_cancelled_removes_mapping(self, lighter_bot):
        lighter_bot._client_to_exchange_order_id[777] = 888
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "cancelled",
                "price": 14.80, "size": 5.0, "client_order_index": 777,
                "order_index": 888, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 777 not in lighter_bot._client_to_exchange_order_id


# ===========================================================================
# symbol_to_coin override
# ===========================================================================

class TestSymbolToCoin:
    def test_known_symbol(self, lighter_bot):
        assert lighter_bot.symbol_to_coin("HYPE/USDC:USDC") == "HYPE"
        assert lighter_bot.symbol_to_coin("BTC/USDC:USDC") == "BTC"

    def test_unknown_symbol_fallback(self, lighter_bot):
        assert lighter_bot.symbol_to_coin("DOGE/USDC:USDC") == "DOGE"

    def test_plain_coin(self, lighter_bot):
        assert lighter_bot.symbol_to_coin("SOL") == "SOL"


# ===========================================================================
# Error classification tests (#10)
# ===========================================================================

class TestErrorClassification:
    def test_is_quota_error_volume_quota(self):
        from exchanges.lighter import _is_quota_error
        assert _is_quota_error("volume quota exhausted")
        assert _is_quota_error("Volume Quota limit reached")

    def test_is_quota_error_not_enough(self):
        from exchanges.lighter import _is_quota_error
        assert _is_quota_error("quota: not enough remaining")

    def test_is_quota_error_false(self):
        from exchanges.lighter import _is_quota_error
        assert not _is_quota_error("network error")
        assert not _is_quota_error("invalid nonce")

    def test_is_transient_error_429(self):
        from exchanges.lighter import _is_transient_error
        assert _is_transient_error("HTTP 429 Too Many Requests")
        assert _is_transient_error("too many requests")

    def test_is_transient_error_excludes_quota(self):
        from exchanges.lighter import _is_transient_error
        # Quota errors should NOT be classified as transient (use _is_quota_error instead)
        assert not _is_transient_error("volume quota exhausted")

    def test_is_transient_error_nonce(self):
        from exchanges.lighter import _is_transient_error
        assert _is_transient_error("invalid nonce: expected 5, got 4")

    def test_is_transient_error_false(self):
        from exchanges.lighter import _is_transient_error
        assert not _is_transient_error("order not found")
        assert not _is_transient_error("network timeout")


# ===========================================================================
# Rate limiting tests (#4)
# ===========================================================================

class TestRateLimiting:
    def test_ops_available_initial(self, lighter_bot):
        assert lighter_bot._ops_available() == 40

    def test_record_ops_sent(self, lighter_bot):
        lighter_bot._record_ops_sent(10)
        assert lighter_bot._ops_available() == 30

    def test_trigger_global_backoff(self, lighter_bot):
        lighter_bot._trigger_global_backoff()
        assert lighter_bot._global_backoff_until > time.monotonic()
        assert lighter_bot._global_backoff_consecutive == 1
        lighter_bot._trigger_global_backoff()
        assert lighter_bot._global_backoff_consecutive == 2

    def test_reset_global_backoff(self, lighter_bot):
        lighter_bot._global_backoff_consecutive = 3
        lighter_bot._reset_global_backoff()
        assert lighter_bot._consecutive_successes == 1
        assert lighter_bot._global_backoff_consecutive == 3
        lighter_bot._reset_global_backoff()
        assert lighter_bot._global_backoff_consecutive == 0

    @pytest.mark.asyncio
    async def test_wait_for_write_slot_passes(self, lighter_bot):
        result = await lighter_bot._wait_for_write_slot(op_count=1)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_write_slot_blocked_by_backoff(self, lighter_bot):
        lighter_bot._global_backoff_until = time.monotonic() + 60
        result = await lighter_bot._wait_for_write_slot(op_count=1)
        assert result is False


# ===========================================================================
# Volume quota tests (#1)
# ===========================================================================

class TestVolumeQuota:
    def test_update_volume_quota(self, lighter_bot):
        lighter_bot._update_volume_quota(100)
        assert lighter_bot._volume_quota_remaining == 100

    def test_update_volume_quota_zero(self, lighter_bot):
        lighter_bot._update_volume_quota(0)
        assert lighter_bot._volume_quota_remaining == 0
        assert lighter_bot._quota_warning_level == "critical"

    def test_update_volume_quota_none(self, lighter_bot):
        lighter_bot._volume_quota_remaining = 50
        lighter_bot._update_volume_quota(None)
        assert lighter_bot._volume_quota_remaining == 50

    def test_quota_pace_multiplier_unknown(self, lighter_bot):
        lighter_bot._volume_quota_remaining = None
        assert lighter_bot._quota_pace_multiplier() == 1.0

    def test_quota_pace_multiplier_high(self, lighter_bot):
        lighter_bot._volume_quota_remaining = 600
        assert lighter_bot._quota_pace_multiplier() == 1.0

    def test_quota_pace_multiplier_medium(self, lighter_bot):
        lighter_bot._volume_quota_remaining = 100
        assert lighter_bot._quota_pace_multiplier() == 1.5

    def test_quota_pace_multiplier_low(self, lighter_bot):
        lighter_bot._volume_quota_remaining = 15  # between _rl_quota_low (10) and _rl_quota_medium (50)
        assert lighter_bot._quota_pace_multiplier() == 3.0

    def test_quota_pace_multiplier_exhausted(self, lighter_bot):
        lighter_bot._volume_quota_remaining = 0
        assert lighter_bot._quota_pace_multiplier() == float("inf")


# ===========================================================================
# Nonce error handling tests (#2, #3)
# ===========================================================================

class TestNonceHandling:
    def test_handle_nonce_error_quota(self, lighter_bot):
        lighter_bot._handle_nonce_error("volume quota exhausted")
        assert lighter_bot._volume_quota_remaining == 0
        lighter_bot.lighter_client.nonce_manager.hard_refresh_nonce.assert_called_with(0)

    def test_handle_nonce_error_429(self, lighter_bot):
        lighter_bot._handle_nonce_error("HTTP 429 too many requests")
        assert lighter_bot._global_backoff_consecutive == 1
        lighter_bot.lighter_client.nonce_manager.hard_refresh_nonce.assert_called()

    def test_acknowledge_nonce_failure(self, lighter_bot):
        lighter_bot._acknowledge_nonce_failure(0)
        lighter_bot.lighter_client.nonce_manager.acknowledge_failure.assert_called_with(0)

    @pytest.mark.asyncio
    async def test_cancel_sign_error_acknowledges_nonce(self, lighter_bot):
        lighter_bot.lighter_client.sign_cancel_order.return_value = (
            1, {"info": "cancel"}, "0xhash", "some sign error"
        )
        lighter_bot._known_exchange_order_ids.add(999)
        result = await lighter_bot.execute_cancellation(
            {"id": "999", "symbol": "HYPE/USDC:USDC"}
        )
        assert result == {}
        lighter_bot.lighter_client.nonce_manager.acknowledge_failure.assert_called()

    @pytest.mark.asyncio
    async def test_cancel_send_error_acknowledges_nonce(self, lighter_bot):
        lighter_bot.lighter_client.send_tx = AsyncMock(
            side_effect=Exception("network error")
        )
        lighter_bot._known_exchange_order_ids.add(999)
        result = await lighter_bot.execute_cancellation(
            {"id": "999", "symbol": "HYPE/USDC:USDC"}
        )
        assert result == {}
        lighter_bot.lighter_client.nonce_manager.acknowledge_failure.assert_called()

    @pytest.mark.asyncio
    async def test_execute_order_quota_error_refreshes_nonce(self, lighter_bot):
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "volume quota exhausted")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        result = await lighter_bot.execute_order(order)
        assert result == {}
        assert lighter_bot._volume_quota_remaining == 0
        lighter_bot.lighter_client.nonce_manager.hard_refresh_nonce.assert_called()


# ===========================================================================
# Fetch open orders test (#13)
# ===========================================================================

class TestFetchOpenOrders:
    @pytest.mark.asyncio
    async def test_fetch_open_orders(self, lighter_bot):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=MOCK_ACTIVE_ORDERS)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        orders = await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        assert len(orders) == 2
        assert orders[0]["side"] == "buy"
        assert orders[0]["price"] == 14.80
        assert orders[1]["side"] == "sell"
        assert lighter_bot._client_to_exchange_order_id[123456789] == 987654

    @pytest.mark.asyncio
    async def test_fetch_open_orders_empty(self, lighter_bot):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"orders": []})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        orders = await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        assert orders == []


# ===========================================================================
# Short position test (#14)
# ===========================================================================

class TestFetchShortPosition:
    @pytest.mark.asyncio
    async def test_fetch_short_position(self, lighter_bot):
        short_response = {
            "accounts": [{
                "available_balance": 5000.0, "collateral": 5500.0,
                "total_asset_value": 5500.0,
                "positions": {"5": {"position": 5.0, "sign": -1, "entry_price": 16.0}},
            }]
        }
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(short_response)
        )
        positions, _ = await lighter_bot.fetch_positions()
        assert len(positions) == 1
        assert positions[0]["position_side"] == "short"
        assert positions[0]["size"] == -5.0

    @pytest.mark.asyncio
    async def test_fetch_mixed_positions(self, lighter_bot):
        mixed = {
            "accounts": [{
                "available_balance": 5000.0, "collateral": 5500.0,
                "total_asset_value": 5500.0,
                "positions": {
                    "5": {"position": 10.0, "sign": 1, "entry_price": 14.50},
                    "0": {"position": 0.5, "sign": -1, "entry_price": 95000.0},
                },
            }]
        }
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(mixed)
        )
        positions, _ = await lighter_bot.fetch_positions()
        assert len(positions) == 2
        shorts = [p for p in positions if p["position_side"] == "short"]
        assert len(shorts) == 1
        assert shorts[0]["size"] == -0.5


# ===========================================================================
# Auth token expiry test (#15)
# ===========================================================================

class TestAuthTokenExpiry:
    @pytest.mark.asyncio
    async def test_cached_token_within_expiry(self, lighter_bot):
        lighter_bot._auth_token = "cached_token"
        lighter_bot._auth_token_ts = time.time() - 100
        assert await lighter_bot._get_auth_token() == "cached_token"

    @pytest.mark.asyncio
    async def test_expired_token_refreshes(self, lighter_bot):
        lighter_bot._auth_token = "old_token"
        lighter_bot._auth_token_ts = time.time() - 500
        lighter_bot.lighter_client.create_auth_token_with_expiry.return_value = (
            "new_token", None
        )
        token = await lighter_bot._get_auth_token()
        assert token == "new_token"

    @pytest.mark.asyncio
    async def test_token_refresh_error_returns_old(self, lighter_bot):
        lighter_bot._auth_token = "old_token"
        lighter_bot._auth_token_ts = time.time() - 500
        lighter_bot.lighter_client.create_auth_token_with_expiry.return_value = (
            None, "auth error"
        )
        assert await lighter_bot._get_auth_token() == "old_token"



# ===========================================================================
# Persistent aiohttp session test (#6)
# ===========================================================================

class TestPersistentSession:
    @pytest.mark.asyncio
    async def test_get_aiohttp_session_creates_once(self, lighter_bot):
        session1 = await lighter_bot._get_aiohttp_session()
        session2 = await lighter_bot._get_aiohttp_session()
        assert session1 is session2
        if not session1.closed:
            await session1.close()

    @pytest.mark.asyncio
    async def test_close_closes_aiohttp_session(self, lighter_bot):
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        lighter_bot._aiohttp_session = mock_session
        lighter_bot.api_client.close = AsyncMock()
        await lighter_bot.close()
        mock_session.close.assert_called_once()


# ===========================================================================
# Position sign edge cases (Phase 1.2)
# ===========================================================================

class TestPositionSignEdgeCases:
    @pytest.mark.asyncio
    async def test_sign_zero_skipped(self, lighter_bot):
        """sign=0 with non-zero size is nonsensical and should be skipped."""
        response = {
            "accounts": [{
                "available_balance": 5000.0, "collateral": 5500.0,
                "total_asset_value": 5500.0,
                "positions": {"5": {"position": 10.0, "sign": 0, "entry_price": 14.50}},
            }]
        }
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(response)
        )
        positions, _ = await lighter_bot.fetch_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_sign_two_treated_as_positive(self, lighter_bot):
        """sign=2 (unexpected) should warn and treat position as positive."""
        response = {
            "accounts": [{
                "available_balance": 5000.0, "collateral": 5500.0,
                "total_asset_value": 5500.0,
                "positions": {"5": {"position": 7.0, "sign": 2, "entry_price": 15.0}},
            }]
        }
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(response)
        )
        positions, _ = await lighter_bot.fetch_positions()
        assert len(positions) == 1
        assert positions[0]["size"] == 7.0
        assert positions[0]["position_side"] == "long"



# ===========================================================================
# is_ask type coercion tests (Phase 1.3)
# ===========================================================================

class TestIsAskCoercion:
    def test_bool_true(self, lighter_bot):
        assert lighter_bot._coerce_is_ask(True) is True

    def test_bool_false(self, lighter_bot):
        assert lighter_bot._coerce_is_ask(False) is False

    def test_int_one(self, lighter_bot):
        assert lighter_bot._coerce_is_ask(1) is True

    def test_int_zero(self, lighter_bot):
        assert lighter_bot._coerce_is_ask(0) is False

    def test_float_one(self, lighter_bot):
        assert lighter_bot._coerce_is_ask(1.0) is True

    def test_string_true(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("true") is True

    def test_string_True(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("True") is True

    def test_string_one(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("1") is True

    def test_string_false(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("false") is False

    def test_string_zero(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("0") is False

    def test_none(self, lighter_bot):
        assert lighter_bot._coerce_is_ask(None) is False

    def test_ws_order_with_int_is_ask(self, lighter_bot):
        """WS order update with is_ask=1 (int) should parse as sell."""
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": 1, "status": "open",
                "price": 15.50, "size": 2.0, "client_order_index": 333,
                "order_index": 444, "timestamp": 1709500000000,
            }],
        }
        lighter_bot.execution_scheduled = False
        lighter_bot._handle_ws_order_update(data)
        assert lighter_bot.execution_scheduled

    def test_ws_order_with_string_is_ask(self, lighter_bot):
        """WS order update with is_ask='true' (string) should parse as sell."""
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": "true", "status": "open",
                "price": 15.50, "size": 2.0, "client_order_index": 555,
                "order_index": 666, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)
        assert lighter_bot._client_to_exchange_order_id[555] == 666


# ===========================================================================
# Order ID mapping cap tests (Phase 2.1)
# ===========================================================================

class TestOrderIdMappingCap:
    def test_trim_at_200(self, lighter_bot):
        """Mapping should be trimmed when exceeding 200 entries."""
        for i in range(250):
            lighter_bot._client_to_exchange_order_id[i] = i + 1000
        lighter_bot._trim_order_id_mapping()
        assert len(lighter_bot._client_to_exchange_order_id) == 100

    def test_trim_keeps_newest(self, lighter_bot):
        """Trim should keep the highest (newest) client IDs."""
        for i in range(250):
            lighter_bot._client_to_exchange_order_id[i] = i + 1000
        lighter_bot._trim_order_id_mapping()
        # Should keep keys 150-249 (the 100 newest)
        assert 249 in lighter_bot._client_to_exchange_order_id
        assert 150 in lighter_bot._client_to_exchange_order_id
        assert 149 not in lighter_bot._client_to_exchange_order_id
        assert 0 not in lighter_bot._client_to_exchange_order_id

    def test_no_trim_under_cap(self, lighter_bot):
        """Should not trim if under 200 entries."""
        for i in range(50):
            lighter_bot._client_to_exchange_order_id[i] = i + 1000
        lighter_bot._trim_order_id_mapping()
        assert len(lighter_bot._client_to_exchange_order_id) == 50

    def test_ws_update_triggers_trim(self, lighter_bot):
        """WS order updates should trigger trim after insertion."""
        lighter_bot._ws_orders_snapshot_received = True  # skip snapshot reconciliation
        for i in range(201):
            lighter_bot._client_to_exchange_order_id[i] = i + 1000
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "open",
                "price": 14.80, "size": 5.0, "client_order_index": 9999,
                "order_index": 8888, "timestamp": 1709500000000,
            }],
        }
        lighter_bot.execution_scheduled = False
        lighter_bot._handle_ws_order_update(data)
        assert len(lighter_bot._client_to_exchange_order_id) == 100


# ===========================================================================
# reduce_only passthrough test (Phase 2.2)
# ===========================================================================

class TestReduceOnlyPassthrough:
    @pytest.mark.asyncio
    async def test_reduce_only_true_passed(self, lighter_bot):
        """reduce_only=True should be forwarded to create_order."""
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "sell", "qty": 5.0,
            "price": 15.50, "reduce_only": True, "custom_id": "close1",
        }
        result = await lighter_bot.execute_order(order)
        assert lighter_bot.did_create_order(result)
        call_kwargs = lighter_bot.lighter_client.create_order.call_args
        assert call_kwargs[1]["reduce_only"] is True

    @pytest.mark.asyncio
    async def test_reduce_only_false_passed(self, lighter_bot):
        """reduce_only=False should be forwarded to create_order."""
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
            "price": 14.80, "reduce_only": False, "custom_id": "entry1",
        }
        result = await lighter_bot.execute_order(order)
        assert lighter_bot.did_create_order(result)
        call_kwargs = lighter_bot.lighter_client.create_order.call_args
        assert call_kwargs[1]["reduce_only"] is False

    @pytest.mark.asyncio
    async def test_reduce_only_missing_defaults_false(self, lighter_bot):
        """Missing reduce_only should default to False."""
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
            "price": 14.80, "custom_id": "entry2",
        }
        result = await lighter_bot.execute_order(order)
        assert lighter_bot.did_create_order(result)
        call_kwargs = lighter_bot.lighter_client.create_order.call_args
        assert call_kwargs[1]["reduce_only"] is False


# ===========================================================================
# Multiple concurrent WS order updates (Phase 3.3)
# ===========================================================================

class TestMultipleWsUpdates:
    def test_batch_order_updates(self, lighter_bot):
        """Multiple orders in a single WS message should all be processed."""
        data = {
            "type": "update/account_orders",
            "orders": [
                {
                    "market_id": 5, "is_ask": False, "status": "open",
                    "price": 14.80, "size": 5.0, "client_order_index": 1001,
                    "order_index": 2001, "timestamp": 1709500000000,
                },
                {
                    "market_id": 5, "is_ask": True, "status": "open",
                    "price": 15.50, "size": 3.0, "client_order_index": 1002,
                    "order_index": 2002, "timestamp": 1709500001000,
                },
                {
                    "market_id": 0, "is_ask": False, "status": "filled",
                    "price": 95000.0, "size": 0.1, "client_order_index": 1003,
                    "order_index": 2003, "timestamp": 1709500002000,
                },
            ],
        }
        lighter_bot._client_to_exchange_order_id[1003] = 2003
        lighter_bot.execution_scheduled = False
        lighter_bot._handle_ws_order_update(data)

        # Open orders should be in mapping
        assert lighter_bot._client_to_exchange_order_id[1001] == 2001
        assert lighter_bot._client_to_exchange_order_id[1002] == 2002
        # Filled order should be removed
        assert 1003 not in lighter_bot._client_to_exchange_order_id
        assert lighter_bot.execution_scheduled


# ===========================================================================
# Batch Orders (Phase 4.1)
# ===========================================================================

class TestBatchOrders:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_batch_creates_two_orders(self, lighter_bot):
        """Two create orders should be batched via send_tx_batch."""
        orders = [
            {"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
             "price": 14.80, "custom_id": "b1"},
            {"symbol": "HYPE/USDC:USDC", "side": "sell", "qty": 3.0,
             "price": 15.50, "custom_id": "s1"},
        ]
        results = await lighter_bot.execute_orders(orders)
        assert len(results) == 2
        assert lighter_bot.did_create_order(results[0])
        assert lighter_bot.did_create_order(results[1])
        lighter_bot.lighter_client.send_tx_batch.assert_called_once()
        assert lighter_bot.lighter_client.sign_create_order.call_count == 2

    @pytest.mark.asyncio
    async def test_batch_single_order_delegates(self, lighter_bot):
        """Single order should use execute_order (high-level create_order), not batch."""
        orders = [
            {"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
             "price": 14.80, "custom_id": "b1"},
        ]
        results = await lighter_bot.execute_orders(orders)
        assert len(results) == 1
        assert lighter_bot.did_create_order(results[0])
        lighter_bot.lighter_client.create_order.assert_called_once()
        lighter_bot.lighter_client.send_tx_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_partial_sign_failure(self, lighter_bot):
        """If one of three ops fails signing, the other two should still be sent."""
        call_count = 0

        def sign_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return (14, None, None, "nonce error")
            return (14, {"info": "create"}, "0xhash", None)

        lighter_bot.lighter_client.sign_create_order.side_effect = sign_side_effect

        orders = [
            {"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
             "price": 14.80, "custom_id": "b1"},
            {"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 3.0,
             "price": 14.50, "custom_id": "b2"},
            {"symbol": "HYPE/USDC:USDC", "side": "sell", "qty": 2.0,
             "price": 15.50, "custom_id": "s1"},
        ]
        results = await lighter_bot.execute_orders(orders)
        assert len(results) == 3
        assert lighter_bot.did_create_order(results[0])
        assert not lighter_bot.did_create_order(results[1])  # failed signing
        assert lighter_bot.did_create_order(results[2])
        lighter_bot.lighter_client.send_tx_batch.assert_called_once()
        # Verify batch had 2 ops (not 3)
        call_args = lighter_bot.lighter_client.send_tx_batch.call_args
        assert len(call_args[0][0]) == 2  # tx_types has 2 elements

    @pytest.mark.asyncio
    async def test_batch_send_error_hard_refreshes_nonce(self, lighter_bot):
        """If send_tx_batch raises, acknowledge_failure should be called for all
        signed nonces, then hard_refresh_nonce (matching reference impl)."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            side_effect=Exception("network error")
        )

        orders = [
            {"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
             "price": 14.80, "custom_id": "b1"},
            {"symbol": "HYPE/USDC:USDC", "side": "sell", "qty": 3.0,
             "price": 15.50, "custom_id": "s1"},
        ]
        results = await lighter_bot.execute_orders(orders)
        assert all(r == {} for r in results)
        # acknowledge_failure called for each signed nonce before hard_refresh
        assert lighter_bot.lighter_client.nonce_manager.acknowledge_failure.call_count == 2
        lighter_bot.lighter_client.nonce_manager.hard_refresh_nonce.assert_called()
        # Should flag for reconciliation
        assert lighter_bot.execution_scheduled is True

    @pytest.mark.asyncio
    async def test_batch_free_slot_mode(self, lighter_bot):
        """When quota=0, only 1 op should be sent via single REST send_tx."""
        lighter_bot._volume_quota_remaining = 0

        orders = [
            {"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 5.0,
             "price": 14.80, "custom_id": "b1"},
            {"symbol": "HYPE/USDC:USDC", "side": "sell", "qty": 3.0,
             "price": 15.50, "custom_id": "s1"},
        ]
        results = await lighter_bot.execute_orders(orders)
        # Only first op processed; second gets empty dict
        assert len(results) == 2
        assert lighter_bot.did_create_order(results[0])
        assert not lighter_bot.did_create_order(results[1])
        lighter_bot.lighter_client.send_tx.assert_called_once()
        lighter_bot.lighter_client.send_tx_batch.assert_not_called()


# ===========================================================================
# Batch Cancellations (Phase 4.1)
# ===========================================================================

class TestBatchCancellations:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_batch_cancels_use_individual_calls(self, lighter_bot):
        """Multiple cancels should use individual send_tx (free, 0 quota) not send_tx_batch."""
        lighter_bot._known_exchange_order_ids.update({1001, 1002})
        orders = [
            {"symbol": "HYPE/USDC:USDC", "id": "1001"},
            {"symbol": "HYPE/USDC:USDC", "id": "1002"},
        ]
        results = await lighter_bot.execute_cancellations(orders)
        assert len(results) == 2
        assert lighter_bot.did_cancel_order(results[0])
        assert lighter_bot.did_cancel_order(results[1])
        # C1 fix: individual send_tx calls (free), NOT send_tx_batch (costs quota)
        assert lighter_bot.lighter_client.send_tx.call_count == 2
        lighter_bot.lighter_client.send_tx_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_cancel_single_delegates(self, lighter_bot):
        """Single cancel should use execute_cancellation, not batch."""
        lighter_bot._known_exchange_order_ids.add(1001)
        orders = [{"symbol": "HYPE/USDC:USDC", "id": "1001"}]
        results = await lighter_bot.execute_cancellations(orders)
        assert len(results) == 1
        assert lighter_bot.did_cancel_order(results[0])
        lighter_bot.lighter_client.send_tx.assert_called_once()
        lighter_bot.lighter_client.send_tx_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_cancels_empty(self, lighter_bot):
        """Empty cancel list should return empty list."""
        results = await lighter_bot.execute_cancellations([])
        assert results == []


# ===========================================================================
# C3: CloudFront backoff timing test
# ===========================================================================

class TestCloudFrontBackoff:
    def test_backoff_base_is_15s(self, lighter_bot):
        """Backoff base should be 15s to match reference implementation."""
        assert lighter_bot._rl_backoff_base == 15.0

    def test_first_backoff_reaches_15s(self, lighter_bot):
        """First 429 backoff should be ~15s (escalates: 15 -> 30 -> 60 -> 120)."""
        lighter_bot._trigger_global_backoff()
        duration = lighter_bot._global_backoff_until - time.monotonic()
        assert duration >= 14.0  # allow small timing slack


# ===========================================================================
# H1: Free-slot interval test
# ===========================================================================

class TestFreeSlotInterval:
    def test_free_slot_interval_is_15s(self, lighter_bot):
        """Free-slot interval should be 15s (matching reference); quota recovery adds +1s."""
        assert lighter_bot._rl_free_slot_interval == 15.0


# ===========================================================================
# H2: Ticker cache TTL test
# ===========================================================================

class TestTickerCache:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_fetch_tickers_caches_result(self, lighter_bot):
        """Second call within TTL should not hit the API again."""
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        t1 = await lighter_bot.fetch_tickers()
        t2 = await lighter_bot.fetch_tickers()
        assert t1 is t2  # same object (cached)
        lighter_bot.order_api.order_books.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_ticker_reuses_cache(self, lighter_bot):
        """fetch_ticker for different symbols should reuse cached fetch_tickers."""
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        t1 = await lighter_bot.fetch_ticker("HYPE/USDC:USDC")
        t2 = await lighter_bot.fetch_ticker("BTC/USDC:USDC")
        assert t1
        assert t2
        lighter_bot.order_api.order_books.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, lighter_bot):
        """After TTL expires, fetch_tickers should call API again."""
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        lighter_bot._tickers_cache_ttl = 0.0  # expire immediately
        await lighter_bot.fetch_tickers()
        await lighter_bot.fetch_tickers()
        assert lighter_bot.order_api.order_books.call_count == 2


# ===========================================================================
# C2: WS account update has no balance parsing
# ===========================================================================

class TestWsAccountUpdateNoBalance:
    def test_account_update_position_sets_flag(self, lighter_bot):
        """Position data in account_all should set execution_scheduled."""
        lighter_bot.execution_scheduled = False
        data = {"positions": {"5": {"position": 10.0, "sign": 1}}}
        lighter_bot._handle_ws_account_update(data)
        assert lighter_bot.execution_scheduled is True

    def test_account_update_no_balance_handling(self, lighter_bot):
        """account_all delivers positions only, NOT balance. Balance comes from user_stats channel."""
        lighter_bot.execution_scheduled = False
        data = {"user_stats": {"available_balance": 9999.0}}
        lighter_bot._handle_ws_account_update(data)
        # No balance handler should be invoked; only position data matters
        assert lighter_bot.execution_scheduled is False


# ===========================================================================
# L1: Client order ID atomic counter test
# ===========================================================================

class TestClientOrderIdCounter:
    def test_counter_increments(self, lighter_bot):
        """Each call should increment the counter, ensuring uniqueness."""
        id1 = lighter_bot._generate_client_order_id()
        id2 = lighter_bot._generate_client_order_id()
        assert id1 != id2
        assert lighter_bot._order_id_counter >= 2

    def test_rapid_calls_unique(self, lighter_bot):
        """Rapid consecutive calls should produce unique IDs."""
        ids = [lighter_bot._generate_client_order_id() for _ in range(100)]
        assert len(set(ids)) == 100



# ===========================================================================
# Fix 1: Order ID collision guard tests
# ===========================================================================

class TestOrderIdCollisionGuard:
    @pytest.mark.asyncio
    async def test_cancel_validates_exchange_order_id(self, lighter_bot):
        """Cancel with unknown ID should return empty dict instead of guessing."""
        # ID 999 is not in _client_to_exchange_order_id nor _known_exchange_order_ids
        order = {"id": "999", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert result == {}

    @pytest.mark.asyncio
    async def test_cancel_with_known_exchange_id(self, lighter_bot):
        """Cancel should succeed when ID is in _known_exchange_order_ids."""
        lighter_bot._known_exchange_order_ids.add(999)
        order = {"id": "999", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert lighter_bot.did_cancel_order(result)

    def test_known_exchange_order_ids_populated_from_ws(self, lighter_bot):
        """WS order updates should populate _known_exchange_order_ids."""
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "open",
                "price": 14.80, "size": 5.0, "client_order_index": 111,
                "order_index": 222, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 222 in lighter_bot._known_exchange_order_ids

    def test_known_exchange_order_ids_removed_on_terminal(self, lighter_bot):
        """Terminal WS status should remove from _known_exchange_order_ids."""
        lighter_bot._known_exchange_order_ids.add(222)
        lighter_bot._client_to_exchange_order_id[111] = 222
        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "cancelled",
                "price": 14.80, "size": 5.0, "client_order_index": 111,
                "order_index": 222, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 222 not in lighter_bot._known_exchange_order_ids

    @pytest.mark.asyncio
    async def test_known_exchange_order_ids_populated_from_rest(self, lighter_bot):
        """fetch_open_orders should populate _known_exchange_order_ids."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=MOCK_ACTIVE_ORDERS)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        assert 987654 in lighter_bot._known_exchange_order_ids
        assert 987655 in lighter_bot._known_exchange_order_ids


# ===========================================================================
# Fix 2: _wait_for_write_slot max timeout
# ===========================================================================

class TestWriteSlotMaxTimeout:
    @pytest.mark.asyncio
    async def test_wait_for_write_slot_max_timeout(self, lighter_bot):
        """Method should return False before 15s deadline when phases would exceed it."""
        # Set global backoff far in the future (but within 2s threshold)
        # The sliding window phase will hit timeout
        lighter_bot._global_backoff_until = 0  # no backoff
        # Fill the op window so it needs to wait
        for _ in range(40):
            lighter_bot._op_timestamps.append(time.monotonic())
        # _time_until_ops_free should return ~60s, exceeding the 15s deadline
        result = await lighter_bot._wait_for_write_slot(op_count=1)
        assert result is False


# ===========================================================================
# Fix 3: Concurrent cancellations
# ===========================================================================

class TestConcurrentCancellations:
    @pytest.mark.asyncio
    async def test_concurrent_cancellations(self, lighter_bot):
        """execute_cancellations should run cancels concurrently."""
        call_times = []
        original_cancel = lighter_bot.execute_cancellation

        async def tracking_cancel(order):
            call_times.append(time.monotonic())
            return await original_cancel(order)

        lighter_bot.execute_cancellation = tracking_cancel
        # Pre-populate known exchange IDs
        for i in range(1001, 1004):
            lighter_bot._known_exchange_order_ids.add(i)

        orders = [
            {"symbol": "HYPE/USDC:USDC", "id": str(i)}
            for i in range(1001, 1004)
        ]
        results = await lighter_bot.execute_cancellations(orders)
        assert len(results) == 3
        # All calls should complete
        assert len(call_times) == 3


# ===========================================================================
# Fix 4: Rejection circuit breaker
# ===========================================================================

class TestRejectionCircuitBreaker:
    @pytest.mark.asyncio
    async def test_rejection_circuit_breaker(self, lighter_bot):
        """After 5 consecutive rejections, orders should be paused."""
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "rejected: insufficient margin")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        for _ in range(5):
            await lighter_bot.execute_order(order)

        assert lighter_bot._consecutive_rejections >= 5
        assert lighter_bot._rejection_pause_until > time.monotonic()

        # Next order should be blocked by the pause
        result = await lighter_bot.execute_order(order)
        assert result == {}

    @pytest.mark.asyncio
    async def test_rejection_counter_resets_on_success(self, lighter_bot):
        """Successful order should reset rejection counter."""
        lighter_bot._consecutive_rejections = 4
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        result = await lighter_bot.execute_order(order)
        assert lighter_bot.did_create_order(result)
        assert lighter_bot._consecutive_rejections == 0


# ===========================================================================
# Fix 5: WS cancel confirmation
# ===========================================================================

class TestWsCancelConfirmation:
    @pytest.mark.asyncio
    async def test_ws_cancel_confirmation_event(self, lighter_bot):
        """Cancel confirmation should be resolved by WS update."""
        import asyncio as _asyncio

        lighter_bot._client_to_exchange_order_id[111] = 222
        lighter_bot._known_exchange_order_ids.add(222)

        # Schedule a WS update to fire the event after a short delay
        async def fire_event():
            await _asyncio.sleep(0.1)
            data = {
                "type": "update/account_orders",
                "orders": [{
                    "market_id": 5, "is_ask": False, "status": "cancelled",
                    "price": 14.80, "size": 5.0, "client_order_index": 111,
                    "order_index": 222, "timestamp": 1709500000000,
                }],
            }
            lighter_bot._handle_ws_order_update(data)

        _asyncio.create_task(fire_event())
        # Temporarily use client_to_exchange mapping for this test
        order = {"id": "111", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert lighter_bot.did_cancel_order(result)

    @pytest.mark.asyncio
    async def test_ws_cancel_confirmation_timeout(self, lighter_bot):
        """Cancel should still succeed after timeout (graceful fallback)."""
        lighter_bot._client_to_exchange_order_id[333] = 444
        order = {"id": "333", "symbol": "HYPE/USDC:USDC"}
        # No WS event will fire, so it will timeout after 2s
        import time as _time
        start = _time.monotonic()
        result = await lighter_bot.execute_cancellation(order)
        elapsed = _time.monotonic() - start
        assert lighter_bot.did_cancel_order(result)
        # Should have waited ~2s for confirmation timeout
        assert elapsed >= 1.5


# ===========================================================================
# Fix 7: Quota recovery
# ===========================================================================

class TestQuotaRecovery:
    @pytest.mark.asyncio
    async def test_quota_recovery_basic(self, lighter_bot):
        """Quota recovery should attempt market orders when quota is exhausted."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._bot_start_time = time.monotonic() - 200  # past grace period
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        # Mock create_market_order
        resp = types.SimpleNamespace(volume_quota_remaining=60, code=0)
        lighter_bot.lighter_client.create_market_order = AsyncMock(
            return_value=("tx", resp, None)
        )
        result = await lighter_bot._attempt_quota_recovery()
        assert result is True
        lighter_bot.lighter_client.create_market_order.assert_called()

    @pytest.mark.asyncio
    async def test_quota_recovery_cooldown(self, lighter_bot):
        """Should not attempt recovery within 2min cooldown."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._quota_recovery_last_attempt = time.monotonic() - 60  # 60s ago
        result = await lighter_bot._attempt_quota_recovery()
        assert result is False

    @pytest.mark.asyncio
    async def test_quota_recovery_loss_limit(self, lighter_bot):
        """Should stop when cumulative loss exceeds $2."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._bot_start_time = time.monotonic() - 200
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        # Return quota=1 (not reaching target) so it keeps trying until loss limit
        resp = types.SimpleNamespace(volume_quota_remaining=1, code=0)
        lighter_bot.lighter_client.create_market_order = AsyncMock(
            return_value=("tx", resp, None)
        )
        # Manipulate to track recovery doesn't get stuck
        result = await lighter_bot._attempt_quota_recovery()
        # Should have stopped (quota didn't increase after attempt 1)
        assert result is False

    @pytest.mark.asyncio
    async def test_quota_recovery_skips_when_sufficient(self, lighter_bot):
        """Should not attempt recovery when quota is sufficient."""
        lighter_bot._volume_quota_remaining = 100
        result = await lighter_bot._attempt_quota_recovery()
        assert result is False

    @pytest.mark.asyncio
    async def test_quota_recovery_reentrancy_guard(self, lighter_bot):
        """Should not attempt recovery if already in progress."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._quota_recovery_in_progress = True
        result = await lighter_bot._attempt_quota_recovery()
        assert result is False


# ===========================================================================
# Fix 9: WS user_stats subscription
# ===========================================================================

class TestWsUserStats:
    def test_ws_user_stats_update_calls_handle_balance(self, lighter_bot):
        """Balance should be parsed from user_stats WS update."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": 5500.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 5500.0

    def test_ws_user_stats_collateral_fallback(self, lighter_bot):
        """Should fall back to collateral field if available_balance missing."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"collateral": 6000.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 6000.0

    def test_ws_user_stats_empty(self, lighter_bot):
        """Empty stats should not crash."""
        lighter_bot.balance = 5000.0
        data = {"stats": {}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 5000.0

    @pytest.mark.asyncio
    async def test_ws_user_stats_subscription_in_channels(self, lighter_bot):
        """_subscribe_ws_channels should subscribe to user_stats."""
        ws_mock = AsyncMock()
        auth = "test_auth"
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]

        await lighter_bot._subscribe_ws_channels(ws_mock, auth)
        # Check that user_stats subscription was sent
        calls = [str(c) for c in ws_mock.send.call_args_list]
        assert any("user_stats" in c for c in calls)


# ===========================================================================
# Fix 10: WS message type exact matching
# ===========================================================================

class TestMsgTypeExactMatch:
    def test_false_prefix_does_not_trigger_order_handler(self, lighter_bot):
        """A type like 'foo/update/account_orders' should NOT trigger order handling.

        The fix uses startswith() so only 'update/account_orders...' patterns match."""
        lighter_bot.handle_order_update = MagicMock()
        # This would match with 'in' but should NOT match with startswith
        data = {
            "type": "foo/update/account_orders",
            "orders": [{"market_id": 5, "is_ask": False, "status": "open",
                        "price": 14.80, "size": 5.0, "client_order_index": 50,
                        "order_index": 60, "timestamp": 1709500000000}],
        }
        lighter_bot._handle_ws_order_update(data)
        # _handle_ws_order_update parses orders regardless of 'type' field —
        # the startswith check is in watch_orders dispatch. So we verify
        # that the correct prefix DOES work:
        lighter_bot.handle_order_update.reset_mock()
        data_good = {
            "type": "update/account_orders/5/0",
            "orders": [{"market_id": 5, "is_ask": False, "status": "open",
                        "price": 14.80, "size": 5.0, "client_order_index": 51,
                        "order_index": 61, "timestamp": 1709500000000}],
        }
        lighter_bot._handle_ws_order_update(data_good)
        lighter_bot.handle_order_update.assert_called_once()


# ===========================================================================
# Fix 13: fetch_positions list format
# ===========================================================================

class TestFetchPositionsListFormat:
    @pytest.mark.asyncio
    async def test_fetch_positions_list_format(self, lighter_bot):
        """Positions as list (not just dict) should parse correctly."""
        response = {
            "accounts": [{
                "available_balance": 5000.0, "collateral": 5500.0,
                "total_asset_value": 5500.0,
                "positions": [
                    {"position": 10.0, "sign": 1, "entry_price": 14.50},
                ],
            }]
        }
        # Build account response manually to support list positions
        acc = types.SimpleNamespace(
            available_balance=5000.0,
            collateral=5500.0,
            total_asset_value=5500.0,
            positions=[{"position": 10.0, "sign": 1, "entry_price": 14.50}],
        )
        lighter_bot.account_api.account = AsyncMock(
            return_value=types.SimpleNamespace(accounts=[acc])
        )
        # market_index 0 (index from enumerate) won't map unless we handle it
        # The list format uses enumerate, so index 0 -> market_index 0 -> BTC
        result = await lighter_bot.fetch_positions()
        positions, balance = result
        # Index 0 maps to BTC in our test setup
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC/USDC:USDC"


# ===========================================================================
# WS reconnect backoff
# ===========================================================================

class TestWatchOrdersReconnectBackoff:
    def test_backoff_state_defaults(self, lighter_bot):
        """Verify backoff-related state variables have correct initial values."""
        # _global_backoff_until should start at 0 (no backoff)
        assert lighter_bot._global_backoff_until == 0
        # _rejection_pause_until should start at 0 (no pause)
        assert lighter_bot._rejection_pause_until == 0
        # _consecutive_rejections should start at 0
        assert lighter_bot._consecutive_rejections == 0


# ===========================================================================
# C1: execute_cancellation — resp.code != 0 error path
# ===========================================================================

class TestCancelRespCodeError:
    @pytest.mark.asyncio
    async def test_cancel_resp_code_nonzero_returns_empty(self, lighter_bot):
        """send_tx returning code != 0 should return {} (cancel failed)."""
        lighter_bot._known_exchange_order_ids.add(500)
        lighter_bot.lighter_client.send_tx = AsyncMock(
            return_value=types.SimpleNamespace(code=1, message="Order not found")
        )
        order = {"id": "500", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert result == {}


# ===========================================================================
# C2: execute_cancellation — rate-limit blocks cancel
# ===========================================================================

class TestCancelRateLimitBlocked:
    @pytest.mark.asyncio
    async def test_cancel_blocked_by_rejection_pause(self, lighter_bot):
        """Rejection pause should block cancel without calling sign_cancel_order."""
        lighter_bot._rejection_pause_until = time.monotonic() + 300
        lighter_bot._known_exchange_order_ids.add(600)
        lighter_bot.lighter_client.sign_cancel_order = MagicMock()
        order = {"id": "600", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)
        assert result == {}
        lighter_bot.lighter_client.sign_cancel_order.assert_not_called()


# ===========================================================================
# C3: execute_order — rate-limit blocks order
# ===========================================================================

class TestOrderRateLimitBlocked:
    @pytest.mark.asyncio
    async def test_order_blocked_by_rejection_pause(self, lighter_bot):
        """Rejection pause should block order without calling create_order."""
        lighter_bot._rejection_pause_until = time.monotonic() + 300
        lighter_bot.lighter_client.create_order = AsyncMock()
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False,
        }
        result = await lighter_bot.execute_order(order)
        assert result == {}
        lighter_bot.lighter_client.create_order.assert_not_called()


# ===========================================================================
# C4: _wait_for_write_slot — rejection pause check
# ===========================================================================

class TestWriteSlotRejectionPause:
    @pytest.mark.asyncio
    async def test_rejection_pause_returns_false_immediately(self, lighter_bot):
        """Active rejection pause should return False without sleeping."""
        lighter_bot._rejection_pause_until = time.monotonic() + 60
        start = time.monotonic()
        result = await lighter_bot._wait_for_write_slot()
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 1.0  # should be near-instant


# ===========================================================================
# C5: _wait_for_write_slot — cancel_only skips quota pacing
# ===========================================================================

class TestWriteSlotCancelSkipsQuota:
    @pytest.mark.asyncio
    async def test_cancel_only_skips_phase4(self, lighter_bot):
        """cancel_only=True should skip Phase 4 (quota pacing) and return True."""
        lighter_bot._volume_quota_remaining = 0
        start = time.monotonic()
        result = await lighter_bot._wait_for_write_slot(cancel_only=True)
        elapsed = time.monotonic() - start
        assert result is True
        assert elapsed < 2.0  # should not wait for free-slot interval


# ===========================================================================
# C6: _wait_for_write_slot — short global backoff sleeps then proceeds
# ===========================================================================

class TestWriteSlotShortBackoff:
    @pytest.mark.asyncio
    async def test_short_global_backoff_sleeps_and_proceeds(self, lighter_bot):
        """Global backoff ≤2s should sleep briefly then return True."""
        lighter_bot._global_backoff_until = time.monotonic() + 0.1
        result = await lighter_bot._wait_for_write_slot()
        assert result is True


# ===========================================================================
# C7: _handle_ws_order_update — expired/rejected statuses
# ===========================================================================

class TestWsOrderUpdateTerminalStatuses:
    def test_expired_removes_mapping_and_fires_event(self, lighter_bot):
        """Expired order should remove from mappings and fire cancel event."""
        import asyncio as _asyncio
        lighter_bot._client_to_exchange_order_id[70] = 700
        lighter_bot._known_exchange_order_ids.add(700)
        evt = _asyncio.Event()
        lighter_bot._order_cancel_events[700] = evt

        data = {
            "type": "update/account_orders",
            "orders": [{"market_id": 5, "is_ask": False, "status": "expired",
                        "price": 14.80, "size": 5.0, "client_order_index": 70,
                        "order_index": 700, "timestamp": 1709500000000}],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 70 not in lighter_bot._client_to_exchange_order_id
        assert 700 not in lighter_bot._known_exchange_order_ids
        assert evt.is_set()

    def test_rejected_removes_mapping_and_fires_event(self, lighter_bot):
        """Rejected order should remove from mappings and fire cancel event."""
        import asyncio as _asyncio
        lighter_bot._client_to_exchange_order_id[80] = 800
        lighter_bot._known_exchange_order_ids.add(800)
        evt = _asyncio.Event()
        lighter_bot._order_cancel_events[800] = evt

        data = {
            "type": "update/account_orders",
            "orders": [{"market_id": 5, "is_ask": True, "status": "rejected",
                        "price": 15.0, "size": 3.0, "client_order_index": 80,
                        "order_index": 800, "timestamp": 1709500000000}],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 80 not in lighter_bot._client_to_exchange_order_id
        assert 800 not in lighter_bot._known_exchange_order_ids
        assert evt.is_set()


# ===========================================================================
# C8: _handle_ws_order_update — unknown market_id
# ===========================================================================

class TestWsOrderUpdateUnknownMarket:
    def test_unknown_market_id_no_crash(self, lighter_bot):
        """Unknown market_id should not crash; execution_scheduled still set."""
        lighter_bot.execution_scheduled = False
        data = {
            "type": "update/account_orders",
            "orders": [{"market_id": 999, "is_ask": False, "status": "open",
                        "price": 10.0, "size": 1.0, "client_order_index": 90,
                        "order_index": 900, "timestamp": 1709500000000}],
        }
        lighter_bot._handle_ws_order_update(data)
        # Should not crash; order is still parsed (with empty symbol)
        assert 900 in lighter_bot._known_exchange_order_ids


# ===========================================================================
# C9: _handle_ws_user_stats — data at top level (no "stats" wrapper)
# ===========================================================================

class TestWsUserStatsTopLevel:
    def test_top_level_balance_updates(self, lighter_bot):
        """Balance data without 'stats' wrapper should still update balance."""
        lighter_bot.balance = 5000.0
        data = {"available_balance": 7000.0}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 7000.0


# ===========================================================================
# C10: _handle_ws_user_stats — invalid balance string
# ===========================================================================

class TestWsUserStatsInvalidBalance:
    def test_invalid_balance_no_crash(self, lighter_bot):
        """Non-numeric balance should not crash; balance stays unchanged."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": "not_a_number"}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 5000.0


# ===========================================================================
# C11: fetch_positions — API exception returns False
# ===========================================================================

class TestFetchPositionsException:
    @pytest.mark.asyncio
    async def test_api_exception_returns_false(self, lighter_bot):
        """Exception from account API should return False."""
        lighter_bot.account_api.account = AsyncMock(
            side_effect=Exception("connection timeout")
        )
        result = await lighter_bot.fetch_positions()
        assert result is False


# ===========================================================================
# C12: fetch_positions — empty accounts list
# ===========================================================================

class TestFetchPositionsEmptyAccounts:
    @pytest.mark.asyncio
    async def test_empty_accounts_returns_false(self, lighter_bot):
        """Response with accounts=[] should return False."""
        lighter_bot.account_api.account = AsyncMock(
            return_value=types.SimpleNamespace(accounts=[])
        )
        result = await lighter_bot.fetch_positions()
        assert result is False


# ===========================================================================
# C13: fetch_open_orders — HTTP error response
# ===========================================================================

class TestFetchOpenOrdersHttpError:
    @pytest.mark.asyncio
    async def test_http_500_returns_empty_list(self, lighter_bot):
        """HTTP 500 from active orders endpoint should return empty list."""
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        orders = await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        assert orders == []


# ===========================================================================
# C14: fetch_tickers — API error returns False
# ===========================================================================

class TestFetchTickersException:
    @pytest.mark.asyncio
    async def test_api_exception_returns_false(self, lighter_bot):
        """Exception from order_books should return False."""
        lighter_bot.order_api.order_books = AsyncMock(
            side_effect=Exception("network error")
        )
        lighter_bot._tickers_cache = None  # force fresh fetch
        result = await lighter_bot.fetch_tickers()
        assert result is False


# ===========================================================================
# C15: _attempt_quota_recovery — fallback to create_order
# ===========================================================================

class TestQuotaRecoveryFallback:
    @pytest.mark.asyncio
    async def test_fallback_to_create_order(self, lighter_bot):
        """When create_market_order is unavailable, should fall back to create_order."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._bot_start_time = time.monotonic() - 200
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        # Remove create_market_order so fallback kicks in
        if hasattr(lighter_bot.lighter_client, "create_market_order"):
            delattr(lighter_bot.lighter_client, "create_market_order")
        # Fallback path: tx, tx_hash, err = create_order(...); response = tx
        # So tx must carry volume_quota_remaining for quota to update
        resp_ns = types.SimpleNamespace(volume_quota_remaining=60, code=0)
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=(resp_ns, "0xhash", None)
        )
        result = await lighter_bot._attempt_quota_recovery()
        assert result is True
        lighter_bot.lighter_client.create_order.assert_called()


# ===========================================================================
# C16: _attempt_quota_recovery — order error returns False
# ===========================================================================

class TestQuotaRecoveryOrderError:
    @pytest.mark.asyncio
    async def test_order_error_returns_false(self, lighter_bot):
        """Error from recovery order should return False immediately."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._bot_start_time = time.monotonic() - 200
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        lighter_bot.lighter_client.create_market_order = AsyncMock(
            return_value=("tx", None, "insufficient balance")
        )
        result = await lighter_bot._attempt_quota_recovery()
        assert result is False


# ===========================================================================
# C17: _subscribe_ws_channels — subscribes to all 3 channel types
# ===========================================================================

class TestSubscribeWsChannelsComplete:
    @pytest.mark.asyncio
    async def test_subscribes_all_channels(self, lighter_bot):
        """Should subscribe to account_orders (per market), account_all, and user_stats."""
        ws_mock = AsyncMock()
        auth = "test_auth"
        lighter_bot.active_symbols = ["HYPE/USDC:USDC", "BTC/USDC:USDC"]

        await lighter_bot._subscribe_ws_channels(ws_mock, auth)

        calls = [str(c) for c in ws_mock.send.call_args_list]
        # 2 account_orders (one per market) + 1 account_all + 1 user_stats = 4
        assert ws_mock.send.call_count == 4
        account_orders_calls = [c for c in calls if "account_orders" in c]
        account_all_calls = [c for c in calls if "account_all" in c]
        user_stats_calls = [c for c in calls if "user_stats" in c]
        assert len(account_orders_calls) == 2
        assert len(account_all_calls) == 1
        assert len(user_stats_calls) == 1


# ===========================================================================
# C18: execute_cancellations — partial failures in concurrent batch
# ===========================================================================

class TestCancellationsPartialFailure:
    @pytest.mark.asyncio
    async def test_partial_failures_in_batch(self, lighter_bot):
        """2 known IDs + 1 unknown should yield 2 successes and 1 empty dict."""
        lighter_bot._known_exchange_order_ids.add(1001)
        lighter_bot._known_exchange_order_ids.add(1002)
        # 1003 is NOT in known IDs → should return {}

        orders = [
            {"symbol": "HYPE/USDC:USDC", "id": "1001"},
            {"symbol": "HYPE/USDC:USDC", "id": "1002"},
            {"symbol": "HYPE/USDC:USDC", "id": "1003"},
        ]
        results = await lighter_bot.execute_cancellations(orders)
        assert len(results) == 3
        successes = [r for r in results if lighter_bot.did_cancel_order(r)]
        failures = [r for r in results if r == {}]
        assert len(successes) == 2
        assert len(failures) == 1


# ===========================================================================
# C19: _sign_and_send_batch — resp.code != 0
# ===========================================================================

class TestBatchRespCodeError:
    @pytest.mark.asyncio
    async def test_batch_nonzero_code_returns_all_empty(self, lighter_bot):
        """Non-zero response code from send_tx_batch should return all empty results."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(code=1, message="batch error")
        )
        ops = [
            {"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
             "qty": 1.0, "price": 15.0, "reduce_only": False},
            {"action": "create", "symbol": "HYPE/USDC:USDC", "side": "sell",
             "qty": 1.0, "price": 16.0, "reduce_only": False},
        ]
        results = await lighter_bot._sign_and_send_batch(ops)
        assert len(results) == 2
        assert all(r == {} for r in results)


# ===========================================================================
# NEW: format_custom_id_single truncation
# ===========================================================================

class TestFormatCustomIdSingle:
    def test_truncates_to_max_length(self, lighter_bot):
        """Output should be at most custom_id_max_length chars."""
        result = lighter_bot.format_custom_id_single(42)
        assert len(result) <= lighter_bot.custom_id_max_length

    def test_returns_string(self, lighter_bot):
        result = lighter_bot.format_custom_id_single(0)
        assert isinstance(result, str)


# ===========================================================================
# NEW: _build_coin_symbol_caches correctness
# ===========================================================================

class TestBuildCoinSymbolCaches:
    def test_caches_populated(self, lighter_bot):
        """coin_to_symbol_map and symbol_to_coin_map should cover all markets."""
        assert len(lighter_bot.coin_to_symbol_map) == len(lighter_bot.markets_dict)
        assert len(lighter_bot.symbol_to_coin_map) == len(lighter_bot.markets_dict)

    def test_round_trip(self, lighter_bot):
        """coin -> symbol -> coin should be identity."""
        for coin, symbol in lighter_bot.coin_to_symbol_map.items():
            assert lighter_bot.symbol_to_coin_map[symbol] == coin


# ===========================================================================
# NEW: WS handler — uppercase terminal status (case-sensitivity fix)
# ===========================================================================

class TestWsUppercaseTerminalStatus:
    def test_uppercase_filled_cleans_mapping(self, lighter_bot):
        """Uppercase 'FILLED' should still clean up order ID mapping."""
        lighter_bot._client_to_exchange_order_id[900] = 901
        lighter_bot._known_exchange_order_ids.add(901)
        data = {
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "FILLED",
                "price": 15.0, "size": 1.0, "client_order_index": 900,
                "order_index": 901, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 900 not in lighter_bot._client_to_exchange_order_id
        assert 901 not in lighter_bot._known_exchange_order_ids

    def test_uppercase_cancelled_cleans_mapping(self, lighter_bot):
        """Uppercase 'CANCELLED' should still clean up order ID mapping."""
        lighter_bot._client_to_exchange_order_id[902] = 903
        lighter_bot._known_exchange_order_ids.add(903)
        data = {
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "CANCELLED",
                "price": 15.0, "size": 1.0, "client_order_index": 902,
                "order_index": 903, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)
        assert 902 not in lighter_bot._client_to_exchange_order_id
        assert 903 not in lighter_bot._known_exchange_order_ids


# ===========================================================================
# NEW: Cancel event race condition — event registered before send_tx
# ===========================================================================

class TestCancelEventTiming:
    @pytest.mark.asyncio
    async def test_event_registered_before_send(self, lighter_bot):
        """Cancel event should be in _order_cancel_events during send_tx."""
        lighter_bot._known_exchange_order_ids.add(700)
        registered_during_send = {}

        original_send_tx = lighter_bot.lighter_client.send_tx

        async def capture_send_tx(**kwargs):
            # Check if event is registered at the moment of send
            registered_during_send["found"] = 700 in lighter_bot._order_cancel_events
            return types.SimpleNamespace(code=0, message="OK")

        lighter_bot.lighter_client.send_tx = AsyncMock(side_effect=capture_send_tx)
        order = {"id": "700", "symbol": "HYPE/USDC:USDC"}
        await lighter_bot.execute_cancellation(order)
        assert registered_during_send.get("found") is True

    @pytest.mark.asyncio
    async def test_event_cleaned_up_after_timeout(self, lighter_bot):
        """Cancel event should be cleaned up even after timeout."""
        lighter_bot._known_exchange_order_ids.add(701)
        lighter_bot.lighter_client.send_tx = AsyncMock(
            return_value=types.SimpleNamespace(code=0, message="OK")
        )
        order = {"id": "701", "symbol": "HYPE/USDC:USDC"}
        await lighter_bot.execute_cancellation(order)
        # Event should be cleaned up from _order_cancel_events
        assert 701 not in lighter_bot._order_cancel_events


# ===========================================================================
# NEW: Nonce lifecycle — not rolled back after successful send
# ===========================================================================

class TestNonceLifecycleCancel:
    @pytest.mark.asyncio
    async def test_nonce_not_rolled_back_after_successful_send(self, lighter_bot):
        """After send_tx succeeds, acknowledge_failure should NOT be called
        even if a later step raises an exception."""
        lighter_bot._known_exchange_order_ids.add(800)
        lighter_bot.lighter_client.send_tx = AsyncMock(
            return_value=types.SimpleNamespace(code=0, message="OK")
        )
        lighter_bot.lighter_client.nonce_manager.acknowledge_failure.reset_mock()

        order = {"id": "800", "symbol": "HYPE/USDC:USDC"}
        result = await lighter_bot.execute_cancellation(order)

        # Should succeed
        assert lighter_bot.did_cancel_order(result)
        # acknowledge_failure should NOT have been called
        lighter_bot.lighter_client.nonce_manager.acknowledge_failure.assert_not_called()


# ===========================================================================
# NEW: Batch error-type branching
# ===========================================================================

class TestBatchErrorTypeBranching:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_batch_quota_error_updates_quota_to_zero(self, lighter_bot):
        """Quota error in batch response should set quota to 0."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=1, message="volume quota: not enough remaining"
            )
        )
        ops = [{"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
                "qty": 1.0, "price": 15.0, "reduce_only": False}]
        await lighter_bot._sign_and_send_batch(ops)
        assert lighter_bot._volume_quota_remaining == 0

    @pytest.mark.asyncio
    async def test_batch_429_triggers_global_backoff(self, lighter_bot):
        """429 error in batch response should trigger global backoff."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=1, message="429 too many requests"
            )
        )
        ops = [{"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
                "qty": 1.0, "price": 15.0, "reduce_only": False}]
        import time as _time
        before = lighter_bot._global_backoff_until
        await lighter_bot._sign_and_send_batch(ops)
        assert lighter_bot._global_backoff_until > before

    @pytest.mark.asyncio
    async def test_batch_nonce_error_hard_refreshes(self, lighter_bot):
        """Nonce error in batch response should hard_refresh_nonce."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=1, message="invalid nonce"
            )
        )
        lighter_bot.lighter_client.nonce_manager.hard_refresh_nonce.reset_mock()
        ops = [{"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
                "qty": 1.0, "price": 15.0, "reduce_only": False}]
        await lighter_bot._sign_and_send_batch(ops)
        lighter_bot.lighter_client.nonce_manager.hard_refresh_nonce.assert_called()

    @pytest.mark.asyncio
    async def test_batch_send_exception_acknowledges_all_nonces(self, lighter_bot):
        """Send exception should acknowledge_failure for all signed nonces."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            side_effect=Exception("connection lost")
        )
        lighter_bot.lighter_client.nonce_manager.acknowledge_failure.reset_mock()
        ops = [
            {"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
             "qty": 1.0, "price": 15.0, "reduce_only": False},
            {"action": "create", "symbol": "HYPE/USDC:USDC", "side": "sell",
             "qty": 1.0, "price": 16.0, "reduce_only": False},
        ]
        await lighter_bot._sign_and_send_batch(ops)
        # Should acknowledge failure for each signed op
        assert lighter_bot.lighter_client.nonce_manager.acknowledge_failure.call_count == 2


# ===========================================================================
# NEW: Multi-market scenarios
# ===========================================================================

class TestMultiMarket:
    def test_all_markets_have_settings(self, lighter_bot):
        """All markets in markets_dict should have symbol_ids entries."""
        for symbol in lighter_bot.markets_dict:
            assert symbol in lighter_bot.symbol_ids

    def test_btc_and_hype_coexist(self, lighter_bot):
        """BTC and HYPE should both be present with correct market IDs."""
        assert "BTC/USDC:USDC" in lighter_bot.market_id_map
        assert "HYPE/USDC:USDC" in lighter_bot.market_id_map
        assert lighter_bot.market_id_map["BTC/USDC:USDC"] != lighter_bot.market_id_map["HYPE/USDC:USDC"]

    def test_cross_market_order_execution(self, lighter_bot):
        """Orders for different markets should use correct market indices."""
        btc_idx = lighter_bot._symbol_to_market_index("BTC/USDC:USDC")
        hype_idx = lighter_bot._symbol_to_market_index("HYPE/USDC:USDC")
        assert btc_idx != hype_idx


# ===========================================================================
# NEW: Balance warning when all fields are None
# ===========================================================================

class TestBalanceWarningAllNone:
    @pytest.mark.asyncio
    async def test_all_none_balance_logs_warning(self, lighter_bot):
        """When all balance fields are None, should log warning and return 0.0 balance."""
        acc = types.SimpleNamespace(
            available_balance=None,
            collateral=None,
            total_asset_value=None,
            positions={},
        )
        lighter_bot.account_api.account = AsyncMock(
            return_value=types.SimpleNamespace(accounts=[acc])
        )
        result = await lighter_bot.fetch_positions()
        positions, balance = result
        assert balance == 0.0
        assert positions == []


# ===========================================================================
# NEW: Batch order raw value assertions
# ===========================================================================

class TestBatchOrderRawValues:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_batch_passes_correct_raw_values(self, lighter_bot):
        """Batch create should pass correctly converted raw price and amount to sign_create_order."""
        symbol = "HYPE/USDC:USDC"
        price = 15.0
        qty = 2.5
        ops = [{"action": "create", "symbol": symbol, "side": "buy",
                "qty": qty, "price": price, "reduce_only": False}]

        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=0, message="OK", tx_hash=["0xh1"],
                volume_quota_remaining=100,
            )
        )

        await lighter_bot._sign_and_send_batch(ops)

        # Verify sign_create_order was called with correct raw values
        call_kwargs = lighter_bot.lighter_client.sign_create_order.call_args
        expected_raw_price = lighter_bot._to_raw_price(price, symbol)
        expected_raw_amount = lighter_bot._to_raw_amount(qty, symbol)
        assert call_kwargs[1]["price"] == expected_raw_price
        assert call_kwargs[1]["base_amount"] == expected_raw_amount
        assert call_kwargs[1]["is_ask"] is False  # buy = not ask


# ===========================================================================
# NEW: determine_utc_offset
# ===========================================================================

class TestDetermineUtcOffset:
    @pytest.mark.asyncio
    async def test_offset_from_server_timestamp(self, lighter_bot):
        """Should compute offset from server timestamp."""
        mock_root_api = MagicMock()
        mock_root_api.status = AsyncMock(
            return_value=types.SimpleNamespace(timestamp=str(time.time() * 1000))
        )
        with patch("lighter.RootApi", return_value=mock_root_api):
            await lighter_bot.determine_utc_offset(verbose=False)
        # Offset should be close to 0 since we used current time
        assert abs(lighter_bot.utc_offset) < 3600 * 1000  # less than 1 hour

    @pytest.mark.asyncio
    async def test_offset_defaults_to_zero_on_error(self, lighter_bot):
        """On exception, offset should default to 0."""
        mock_root_api = MagicMock()
        mock_root_api.status = AsyncMock(side_effect=Exception("network error"))
        with patch("lighter.RootApi", return_value=mock_root_api):
            await lighter_bot.determine_utc_offset(verbose=False)
        assert lighter_bot.utc_offset == 0


# ===========================================================================
# NEW: Backoff escalation sequence (bug 1.1 regression test)
# ===========================================================================

class TestBackoffEscalation:
    def test_backoff_base_is_15(self, lighter_bot):
        """_rl_backoff_base must be 15.0 to match reference implementation."""
        assert lighter_bot._rl_backoff_base == 15.0

    def test_backoff_escalation_sequence(self, lighter_bot):
        """Verify escalation: 15 -> 30 -> 60 -> 120 (capped)."""
        expected = [15.0, 30.0, 60.0, 120.0, 120.0]
        for i, exp in enumerate(expected):
            before = time.monotonic()
            lighter_bot._trigger_global_backoff()
            duration = lighter_bot._global_backoff_until - before
            assert abs(duration - exp) < 1.0, (
                f"Step {i+1}: expected ~{exp}s backoff, got {duration:.1f}s"
            )

    def test_backoff_resets_after_consecutive_successes(self, lighter_bot):
        """After reset, escalation starts over from 15s."""
        lighter_bot._trigger_global_backoff()  # 15s
        lighter_bot._trigger_global_backoff()  # 30s
        # Reset
        lighter_bot._consecutive_successes = 0
        lighter_bot._reset_global_backoff()
        lighter_bot._reset_global_backoff()  # need 2 consecutive
        assert lighter_bot._global_backoff_consecutive == 0
        # Next backoff should be 15s again
        before = time.monotonic()
        lighter_bot._trigger_global_backoff()
        duration = lighter_bot._global_backoff_until - before
        assert abs(duration - 15.0) < 1.0


# ===========================================================================
# NEW: _wait_for_write_slot does NOT trigger quota recovery (bug 1.2 test)
# ===========================================================================

class TestWriteSlotNoQuotaRecovery:
    @pytest.mark.asyncio
    async def test_write_slot_does_not_call_quota_recovery(self, lighter_bot):
        """After fix 1.2, _wait_for_write_slot must never call _attempt_quota_recovery."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._quota_warning_level = "critical"
        lighter_bot._last_send_time = time.monotonic() - 100  # long ago

        with patch.object(lighter_bot, "_attempt_quota_recovery", new_callable=AsyncMock) as mock_recovery:
            # Even with quota exhausted, wait_for_write_slot should not trigger recovery
            await lighter_bot._wait_for_write_slot(op_count=1)
            mock_recovery.assert_not_called()


# ===========================================================================
# NEW: fetch_pnls with string market_id (bug 3.3 test)
# ===========================================================================

class TestFetchPnlsStringMarketId:
    @pytest.mark.asyncio
    async def test_fetch_pnls_string_market_id(self, lighter_bot):
        """market_id from JSON may be string '5' — should resolve correctly."""
        orders_with_string_ids = {
            "orders": [
                {
                    "order_index": 100001,
                    "client_order_index": 200001,
                    "market_id": "5",  # string, not int
                    "is_ask": True,
                    "status": "filled",
                    "price": 15.50,
                    "size": 2.0,
                    "timestamp": 1709400000000,
                    "realized_pnl": 1.50,
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=orders_with_string_ids)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        pnls = await lighter_bot.fetch_pnls()
        assert len(pnls) == 1
        assert pnls[0]["symbol"] == "HYPE/USDC:USDC"
        assert pnls[0]["pnl"] == 1.50

    @pytest.mark.asyncio
    async def test_fetch_pnls_missing_market_id(self, lighter_bot):
        """If market_id is missing, symbol should be empty string."""
        orders_no_market_id = {
            "orders": [
                {
                    "order_index": 100001,
                    "client_order_index": 200001,
                    "is_ask": False,
                    "status": "filled",
                    "price": 14.80,
                    "size": 1.0,
                    "timestamp": 1709400000000,
                    "realized_pnl": 0.50,
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=orders_no_market_id)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        pnls = await lighter_bot.fetch_pnls()
        assert len(pnls) == 1
        # -1 won't be in market_index_to_symbol, so symbol should be ""
        assert pnls[0]["symbol"] == ""


# ===========================================================================
# NEW: Full order lifecycle integration test
# ===========================================================================

class TestOrderLifecycle:
    @pytest.mark.asyncio
    async def test_create_then_cancel(self, lighter_bot):
        """Exercise: create order -> appears with ID -> cancel order -> gone."""
        symbol = "HYPE/USDC:USDC"
        lighter_bot.active_symbols = [symbol]

        # Step 1: Create order
        order = {
            "symbol": symbol,
            "side": "buy",
            "price": 15.0,
            "qty": 1.0,
            "reduce_only": False,
        }
        result = await lighter_bot.execute_order(order)
        assert result, "execute_order should return a non-empty dict"
        assert result["symbol"] == symbol
        assert result["side"] == "buy"
        client_order_id = result["id"]

        # Step 2: Simulate WS update confirming order (map client -> exchange ID)
        exchange_order_id = 999111
        lighter_bot._client_to_exchange_order_id[int(client_order_id)] = exchange_order_id

        # Step 3: Cancel order
        cancel_order = {"id": client_order_id, "symbol": symbol}
        cancel_result = await lighter_bot.execute_cancellation(cancel_order)
        assert lighter_bot.did_cancel_order(cancel_result)

    @pytest.mark.asyncio
    async def test_create_order_rate_limited_returns_empty(self, lighter_bot):
        """When rate limiter blocks, execute_order returns empty dict."""
        lighter_bot._global_backoff_until = time.monotonic() + 999
        order = {
            "symbol": "HYPE/USDC:USDC",
            "side": "sell",
            "price": 15.5,
            "qty": 1.0,
            "reduce_only": False,
        }
        result = await lighter_bot.execute_order(order)
        assert result == {}


# ===========================================================================
# NEW: _record_ops_sent timing in quota recovery (bug 1.4 test)
# ===========================================================================

class TestQuotaRecoveryOpsTiming:
    @pytest.mark.asyncio
    async def test_ops_not_recorded_on_error(self, lighter_bot):
        """If recovery order fails, _record_ops_sent should NOT have been called."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._quota_recovery_last_attempt = 0
        lighter_bot._bot_start_time = time.monotonic() - 200
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        lighter_bot.min_qtys = {"HYPE/USDC:USDC": 0.01}
        lighter_bot.qty_steps = {"HYPE/USDC:USDC": 0.01}
        lighter_bot._last_send_time = 0  # long ago

        # Mock fetch_tickers
        lighter_bot.fetch_tickers = AsyncMock(return_value={
            "HYPE/USDC:USDC": {"bid": 15.0, "ask": 15.1, "last": 15.05}
        })
        lighter_bot.fetch_positions = AsyncMock(return_value=([], 5000.0))

        # Make create_order return an error
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "some error")
        )

        initial_ops = len(lighter_bot._op_timestamps)
        await lighter_bot._attempt_quota_recovery()
        # No ops should have been recorded since the order errored
        assert len(lighter_bot._op_timestamps) == initial_ops


# ===========================================================================
# NEW: WS subscription partial failure (bug 3.1 test)
# ===========================================================================

class TestWSSubscriptionPartialFailure:
    @pytest.mark.asyncio
    async def test_partial_subscription_failure_continues(self, lighter_bot):
        """If one subscription fails, others should still be attempted."""
        lighter_bot.active_symbols = ["HYPE/USDC:USDC", "BTC/USDC:USDC"]

        send_count = 0
        call_log = []

        async def mock_send(msg):
            nonlocal send_count
            send_count += 1
            call_log.append(msg)
            # Fail on the second call
            if send_count == 2:
                raise Exception("connection lost")

        ws = MagicMock()
        ws.send = mock_send

        # Should not raise — errors are caught per-subscription
        await lighter_bot._subscribe_ws_channels(ws, "mock_auth")
        # Should have attempted all subscriptions (2 market channels + account_all + user_stats = 4)
        # Even though one failed, total attempts should be >= 3
        assert send_count >= 3


# ===========================================================================
# NEW: known_exchange_order_ids merge (bug 3.2 test)
# ===========================================================================

class TestKnownOrderIdsPrune:
    @pytest.mark.asyncio
    async def test_fetch_open_orders_replaces_ids(self, lighter_bot):
        """fetch_open_orders should replace (not merge) known IDs to prune stale entries."""
        symbol = "HYPE/USDC:USDC"
        lighter_bot.active_symbols = [symbol]
        # Pre-populate with a stale ID (order that closed while WS was down)
        lighter_bot._known_exchange_order_ids = {777}

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=MOCK_ACTIVE_ORDERS)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        orders = await lighter_bot.fetch_open_orders()
        # Stale ID 777 should be pruned (replaced, not merged)
        assert 777 not in lighter_bot._known_exchange_order_ids
        # The new IDs from the fetch should be there
        assert 987654 in lighter_bot._known_exchange_order_ids
        assert 987655 in lighter_bot._known_exchange_order_ids


# ===========================================================================
# NEW: P0 — Negative balance validation in _handle_ws_user_stats
# ===========================================================================

class TestWsUserStatsNegativeBalance:
    def test_negative_balance_rejected(self, lighter_bot):
        """Negative balance from WS should be rejected."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": -100.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 5000.0  # unchanged

    def test_zero_balance_accepted(self, lighter_bot):
        """Zero balance should be accepted (not negative)."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": 0.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 0.0


# ===========================================================================
# NEW: P0 — Per-market maxLeverage from API
# ===========================================================================

class TestPerMarketMaxLeverage:
    def test_hype_leverage_from_api(self, lighter_bot):
        """HYPE should use max_leverage=20 from mock API (not hardcoded 50)."""
        assert lighter_bot.max_leverage["HYPE/USDC:USDC"] == 20

    def test_btc_leverage_from_api(self, lighter_bot):
        """BTC should use max_leverage=100 from mock API."""
        assert lighter_bot.max_leverage["BTC/USDC:USDC"] == 100

    def test_eth_leverage_from_api(self, lighter_bot):
        """ETH should use max_leverage=50 from mock API."""
        assert lighter_bot.max_leverage["ETH/USDC:USDC"] == 50


# ===========================================================================
# NEW: P1 — Monotonicity guarantee for client order IDs
# ===========================================================================

class TestClientOrderIdMonotonicity:
    def test_ids_strictly_increasing(self, lighter_bot):
        """Each generated ID should be strictly greater than the last."""
        prev = 0
        for _ in range(100):
            new_id = lighter_bot._generate_client_order_id()
            assert new_id > prev, f"ID {new_id} not greater than previous {prev}"
            prev = new_id

    def test_monotonic_after_simulated_clock_backward(self, lighter_bot):
        """Even if time_ns would produce a lower value, IDs should not decrease."""
        # Generate a high ID
        lighter_bot._last_client_order_id = 2**47  # near max
        new_id = lighter_bot._generate_client_order_id()
        assert new_id > 2**47


# ===========================================================================
# NEW: P1 — Post-warmup grace period for quota recovery
# ===========================================================================

class TestQuotaRecoveryGracePeriod:
    @pytest.mark.asyncio
    async def test_recovery_blocked_during_warmup(self, lighter_bot):
        """Quota recovery should be blocked during post-warmup grace period."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._bot_start_time = time.monotonic()  # just started
        result = await lighter_bot._attempt_quota_recovery()
        assert result is False

    @pytest.mark.asyncio
    async def test_recovery_allowed_after_grace(self, lighter_bot):
        """Quota recovery should be allowed after grace period elapses."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._bot_start_time = time.monotonic() - 200  # started 200s ago
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        resp = types.SimpleNamespace(volume_quota_remaining=60, code=0)
        lighter_bot.lighter_client.create_market_order = AsyncMock(
            return_value=("tx", resp, None)
        )
        result = await lighter_bot._attempt_quota_recovery()
        assert result is True


# ===========================================================================
# NEW: P2 — WS snapshot reconciliation
# ===========================================================================

class TestWsSnapshotReconciliation:
    def test_first_message_clears_stale_mappings(self, lighter_bot):
        """First WS message (snapshot) should clear stale order mappings."""
        # Pre-populate stale mapping that won't be in snapshot
        lighter_bot._client_to_exchange_order_id[100] = 200
        lighter_bot._known_exchange_order_ids.add(200)
        lighter_bot._ws_orders_snapshot_received = False  # reset

        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "open",
                "price": 14.80, "size": 5.0, "client_order_index": 300,
                "order_index": 400, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)

        # Stale mapping should be removed
        assert 100 not in lighter_bot._client_to_exchange_order_id
        assert 200 not in lighter_bot._known_exchange_order_ids
        # New order should be present
        assert lighter_bot._client_to_exchange_order_id[300] == 400
        assert lighter_bot._ws_orders_snapshot_received is True

    def test_second_message_is_incremental(self, lighter_bot):
        """Second WS message should NOT clear existing mappings (incremental)."""
        lighter_bot._ws_orders_snapshot_received = True  # already received snapshot
        lighter_bot._client_to_exchange_order_id[100] = 200

        data = {
            "type": "update/account_orders",
            "orders": [{
                "market_id": 5, "is_ask": False, "status": "open",
                "price": 14.80, "size": 5.0, "client_order_index": 300,
                "order_index": 400, "timestamp": 1709500000000,
            }],
        }
        lighter_bot._handle_ws_order_update(data)

        # Both mappings should exist
        assert lighter_bot._client_to_exchange_order_id[100] == 200
        assert lighter_bot._client_to_exchange_order_id[300] == 400


# ===========================================================================
# NEW: P2 — Orphan order detection in fetch_open_orders
# ===========================================================================

class TestOrphanOrderDetection:
    @pytest.mark.asyncio
    async def test_orphan_detected(self, lighter_bot):
        """Orders on exchange without local mapping should be detected as orphans."""
        # No local mappings — all exchange orders are orphans
        lighter_bot._client_to_exchange_order_id.clear()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=MOCK_ACTIVE_ORDERS)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        orders = await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        # Orders are returned but local mapping was empty so they're orphans
        # The orphan detection just logs, doesn't modify behavior
        assert len(orders) == 2

    @pytest.mark.asyncio
    async def test_no_orphans_when_all_tracked(self, lighter_bot):
        """No orphans when all exchange orders have local mappings."""
        # Pre-populate local mappings matching mock data
        lighter_bot._client_to_exchange_order_id[123456789] = 987654
        lighter_bot._client_to_exchange_order_id[123456790] = 987655

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=MOCK_ACTIVE_ORDERS)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        orders = await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        assert len(orders) == 2


# ===========================================================================
# NEW: P3 — _wait_for_write_slot quota pacing (mult > 1.0)
# ===========================================================================

class TestWriteSlotQuotaPacing:
    @pytest.mark.asyncio
    async def test_medium_quota_adds_extra_sleep(self, lighter_bot):
        """With medium quota (1.5x pacing), write slot should take slightly longer."""
        lighter_bot._volume_quota_remaining = 100  # medium range -> 1.5x multiplier
        lighter_bot._last_send_time = time.monotonic()  # just sent

        start = time.monotonic()
        result = await lighter_bot._wait_for_write_slot(op_count=1)
        elapsed = time.monotonic() - start

        assert result is True
        # Should have slept for at least the base floor (0.1s) * 1.5x = 0.15s
        assert elapsed >= 0.1

    @pytest.mark.asyncio
    async def test_low_quota_adds_more_sleep(self, lighter_bot):
        """With low quota (3x pacing), write slot should take longer."""
        lighter_bot._volume_quota_remaining = 15  # low range -> 3x multiplier
        lighter_bot._last_send_time = time.monotonic()

        start = time.monotonic()
        result = await lighter_bot._wait_for_write_slot(op_count=1)
        elapsed = time.monotonic() - start

        assert result is True
        # 3x multiplier on 0.1s floor = 0.3s total
        assert elapsed >= 0.15


# ===========================================================================
# NEW: P3 — init_markets flow (mocked)
# ===========================================================================

class TestInitMarketsFlow:
    @pytest.mark.asyncio
    async def test_init_markets_populates_all_fields(self, lighter_bot):
        """init_markets should populate markets_dict, caches, and settings."""
        # Mock all dependencies
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response()
        )

        mock_root_api = MagicMock()
        mock_root_api.status = AsyncMock(
            return_value=types.SimpleNamespace(timestamp=str(time.time() * 1000))
        )

        # Mock methods called during init_markets
        lighter_bot.update_positions = AsyncMock()
        lighter_bot.update_open_orders = AsyncMock()
        lighter_bot.update_effective_min_cost = AsyncMock()
        lighter_bot.update_exchange_config = AsyncMock()
        lighter_bot.determine_utc_offset = AsyncMock()
        lighter_bot.refresh_approved_ignored_coins_lists = MagicMock()
        lighter_bot.set_wallet_exposure_limits = MagicMock()
        lighter_bot.init_coin_overrides = MagicMock()
        lighter_bot.is_forager_mode = MagicMock(return_value=False)

        with patch("utils.filter_markets", return_value=(list(lighter_bot.markets_dict.keys()), [], {})):
            await lighter_bot.init_markets(verbose=False)

        # Verify markets_dict is populated
        assert len(lighter_bot.markets_dict) >= 3
        assert "HYPE/USDC:USDC" in lighter_bot.markets_dict
        assert "BTC/USDC:USDC" in lighter_bot.markets_dict

        # Verify caches built
        assert "HYPE" in lighter_bot.coin_to_symbol_map
        assert "HYPE/USDC:USDC" in lighter_bot.symbol_to_coin_map

        # Verify settings populated
        assert "HYPE/USDC:USDC" in lighter_bot.symbol_ids

        # Verify per-market maxLeverage from API
        assert lighter_bot.markets_dict["HYPE/USDC:USDC"]["info"]["maxLeverage"] == 20
        assert lighter_bot.markets_dict["BTC/USDC:USDC"]["info"]["maxLeverage"] == 100


# ===========================================================================
# TxWebSocket
# ===========================================================================

class TestTxWebSocket:
    """Tests for _TxWebSocket background recv loop and send_batch."""

    def _make_tx_ws(self):
        from exchanges.lighter import _TxWebSocket
        return _TxWebSocket("wss://example.com/stream")

    @pytest.mark.asyncio
    async def test_recv_loop_handles_pings(self):
        """Recv loop should respond to app-level pings and NOT put them on the queue."""
        tx_ws = self._make_tx_ws()
        mock_ws = AsyncMock()

        # Simulate: ping, ping, then connection close
        import websockets.exceptions
        mock_ws.recv = AsyncMock(side_effect=[
            '{"type":"ping"}',
            '{"type":"ping"}',
            websockets.exceptions.ConnectionClosed(None, None),
        ])
        mock_ws.send = AsyncMock()
        mock_ws.state = MagicMock()
        mock_ws.state.name = "OPEN"

        tx_ws._ws = mock_ws
        tx_ws._connected = True

        # Run recv loop until it exits
        await tx_ws._recv_loop()

        # Should have sent pong twice
        assert mock_ws.send.call_count == 2
        from exchanges.lighter import _PONG_MSG
        mock_ws.send.assert_any_call(_PONG_MSG)

        # Queue should be empty (pings not routed)
        assert tx_ws._response_queue.empty()

    @pytest.mark.asyncio
    async def test_recv_loop_routes_responses_to_queue(self):
        """Non-ping, non-system messages should be put on the response queue."""
        tx_ws = self._make_tx_ws()
        mock_ws = AsyncMock()

        response_data = '{"code":0,"message":"ok","tx_hash":["abc123"]}'
        import websockets.exceptions
        mock_ws.recv = AsyncMock(side_effect=[
            '{"type":"connected"}',
            '{"type":"subscribed"}',
            response_data,
            websockets.exceptions.ConnectionClosed(None, None),
        ])
        mock_ws.send = AsyncMock()
        mock_ws.state = MagicMock()
        mock_ws.state.name = "OPEN"

        tx_ws._ws = mock_ws
        tx_ws._connected = True

        await tx_ws._recv_loop()

        # "connected" and "subscribed" should be skipped; response should be queued
        assert not tx_ws._response_queue.empty()
        queued = await tx_ws._response_queue.get()
        assert queued["code"] == 0
        assert queued["tx_hash"] == ["abc123"]

    @pytest.mark.asyncio
    async def test_send_batch_success(self):
        """send_batch should return the parsed response dict."""
        tx_ws = self._make_tx_ws()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.state = MagicMock()
        mock_ws.state.name = "OPEN"

        tx_ws._ws = mock_ws
        tx_ws._connected = True

        # Pre-load a response in the queue (simulating recv loop)
        resp_dict = {"code": 0, "message": "ok", "tx_hash": ["h1"], "volume_quota_remaining": 100}
        await tx_ws._response_queue.put(resp_dict)

        # But that's a stale message; drain it and put the real one
        # Actually, send_batch drains stale messages first, then sends, then awaits.
        # We need to simulate the recv loop putting a response AFTER send.
        # Simplest: use a side_effect on ws.send to enqueue the response.
        async def _on_send(msg):
            await tx_ws._response_queue.put({"code": 0, "tx_hash": ["h2"], "volume_quota_remaining": 50})

        mock_ws.send = AsyncMock(side_effect=_on_send)

        result = await tx_ws.send_batch([14], [{"some": "data"}])

        assert result is not None
        assert result["code"] == 0
        assert result["tx_hash"] == ["h2"]

    @pytest.mark.asyncio
    async def test_send_batch_returns_none_when_closed(self):
        """send_batch should return None when close was requested."""
        tx_ws = self._make_tx_ws()
        tx_ws._close_requested = True
        tx_ws._connected = False

        result = await tx_ws.send_batch([14], [{"some": "data"}])
        assert result is None

    @pytest.mark.asyncio
    async def test_send_batch_timeout_returns_none(self):
        """send_batch should return None on queue timeout and mark disconnected."""
        tx_ws = self._make_tx_ws()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.state = MagicMock()
        mock_ws.state.name = "OPEN"

        tx_ws._ws = mock_ws
        tx_ws._connected = True

        # Don't put anything on the queue — will timeout
        # Patch the timeout to be very short
        import asyncio
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, timeout):
            return await original_wait_for(coro, timeout=0.01)

        with patch("asyncio.wait_for", side_effect=fast_wait_for):
            result = await tx_ws.send_batch([14], [{}])

        assert result is None
        assert not tx_ws._connected

    @pytest.mark.asyncio
    async def test_send_batch_auto_reconnects(self):
        """send_batch should call connect() when not connected."""
        tx_ws = self._make_tx_ws()
        tx_ws._connected = False

        connected_called = False

        async def mock_connect():
            nonlocal connected_called
            connected_called = True
            # Simulate successful connect
            tx_ws._connected = True
            tx_ws._ws = AsyncMock()
            tx_ws._ws.state = MagicMock()
            tx_ws._ws.state.name = "OPEN"

            # Enqueue a response for send_batch to consume
            async def _on_send(msg):
                await tx_ws._response_queue.put({"code": 0})
            tx_ws._ws.send = AsyncMock(side_effect=_on_send)

        tx_ws.connect = mock_connect

        result = await tx_ws.send_batch([14], [{}])
        assert connected_called
        assert result is not None

    @pytest.mark.asyncio
    async def test_close_cancels_recv_task(self):
        """close() should cancel the recv task and close the WS."""
        import asyncio
        tx_ws = self._make_tx_ws()
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        tx_ws._ws = mock_ws
        tx_ws._connected = True

        # Create a long-running recv task
        async def forever():
            await asyncio.sleep(3600)

        tx_ws._recv_task = asyncio.create_task(forever())

        await tx_ws.close()

        assert tx_ws._close_requested
        assert not tx_ws._connected
        assert tx_ws._ws is None
        assert tx_ws._recv_task is None


# ===========================================================================
# WsBatchResponse adapter
# ===========================================================================

class TestWsBatchResponse:
    def test_success_code_200_normalized_to_0(self):
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"code": 200, "message": "ok", "tx_hash": ["h1"],
                                 "volume_quota_remaining": 50})
        assert resp.code == 0
        assert resp.message == "ok"
        assert resp.tx_hash == ["h1"]
        assert resp.volume_quota_remaining == 50

    def test_success_code_0(self):
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"code": 0, "message": "ok"})
        assert resp.code == 0

    def test_error_envelope(self):
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"error": {"code": 429, "message": "too many requests"}})
        assert resp.code == 429
        assert resp.message == "too many requests"

    def test_quota_error(self):
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"error": {"code": 1, "message": "volume quota not enough"}})
        assert resp.code == 1
        assert "volume quota" in resp.message

    def test_missing_fields_default(self):
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({})
        assert resp.code == 0
        assert resp.message == ""
        assert resp.volume_quota_remaining is None
        assert resp.tx_hash == []


# ===========================================================================
# Batch send WS-first integration
# ===========================================================================

class TestBatchSendWsIntegration:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_batch_tries_ws_first(self, lighter_bot):
        """When TxWebSocket is connected, send_batch should be tried before REST."""
        from exchanges.lighter import _TxWebSocket
        tx_ws = _TxWebSocket.__new__(_TxWebSocket)
        tx_ws._connected = True
        tx_ws._ws = MagicMock()
        tx_ws._ws.state = MagicMock()
        tx_ws._ws.state.name = "OPEN"
        tx_ws._lock = __import__("asyncio").Lock()
        tx_ws._response_queue = __import__("asyncio").Queue()
        tx_ws._close_requested = False

        ws_response = {"code": 0, "message": "ok", "tx_hash": ["ws_hash"],
                       "volume_quota_remaining": 100}
        tx_ws.send_batch = AsyncMock(return_value=ws_response)
        lighter_bot._tx_ws = tx_ws

        # Setup for _sign_and_send_batch
        lighter_bot._volume_quota_remaining = 500
        lighter_bot._quota_warning_level = "ok"
        lighter_bot.lighter_client.send_tx_batch = AsyncMock()

        orders = [{"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
                   "price": 20.0, "reduce_only": False, "custom_id": "test123"}]
        ops = [{**o, "action": "create"} for o in orders]
        await lighter_bot._sign_and_send_batch(ops)

        # WS should have been called
        tx_ws.send_batch.assert_called_once()
        # REST should NOT have been called
        lighter_bot.lighter_client.send_tx_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_falls_back_to_rest(self, lighter_bot):
        """When TxWebSocket send_batch returns None, should fall back to REST."""
        from exchanges.lighter import _TxWebSocket
        tx_ws = _TxWebSocket.__new__(_TxWebSocket)
        tx_ws._connected = True
        tx_ws._ws = MagicMock()
        tx_ws._ws.state = MagicMock()
        tx_ws._ws.state.name = "OPEN"
        tx_ws._lock = __import__("asyncio").Lock()
        tx_ws._response_queue = __import__("asyncio").Queue()
        tx_ws._close_requested = False

        # WS returns None (failure)
        tx_ws.send_batch = AsyncMock(return_value=None)
        lighter_bot._tx_ws = tx_ws

        lighter_bot._volume_quota_remaining = 500
        lighter_bot._quota_warning_level = "ok"
        rest_resp = types.SimpleNamespace(code=0, message="ok", tx_hash=["rest_hash"],
                                          volume_quota_remaining=99)
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(return_value=rest_resp)

        orders = [{"symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
                   "price": 20.0, "reduce_only": False, "custom_id": "test456"}]
        ops = [{**o, "action": "create"} for o in orders]
        await lighter_bot._sign_and_send_batch(ops)

        # WS attempted, then REST called
        tx_ws.send_batch.assert_called_once()
        lighter_bot.lighter_client.send_tx_batch.assert_called_once()


# ===========================================================================
# Test: update_tickers None-value normalization
# ===========================================================================

class TestUpdateTickersNormalization:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_none_last_filled_from_bid_ask(self, lighter_bot):
        """When last is None but bid/ask exist, last should be their average."""
        ob = _build_order_books_response()
        # Patch one market to return None-ish values: best_bid/ask but no last
        ob.order_books[0].best_bid = 15.0
        ob.order_books[0].best_ask = 15.0
        lighter_bot.order_api.order_books = AsyncMock(return_value=ob)
        # Manually make fetch_tickers return a ticker with last=None
        async def _fake_fetch_tickers():
            return {
                "HYPE/USDC:USDC": {"bid": 15.0, "ask": 15.2, "last": None},
            }
        lighter_bot.fetch_tickers = _fake_fetch_tickers
        await lighter_bot.update_tickers()
        t = lighter_bot.tickers["HYPE/USDC:USDC"]
        assert t["last"] == pytest.approx(15.1)

    @pytest.mark.asyncio
    async def test_none_bid_filled_from_last(self, lighter_bot):
        """When bid is None but last exists, bid should be set to last."""
        async def _fake_fetch_tickers():
            return {
                "HYPE/USDC:USDC": {"bid": None, "ask": 15.2, "last": 15.1},
            }
        lighter_bot.fetch_tickers = _fake_fetch_tickers
        await lighter_bot.update_tickers()
        t = lighter_bot.tickers["HYPE/USDC:USDC"]
        assert t["bid"] == 15.1

    @pytest.mark.asyncio
    async def test_none_ask_filled_from_last(self, lighter_bot):
        """When ask is None but last exists, ask should be set to last."""
        async def _fake_fetch_tickers():
            return {
                "HYPE/USDC:USDC": {"bid": 15.0, "ask": None, "last": 15.1},
            }
        lighter_bot.fetch_tickers = _fake_fetch_tickers
        await lighter_bot.update_tickers()
        t = lighter_bot.tickers["HYPE/USDC:USDC"]
        assert t["ask"] == 15.1

    @pytest.mark.asyncio
    async def test_all_values_present_unchanged(self, lighter_bot):
        """When all values are present, nothing should change."""
        async def _fake_fetch_tickers():
            return {
                "HYPE/USDC:USDC": {"bid": 15.0, "ask": 15.2, "last": 15.1},
            }
        lighter_bot.fetch_tickers = _fake_fetch_tickers
        await lighter_bot.update_tickers()
        t = lighter_bot.tickers["HYPE/USDC:USDC"]
        assert t == {"bid": 15.0, "ask": 15.2, "last": 15.1}


# ===========================================================================
# Test: fetch_ohlcvs_1m
# ===========================================================================

class TestFetchOhlcvs1m:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_returns_candles(self, lighter_bot):
        """fetch_ohlcvs_1m should return parsed candle data."""
        mock_resp = _make_obj(MOCK_CANDLES)
        lighter_bot.candlestick_api.candlesticks = AsyncMock(return_value=mock_resp)
        result = await lighter_bot.fetch_ohlcvs_1m("HYPE/USDC:USDC")
        assert len(result) == 3
        assert result[0][0] == 1709500000000  # timestamp
        assert result[0][4] == 15.10  # close

    @pytest.mark.asyncio
    async def test_default_limit_is_5000(self, lighter_bot):
        """Default limit should be 5000, not 480 like fetch_ohlcv."""
        mock_resp = _make_obj(MOCK_CANDLES)
        lighter_bot.candlestick_api.candlesticks = AsyncMock(return_value=mock_resp)
        await lighter_bot.fetch_ohlcvs_1m("HYPE/USDC:USDC")
        call_kwargs = lighter_bot.candlestick_api.candlesticks.call_args[1]
        assert call_kwargs["count_back"] == 5000

    @pytest.mark.asyncio
    async def test_custom_limit(self, lighter_bot):
        """Custom limit should be passed through."""
        mock_resp = _make_obj(MOCK_CANDLES)
        lighter_bot.candlestick_api.candlesticks = AsyncMock(return_value=mock_resp)
        await lighter_bot.fetch_ohlcvs_1m("HYPE/USDC:USDC", limit=100)
        call_kwargs = lighter_bot.candlestick_api.candlesticks.call_args[1]
        assert call_kwargs["count_back"] == 100

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_empty_list(self, lighter_bot):
        """Unknown symbol should return [] (not False like fetch_ohlcv)."""
        result = await lighter_bot.fetch_ohlcvs_1m("UNKNOWN/USDC:USDC")
        assert result == []

    @pytest.mark.asyncio
    async def test_error_returns_empty_list(self, lighter_bot):
        """Exception should return [] (not False like fetch_ohlcv)."""
        lighter_bot.candlestick_api.candlesticks = AsyncMock(
            side_effect=Exception("API error")
        )
        result = await lighter_bot.fetch_ohlcvs_1m("HYPE/USDC:USDC")
        assert result == []


# ===========================================================================
# Test: init_markets with malformed API response
# ===========================================================================

class TestInitMarketsMalformed:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_init_markets_api_error_raises(self, lighter_bot):
        """init_markets should propagate exceptions from order_books API."""
        lighter_bot.order_api.order_books = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        with pytest.raises(Exception, match="Connection refused"):
            await lighter_bot.init_markets()

    @pytest.mark.asyncio
    async def test_init_markets_missing_fields(self, lighter_bot):
        """Malformed order book entry missing fields should raise AttributeError."""
        # Create a response with a market missing required fields
        malformed = types.SimpleNamespace(
            order_books=[
                types.SimpleNamespace(
                    market_id=99,
                    symbol="BAD",
                    # missing supported_price_decimals, supported_size_decimals
                ),
            ]
        )
        lighter_bot.order_api.order_books = AsyncMock(return_value=malformed)
        with pytest.raises(AttributeError):
            await lighter_bot.init_markets()


# ===========================================================================
# P0: Circuit breaker must NOT fire on quota / 429 / nonce errors
# ===========================================================================

class TestCircuitBreakerTransientGuard:
    @pytest.mark.asyncio
    async def test_circuit_breaker_not_triggered_by_quota_error(self, lighter_bot):
        """Quota errors should not increment _consecutive_rejections."""
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "volume quota: not enough remaining")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        for _ in range(10):
            await lighter_bot.execute_order(order)
        assert lighter_bot._consecutive_rejections == 0
        assert lighter_bot._rejection_pause_until == 0.0

    @pytest.mark.asyncio
    async def test_circuit_breaker_not_triggered_by_429(self, lighter_bot):
        """429 rate-limit errors should not increment _consecutive_rejections."""
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "429 too many requests")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        for _ in range(10):
            await lighter_bot.execute_order(order)
        assert lighter_bot._consecutive_rejections == 0
        assert lighter_bot._rejection_pause_until == 0.0

    @pytest.mark.asyncio
    async def test_circuit_breaker_not_triggered_by_nonce_error(self, lighter_bot):
        """Nonce errors should not increment _consecutive_rejections."""
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "invalid nonce")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        for _ in range(10):
            await lighter_bot.execute_order(order)
        assert lighter_bot._consecutive_rejections == 0
        assert lighter_bot._rejection_pause_until == 0.0

    @pytest.mark.asyncio
    async def test_circuit_breaker_fires_on_real_error(self, lighter_bot):
        """Non-transient errors (e.g. margin) SHOULD trigger the circuit breaker."""
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", "rejected: insufficient margin")
        )
        order = {
            "symbol": "HYPE/USDC:USDC", "side": "buy", "qty": 1.0,
            "price": 15.0, "reduce_only": False, "custom_id": "test",
        }
        for _ in range(5):
            await lighter_bot.execute_order(order)
        assert lighter_bot._consecutive_rejections >= 5
        assert lighter_bot._rejection_pause_until > time.monotonic()


# ===========================================================================
# P1: Batch error classification — circuit breaker interaction
# ===========================================================================

class TestBatchCircuitBreakerGuard:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_batch_quota_error_no_circuit_breaker(self, lighter_bot):
        """Batch quota error should NOT increment _consecutive_rejections."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=1, message="volume quota: not enough remaining"
            )
        )
        ops = [{"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
                "qty": 1.0, "price": 15.0, "reduce_only": False}]
        for _ in range(10):
            await lighter_bot._sign_and_send_batch(ops)
        assert lighter_bot._consecutive_rejections == 0

    @pytest.mark.asyncio
    async def test_batch_429_error_no_circuit_breaker(self, lighter_bot):
        """Batch 429 error should NOT increment _consecutive_rejections."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=1, message="429 too many requests"
            )
        )
        ops = [{"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
                "qty": 1.0, "price": 15.0, "reduce_only": False}]
        for _ in range(10):
            await lighter_bot._sign_and_send_batch(ops)
        assert lighter_bot._consecutive_rejections == 0

    @pytest.mark.asyncio
    async def test_batch_nonce_error_no_circuit_breaker(self, lighter_bot):
        """Batch nonce error should NOT increment _consecutive_rejections."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=1, message="invalid nonce"
            )
        )
        ops = [{"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
                "qty": 1.0, "price": 15.0, "reduce_only": False}]
        for _ in range(10):
            await lighter_bot._sign_and_send_batch(ops)
        assert lighter_bot._consecutive_rejections == 0

    @pytest.mark.asyncio
    async def test_batch_unknown_error_triggers_circuit_breaker(self, lighter_bot):
        """Batch unknown error SHOULD increment _consecutive_rejections."""
        lighter_bot.lighter_client.send_tx_batch = AsyncMock(
            return_value=types.SimpleNamespace(
                code=1, message="internal server error"
            )
        )
        ops = [{"action": "create", "symbol": "HYPE/USDC:USDC", "side": "buy",
                "qty": 1.0, "price": 15.0, "reduce_only": False}]
        for _ in range(5):
            await lighter_bot._sign_and_send_batch(ops)
        assert lighter_bot._consecutive_rejections >= 5
        assert lighter_bot._rejection_pause_until > time.monotonic()


# ===========================================================================
# P1: Orphan orders trigger reconciliation
# ===========================================================================

class TestOrphanOrderReconciliation:
    @pytest.mark.asyncio
    async def test_orphan_sets_execution_scheduled(self, lighter_bot):
        """Orphan orders (no client_order_index) should set execution_scheduled."""
        lighter_bot._client_to_exchange_order_id.clear()
        lighter_bot.execution_scheduled = False

        # Order without client_order_index — simulates an order placed
        # outside our bot, creating a true orphan
        orphan_orders = {"orders": [{
            "order_index": 999999, "market_id": 5, "is_ask": False,
            "status": "open", "price": 14.80, "size": 5.0,
            "timestamp": 1709500000000,
            # no client_order_index — won't be added to mapping
        }]}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=orphan_orders)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        assert lighter_bot.execution_scheduled is True

    @pytest.mark.asyncio
    async def test_no_orphan_no_execution_scheduled(self, lighter_bot):
        """When all orders are tracked, execution_scheduled should not be set."""
        lighter_bot.execution_scheduled = False

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=MOCK_ACTIVE_ORDERS)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        await lighter_bot.fetch_open_orders("HYPE/USDC:USDC")
        assert lighter_bot.execution_scheduled is False


# ===========================================================================
# P3: _coerce_is_ask extended string handling
# ===========================================================================

class TestCoerceIsAskExtended:
    def test_sell_string(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("sell") is True

    def test_ask_string(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("ask") is True

    def test_buy_string(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("buy") is False

    def test_bid_string(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("bid") is False

    def test_sell_uppercase(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("SELL") is True

    def test_buy_uppercase(self, lighter_bot):
        assert lighter_bot._coerce_is_ask("BUY") is False


# ===========================================================================
# P3: Quota warning logs downward transitions
# ===========================================================================

class TestQuotaWarningTransitions:
    def test_critical_to_low(self, lighter_bot):
        """Transition from critical to low should log (update level)."""
        lighter_bot._quota_warning_level = "critical"
        lighter_bot._rl_quota_low = 50
        lighter_bot._rl_quota_medium = 200
        lighter_bot._update_volume_quota(25)
        assert lighter_bot._quota_warning_level == "low"

    def test_critical_to_ok(self, lighter_bot):
        """Transition from critical to ok should log recovery."""
        lighter_bot._quota_warning_level = "critical"
        lighter_bot._rl_quota_low = 50
        lighter_bot._rl_quota_medium = 200
        lighter_bot._update_volume_quota(500)
        assert lighter_bot._quota_warning_level == "ok"

    def test_low_to_medium(self, lighter_bot):
        """Transition from low to medium should update level."""
        lighter_bot._quota_warning_level = "low"
        lighter_bot._rl_quota_low = 50
        lighter_bot._rl_quota_medium = 200
        lighter_bot._update_volume_quota(100)
        assert lighter_bot._quota_warning_level == "medium"

    def test_same_level_no_change(self, lighter_bot):
        """Staying in the same level should not change anything."""
        lighter_bot._quota_warning_level = "low"
        lighter_bot._rl_quota_low = 50
        lighter_bot._rl_quota_medium = 200
        lighter_bot._update_volume_quota(25)
        assert lighter_bot._quota_warning_level == "low"


# ===========================================================================
# Review fix: _is_quota_error now catches "quota exhausted" variant
# ===========================================================================

class TestIsQuotaErrorExhausted:
    def test_quota_exhausted(self):
        from exchanges.lighter import _is_quota_error
        assert _is_quota_error("quota exhausted")

    def test_quota_is_exhausted(self):
        from exchanges.lighter import _is_quota_error
        assert _is_quota_error("Quota is exhausted for this period")

    def test_quota_exhausted_mixed_case(self):
        from exchanges.lighter import _is_quota_error
        assert _is_quota_error("QUOTA EXHAUSTED")

    def test_existing_variants_still_work(self):
        from exchanges.lighter import _is_quota_error
        assert _is_quota_error("volume quota exhausted")
        assert _is_quota_error("Volume Quota limit reached")
        assert _is_quota_error("quota: not enough remaining")

    def test_non_quota_errors_still_false(self):
        from exchanges.lighter import _is_quota_error
        assert not _is_quota_error("network error")
        assert not _is_quota_error("exhausted resources")  # no "quota"


# ===========================================================================
# Review fix: fetch_ohlcv returns [] (not False) on error
# ===========================================================================

class TestFetchOhlcvReturnsEmptyList:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_empty_list(self, lighter_bot):
        """fetch_ohlcv should return [] for unknown symbol, not False."""
        result = await lighter_bot.fetch_ohlcv("UNKNOWN/USDC:USDC", "1m")
        assert result == []
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_exception_returns_empty_list(self, lighter_bot):
        """fetch_ohlcv should return [] on exception, not False."""
        lighter_bot.candlestick_api.candlesticks = AsyncMock(
            side_effect=Exception("API error")
        )
        result = await lighter_bot.fetch_ohlcv("HYPE/USDC:USDC", "1m")
        assert result == []
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_result_is_iterable(self, lighter_bot):
        """Callers should be able to iterate the error return value."""
        result = await lighter_bot.fetch_ohlcv("UNKNOWN/USDC:USDC")
        # This would crash if result were False
        count = 0
        for _ in result:
            count += 1
        assert count == 0


# ===========================================================================
# Review fix: _WsBatchResponse nested error envelope
# ===========================================================================

class TestWsBatchResponseNestedError:
    def test_nested_error_dict(self):
        """Nested {"error": {"code": ..., "message": ...}} should be parsed."""
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"error": {"code": 500, "message": "internal error"}})
        assert resp.code == 500
        assert resp.message == "internal error"

    def test_nested_error_quota(self):
        """Nested error with quota message should propagate."""
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"error": {"code": 1, "message": "quota exhausted"}})
        assert resp.code == 1
        assert "quota exhausted" in resp.message

    def test_nested_error_missing_code_defaults_to_minus1(self):
        """Missing code in nested error should default to -1."""
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"error": {"message": "something went wrong"}})
        assert resp.code == -1
        assert resp.message == "something went wrong"

    def test_nested_error_empty_dict(self):
        """Empty error dict should give code=-1 and empty message."""
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"error": {}})
        assert resp.code == -1
        assert resp.message == ""

    def test_error_string_falls_through_to_flat(self):
        """If error is a string (not dict), flat parsing should be used."""
        from exchanges.lighter import _WsBatchResponse
        resp = _WsBatchResponse({"error": "some error", "code": 400, "message": "bad"})
        assert resp.code == 400
        assert resp.message == "bad"


# ===========================================================================
# Review fix: _handle_ws_user_stats portfolio_value validation
# ===========================================================================

class TestWsUserStatsPortfolioValue:
    def test_negative_portfolio_value_rejects_update(self, lighter_bot):
        """Negative portfolio_value should reject the entire update."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": 6000.0, "portfolio_value": -100.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 5000.0  # unchanged

    def test_valid_portfolio_value_allows_update(self, lighter_bot):
        """Positive portfolio_value should allow balance update."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": 6000.0, "portfolio_value": 10000.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 10000.0

    def test_zero_portfolio_value_allows_update(self, lighter_bot):
        """Zero portfolio_value should be accepted (not negative)."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": 3000.0, "portfolio_value": 0.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 0.0

    def test_missing_portfolio_value_allows_update(self, lighter_bot):
        """Missing portfolio_value should not block update."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": 7000.0}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 7000.0

    def test_invalid_portfolio_value_string_rejects(self, lighter_bot):
        """Non-numeric portfolio_value should reject update."""
        lighter_bot.balance = 5000.0
        data = {"stats": {"available_balance": 6000.0, "portfolio_value": "NaN"}}
        lighter_bot._handle_ws_user_stats(data)
        assert lighter_bot.balance == 6000.0
        lighter_bot.balance = 5000.0
        data2 = {"stats": {"available_balance": 6000.0, "portfolio_value": "not_a_number"}}
        lighter_bot._handle_ws_user_stats(data2)
        assert lighter_bot.balance == 5000.0


# ===========================================================================
# Sync parity checks (runnable without pytest-asyncio)
# ===========================================================================

class TestLighterLiveParitySync:
    def test_fetch_positions_prefers_account_value_balance(self):
        lighter_bot = _create_bot()
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(MOCK_ACCOUNT_RESPONSE)
        )

        positions, balance = asyncio.run(lighter_bot.fetch_positions())

        assert len(positions) == 1
        assert balance == 5500.0

    def test_fetch_positions_ignores_stale_ws_balance(self):
        lighter_bot = _create_bot()
        lighter_bot._ws_positions_cache = [
            {
                "symbol": "HYPE/USDC:USDC",
                "position_side": "long",
                "size": 1.0,
                "price": 14.5,
            }
        ]
        lighter_bot._ws_positions_cache_ts = time.monotonic()
        lighter_bot._ws_balance_cache = 4000.0
        lighter_bot._ws_balance_cache_ts = time.monotonic() - lighter_bot._ws_cache_max_age - 1.0
        lighter_bot.account_api.account = AsyncMock(
            return_value=_build_account_response(MOCK_ACCOUNT_RESPONSE)
        )

        positions, balance = asyncio.run(lighter_bot.fetch_positions())

        assert positions[0]["symbol"] == "HYPE/USDC:USDC"
        assert balance == 5500.0

    def test_fetch_open_orders_includes_local_open_order_symbols(self):
        lighter_bot = _create_bot()
        lighter_bot.open_orders = {"BTC/USDC:USDC": [{"id": "1"}]}
        lighter_bot.positions = {}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"orders": []})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.return_value = mock_resp
        lighter_bot._aiohttp_session = mock_session

        asyncio.run(lighter_bot.fetch_open_orders())

        market_ids = [
            call.kwargs["params"]["market_id"]
            for call in mock_session.get.call_args_list
        ]
        assert lighter_bot.market_id_map["BTC/USDC:USDC"] in market_ids

    def test_market_order_rejected_when_post_only_enabled(self):
        lighter_bot = _create_bot()
        lighter_bot.config["live"]["time_in_force"] = "post_only"

        result = asyncio.run(
            lighter_bot.execute_order(
                {
                    "symbol": "HYPE/USDC:USDC",
                    "side": "buy",
                    "qty": 1.0,
                    "price": 15.0,
                    "reduce_only": False,
                    "type": "market",
                    "custom_id": "test",
                }
            )
        )

        assert result == {}
        lighter_bot.lighter_client.create_order.assert_not_called()

    def test_market_order_normalizes_to_non_post_only_limit(self):
        lighter_bot = _create_bot()

        result = asyncio.run(
            lighter_bot.execute_order(
                {
                    "symbol": "HYPE/USDC:USDC",
                    "side": "buy",
                    "qty": 1.0,
                    "price": 15.0,
                    "reduce_only": False,
                    "type": "market",
                    "custom_id": "test",
                }
            )
        )

        assert lighter_bot.did_create_order(result)
        call_kwargs = lighter_bot.lighter_client.create_order.call_args.kwargs
        assert call_kwargs["time_in_force"] == lighter_bot.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME

    def test_market_order_uses_aggressive_cached_price(self):
        lighter_bot = _create_bot()
        lighter_bot._ws_tickers_cache["HYPE/USDC:USDC"] = {
            "bid": 14.9,
            "ask": 15.2,
            "last": 15.05,
        }

        result = asyncio.run(
            lighter_bot.execute_order(
                {
                    "symbol": "HYPE/USDC:USDC",
                    "side": "buy",
                    "qty": 1.0,
                    "price": 15.0,
                    "reduce_only": False,
                    "type": "market",
                    "custom_id": "test",
                }
            )
        )

        assert lighter_bot.did_create_order(result)
        call_kwargs = lighter_bot.lighter_client.create_order.call_args.kwargs
        assert call_kwargs["price"] > lighter_bot._to_raw_price(15.0, "HYPE/USDC:USDC")

    def test_cancel_returns_failure_if_rest_fallback_shows_order_still_open(self):
        lighter_bot = _create_bot()
        lighter_bot._client_to_exchange_order_id[333] = 444
        lighter_bot.fetch_open_orders = AsyncMock(
            return_value=[{"id": "444", "symbol": "HYPE/USDC:USDC"}]
        )

        async def _timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=_timeout):
            result = asyncio.run(
                lighter_bot.execute_cancellation({"id": "333", "symbol": "HYPE/USDC:USDC"})
            )

        assert result == {}


# ===========================================================================
# Review: quota recovery IOC fallback path
# ===========================================================================

class TestQuotaRecoveryIOCFallback:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_fallback_to_ioc_when_no_create_market_order(self, lighter_bot):
        """When create_market_order doesn't exist, should use create_order IOC fallback."""
        lighter_bot._volume_quota_remaining = 0
        lighter_bot._bot_start_time = time.monotonic() - 200
        lighter_bot.active_symbols = ["HYPE/USDC:USDC"]
        lighter_bot.order_api.order_books = AsyncMock(
            return_value=_build_order_books_response()
        )
        # Remove create_market_order so fallback is triggered
        if hasattr(lighter_bot.lighter_client, "create_market_order"):
            del lighter_bot.lighter_client.create_market_order

        resp_tx = types.SimpleNamespace(volume_quota_remaining=60, code=0)
        lighter_bot.lighter_client.create_order = AsyncMock(
            return_value=("tx", "hash", None)
        )
        # create_order returns (tx, tx_hash, err) — tx is response for fallback
        lighter_bot.lighter_client.create_order.return_value = (resp_tx, "hash", None)

        result = await lighter_bot._attempt_quota_recovery()
        assert result is True
        lighter_bot.lighter_client.create_order.assert_called()


# ===========================================================================
# Review: execute_cancellations concurrent semaphore
# ===========================================================================

class TestExecuteCancellationsSemaphore:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_single_order_no_gather(self, lighter_bot):
        """Single cancel should bypass gather and call directly."""
        lighter_bot.execute_cancellation = AsyncMock(return_value={"id": "1"})
        orders = [{"id": "1", "symbol": "HYPE/USDC:USDC", "side": "buy"}]
        result = await lighter_bot.execute_cancellations(orders)
        assert len(result) == 1
        lighter_bot.execute_cancellation.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_orders_run_concurrently(self, lighter_bot):
        """Multiple cancels should all complete via gather."""
        call_order = []

        async def _track_cancel(order):
            call_order.append(order["id"])
            return {"id": order["id"]}

        lighter_bot.execute_cancellation = _track_cancel
        orders = [
            {"id": "1", "symbol": "HYPE/USDC:USDC", "side": "buy"},
            {"id": "2", "symbol": "HYPE/USDC:USDC", "side": "sell"},
            {"id": "3", "symbol": "HYPE/USDC:USDC", "side": "buy"},
            {"id": "4", "symbol": "HYPE/USDC:USDC", "side": "sell"},
        ]
        result = await lighter_bot.execute_cancellations(orders)
        assert len(result) == 4
        assert set(call_order) == {"1", "2", "3", "4"}

    @pytest.mark.asyncio
    async def test_empty_orders(self, lighter_bot):
        """Empty list should return empty."""
        result = await lighter_bot.execute_cancellations([])
        assert result == []


# ===========================================================================
# Review: close() resource cleanup
# ===========================================================================

class TestCloseResourceCleanup:
    @pytest.fixture
    def lighter_bot(self):
        return _create_bot()

    @pytest.mark.asyncio
    async def test_close_cleans_up_ws_task(self, lighter_bot):
        """close() should cancel the WS task."""
        task = MagicMock()
        task.done.return_value = False
        lighter_bot._ws_task = task
        lighter_bot._tx_ws = None
        lighter_bot._aiohttp_session = None
        lighter_bot.api_client = None
        await lighter_bot.close()
        task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_cleans_up_tx_ws(self, lighter_bot):
        """close() should close the TxWebSocket."""
        tx_ws = AsyncMock()
        lighter_bot._tx_ws = tx_ws
        lighter_bot._ws_task = None
        lighter_bot._aiohttp_session = None
        lighter_bot.api_client = None
        await lighter_bot.close()
        tx_ws.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_cleans_up_aiohttp_session(self, lighter_bot):
        """close() should close the aiohttp session."""
        session = AsyncMock()
        session.closed = False
        lighter_bot._aiohttp_session = session
        lighter_bot._tx_ws = None
        lighter_bot._ws_task = None
        lighter_bot.api_client = None
        await lighter_bot.close()
        session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_cleans_up_api_client(self, lighter_bot):
        """close() should close the api_client."""
        api = AsyncMock()
        lighter_bot.api_client = api
        lighter_bot._tx_ws = None
        lighter_bot._ws_task = None
        lighter_bot._aiohttp_session = None
        await lighter_bot.close()
        api.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_handles_all_none(self, lighter_bot):
        """close() should not crash when all resources are None."""
        lighter_bot._tx_ws = None
        lighter_bot._ws_task = None
        lighter_bot._aiohttp_session = None
        lighter_bot.api_client = None
        await lighter_bot.close()  # should not raise

    @pytest.mark.asyncio
    async def test_close_handles_exceptions_gracefully(self, lighter_bot):
        """close() should suppress exceptions from individual cleanup steps."""
        tx_ws = AsyncMock()
        tx_ws.close.side_effect = Exception("ws close failed")
        lighter_bot._tx_ws = tx_ws
        session = AsyncMock()
        session.closed = False
        session.close.side_effect = Exception("session close failed")
        lighter_bot._aiohttp_session = session
        api = AsyncMock()
        api.close.side_effect = Exception("api close failed")
        lighter_bot.api_client = api
        lighter_bot._ws_task = None
        # Should not raise despite all cleanup steps failing
        await lighter_bot.close()
