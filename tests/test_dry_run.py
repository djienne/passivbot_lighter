import asyncio

import pytest

from exchanges.dry_run import DryRunMixin


class FakeCM:
    def __init__(self, prices=None, exc=None):
        self.prices = prices or {}
        self.exc = exc

    async def get_last_prices(self, symbols, max_age_ms=10_000):
        if self.exc is not None:
            raise self.exc
        return {symbol: self.prices.get(symbol) for symbol in symbols}


class FakeDryRunBot(DryRunMixin):
    def __init__(self, *, wallet=100.0, leverage=2.0, maker=0.001, taker=0.002):
        self.config = {
            "live": {
                "dry_run_wallet": wallet,
                "leverage": leverage,
            }
        }
        self.markets_dict = {
            "BTC/USDC:USDC": {
                "maker": maker,
                "taker": taker,
            }
        }
        self.c_mults = {"BTC/USDC:USDC": 1.0}
        self.cm = FakeCM({"BTC/USDC:USDC": 10.0})
        self.execution_scheduled = False
        self.pnls = []


def market_order(*, qty, price, side="buy", position_side="long"):
    return {
        "symbol": "BTC/USDC:USDC",
        "side": side,
        "position_side": position_side,
        "qty": qty,
        "price": price,
        "type": "market",
    }


def limit_order(*, qty, price, side="buy", position_side="long"):
    return {
        "symbol": "BTC/USDC:USDC",
        "side": side,
        "position_side": position_side,
        "qty": qty,
        "price": price,
        "type": "limit",
    }


def test_limit_order_fill_updates_position_and_balance():
    bot = FakeDryRunBot()

    created = asyncio.run(bot.execute_order(limit_order(qty=4.0, price=10.0)))

    assert created["status"] == "open"
    assert bot.did_create_order(created) is True

    bot.cm.prices["BTC/USDC:USDC"] = 9.5
    open_orders = asyncio.run(bot.fetch_open_orders())
    positions, balance = asyncio.run(bot.fetch_positions())

    assert open_orders == []
    assert positions == [
        {
            "symbol": "BTC/USDC:USDC",
            "position_side": "long",
            "size": 4.0,
            "price": 10.0,
            "timestamp": positions[0]["timestamp"],
        }
    ]
    assert balance == pytest.approx(97.96)


def test_rejects_new_limit_order_when_reserved_margin_is_insufficient():
    bot = FakeDryRunBot()

    first = asyncio.run(bot.execute_order(limit_order(qty=18.0, price=10.0)))
    second = asyncio.run(bot.execute_order(limit_order(qty=3.0, price=10.0)))

    assert bot.did_create_order(first) is True
    assert second["status"] == "rejected"
    assert bot.did_create_order(second) is False
    assert len(bot._dry_run_open_orders) == 1


def test_unfillable_limit_order_stays_open_after_other_fill_consumes_margin():
    bot = FakeDryRunBot()

    queued = asyncio.run(bot.execute_order(limit_order(qty=9.95, price=10.0)))
    filled = asyncio.run(bot.execute_order(market_order(qty=10.0, price=10.0)))

    assert queued["status"] == "open"
    assert filled["status"] == "closed"

    bot.cm.prices["BTC/USDC:USDC"] = 9.0
    open_orders = asyncio.run(bot.fetch_open_orders())
    positions, balance = asyncio.run(bot.fetch_positions())

    assert len(open_orders) == 1
    assert open_orders[0]["id"] == queued["id"]
    assert positions[0]["size"] == pytest.approx(10.0)
    assert balance == pytest.approx(89.8)


def test_market_order_without_price_is_rejected_and_not_queued():
    bot = FakeDryRunBot()
    bot.cm = FakeCM({"BTC/USDC:USDC": None})

    created = asyncio.run(bot.execute_order(market_order(qty=4.0, price=10.0)))

    assert created["status"] == "rejected"
    assert created["reason"] == "market order rejected: no fresh price available"
    assert bot.did_create_order(created) is False
    assert bot._dry_run_open_orders == {}


def test_market_orders_use_taker_fees():
    bot = FakeDryRunBot()

    created = asyncio.run(bot.execute_order(market_order(qty=4.0, price=10.0)))
    positions, balance = asyncio.run(bot.fetch_positions())

    assert created["status"] == "closed"
    assert positions[0]["size"] == pytest.approx(4.0)
    assert balance == pytest.approx(99.92)


def test_cancel_releases_reserved_margin():
    bot = FakeDryRunBot()

    created = asyncio.run(bot.execute_order(limit_order(qty=9.0, price=10.0)))

    assert bot._get_available_margin() == pytest.approx(55.0)

    cancelled = asyncio.run(bot.execute_cancellation(created))

    assert cancelled["status"] == "canceled"
    assert bot._get_available_margin() == pytest.approx(100.0)


def test_market_order_uses_fetched_price_for_margin_acceptance():
    bot = FakeDryRunBot()
    bot.cm.prices["BTC/USDC:USDC"] = 28.0

    created = asyncio.run(bot.execute_order(market_order(qty=7.0, price=30.0)))

    positions, balance = asyncio.run(bot.fetch_positions())
    assert created["status"] == "closed"
    assert positions[0]["size"] == pytest.approx(7.0)
    assert positions[0]["price"] == pytest.approx(28.0)
    assert balance == pytest.approx(99.608)


def test_market_order_rejects_when_fetched_price_breaks_margin():
    bot = FakeDryRunBot()
    bot.cm.prices["BTC/USDC:USDC"] = 30.0

    created = asyncio.run(bot.execute_order(market_order(qty=7.0, price=28.0)))

    assert created["status"] == "rejected"
    assert "insufficient margin" in created["reason"]
    assert bot._dry_run_open_orders == {}


def test_positive_upnl_increases_available_margin():
    bot = FakeDryRunBot(wallet=60.0)

    opened = asyncio.run(bot.execute_order(market_order(qty=8.0, price=10.0)))
    assert opened["status"] == "closed"

    bot.cm.prices["BTC/USDC:USDC"] = 12.0
    created = asyncio.run(bot.execute_order(limit_order(qty=6.0, price=10.0)))

    assert created["status"] == "open"
    assert len(bot._dry_run_open_orders) == 1


def test_negative_upnl_reduces_available_margin():
    bot = FakeDryRunBot(wallet=60.0)

    opened = asyncio.run(bot.execute_order(market_order(qty=8.0, price=10.0)))
    assert opened["status"] == "closed"

    bot.cm.prices["BTC/USDC:USDC"] = 8.0
    created = asyncio.run(bot.execute_order(limit_order(qty=1.0, price=10.0)))

    assert created["status"] == "rejected"
    assert "insufficient margin" in created["reason"]


def test_missing_marks_fall_back_to_entry_price():
    bot = FakeDryRunBot()

    opened = asyncio.run(bot.execute_order(market_order(qty=4.0, price=10.0)))
    assert opened["status"] == "closed"

    bot.cm = FakeCM({})
    positions, balance = asyncio.run(bot.fetch_positions())
    created = asyncio.run(bot.execute_order(limit_order(qty=3.0, price=10.0)))

    assert positions[0]["price"] == pytest.approx(10.0)
    assert balance == pytest.approx(99.92)
    assert created["status"] == "open"
