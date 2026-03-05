from copy import deepcopy

from config_utils import parse_overrides
from passivbot import Passivbot


class _ConcreteBot(Passivbot):
    """Minimal concrete subclass for testing (satisfies ABC)."""
    def create_ccxt_sessions(self): pass
    async def close(self): pass
    async def fetch_positions(self): return [], 0.0
    async def fetch_open_orders(self, symbol=None): return []
    async def fetch_tickers(self): return {}
    async def fetch_ohlcv(self, symbol, timeframe="1m"): return []
    async def fetch_ohlcvs_1m(self, symbol, since=None, limit=None): return []
    async def fetch_pnls(self, start_time=None, end_time=None, limit=None): return []
    async def watch_orders(self): pass


def test_coin_override_forced_mode_manual(monkeypatch):
    base_config = {
        "bot": {"long": {}, "short": {}},
        "live": {
            "user": "dummy",
            "forced_mode_long": "",
            "forced_mode_short": "",
        },
        "coin_overrides": {
            "DOGEUSDT": {
                "live": {
                    "forced_mode_long": "manual",
                }
            }
        },
    }

    config = parse_overrides(deepcopy(base_config), verbose=False)
    assert "DOGE" in config["coin_overrides"]

    bot = _ConcreteBot.__new__(_ConcreteBot)
    bot.config = config
    bot.exchange = "binance"
    bot.markets_dict = {"DOGE/USDT:USDT": {"active": True}}

    def fake_coin_to_symbol(self, coin, verbose=True):
        if coin in {"DOGE", "DOGEUSDT"}:
            return "DOGE/USDT:USDT"
        return ""

    bot.coin_to_symbol = fake_coin_to_symbol.__get__(bot, Passivbot)
    bot.init_coin_overrides()

    assert "DOGE/USDT:USDT" in bot.coin_overrides
    assert bot.get_forced_PB_mode("long", "DOGE/USDT:USDT") == "manual"
