"""Live-bot walk-forward rolling tests (no exchange).

Exercises the two pieces of live wiring in src/passivbot.py:
- _maybe_adopt_wfo_config: swaps ONLY the strategy (bot section) from the published
  config, keeping operator credentials/live settings, rebuilding from the pristine base.
- _wfo_wind_down: engages tp_only and force-closes (panic) positions that are in profit
  or whose unrealized loss is < max_loss_flatten_frac of total wallet equity.
"""

import asyncio
import json

import pytest

passivbot = pytest.importorskip("passivbot")


def _pristine(active_dir):
    return {
        "live": {
            "wfo_rolling": {"enabled": True, "active_dir": str(active_dir),
                            "max_loss_flatten_frac": 0.05},
            "forced_mode_long": "",
            "forced_mode_short": "",
        },
        "bot": {"long": {"x": 0.0}, "short": {}},
    }


class TestMaybeAdoptConfig:
    def test_no_active_returns_base_strategy(self, tmp_path):
        cfg = passivbot._maybe_adopt_wfo_config(_pristine(tmp_path / "live"))
        assert cfg["bot"]["long"]["x"] == 0.0
        assert "_wfo_loaded_period" not in cfg

    def test_swaps_bot_only_keeps_operator_live(self, tmp_path):
        active = tmp_path / "live"
        active.mkdir()
        (active / "active_config.json").write_text(json.dumps({
            "bot": {"long": {"x": 9.0}, "short": {}},
            "live": {"user": "OTHER_ACCOUNT"},  # must NOT leak into the run config
        }), encoding="utf-8")
        (active / "active.json").write_text(json.dumps({
            "period": "2025-08-01..2025-09-01",
            "chosen_hash": "h1",
            "config_path": str(active / "active_config.json"),
        }), encoding="utf-8")

        pristine = _pristine(active)
        cfg = passivbot._maybe_adopt_wfo_config(pristine)

        assert cfg["bot"]["long"]["x"] == 9.0                 # strategy rolled in
        assert cfg["live"]["wfo_rolling"]["enabled"] is True  # operator live settings kept
        assert "user" not in cfg["live"]                      # published live section NOT merged
        assert cfg["_wfo_loaded_period"] == "2025-08-01..2025-09-01"
        # pristine base is untouched (no leakage across iterations)
        assert pristine["bot"]["long"]["x"] == 0.0


def _fake_bot():
    bot = passivbot.Passivbot.__new__(passivbot.Passivbot)
    bot.config = {"live": {"forced_mode_long": "", "forced_mode_short": ""}}
    bot.coin_overrides = {}
    bot.balance = 1000.0
    bot.inverse = False
    bot.c_mults = {"BTC/USDT:USDT": 1.0, "ETH/USDT:USDT": 1.0}
    bot.fetched_positions = [
        # long entry 100, mark 90 -> uPnL -10 (small loss) -> flatten
        {"symbol": "BTC/USDT:USDT", "position_side": "long", "size": 1.0, "price": 100.0},
        # long entry 100, mark 50 -> uPnL -50 (big loss) -> keep
        {"symbol": "ETH/USDT:USDT", "position_side": "long", "size": 1.0, "price": 100.0},
    ]

    async def fake_upnl():
        return -60.0

    bot.calc_upnl_sum = fake_upnl

    class _CM:
        async def get_last_prices(self, syms, max_age_ms=0):
            return {"BTC/USDT:USDT": 90.0, "ETH/USDT:USDT": 50.0}

    bot.cm = _CM()
    return bot


class TestWindDown:
    def test_tp_only_and_selective_flatten(self):
        bot = _fake_bot()
        asyncio.run(bot._wfo_wind_down({"max_loss_flatten_frac": 0.05}))
        # global: no new entries
        assert bot.config["live"]["forced_mode_long"] == "tp_only"
        assert bot.config["live"]["forced_mode_short"] == "tp_only"
        # equity = 1000 + (-60) = 940; 5% threshold = 47.
        # BTC loss 10 < 47 -> panic (flatten); ETH loss 50 >= 47 -> kept.
        assert bot.coin_overrides["BTC/USDT:USDT"]["live"]["forced_mode_long"] == "panic"
        assert "ETH/USDT:USDT" not in bot.coin_overrides

    def test_no_positions_just_engages_tp_only(self):
        bot = _fake_bot()
        bot.fetched_positions = []
        asyncio.run(bot._wfo_wind_down({"max_loss_flatten_frac": 0.05}))
        assert bot.config["live"]["forced_mode_long"] == "tp_only"
        assert bot.coin_overrides == {}
