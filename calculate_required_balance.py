#!/usr/bin/env python3
"""
Passivbot Balance Calculator - Command Line Version

Calculates the minimum required balance for a given passivbot configuration.
Supports both Hyperliquid (via ccxt) and Lighter (via SDK / cached metadata).

Usage:
    python calculate_required_balance.py --config configs/hype_top.json
    python calculate_required_balance.py --config configs/config_hype.json --buffer 0.2  # 20% buffer

The calculator uses the formula matching the real trading engine (src/passivbot.py):
    effective_min_cost = max(min_qty * price * contract_size, min_cost)
    wallet_exposure_per_position = total_wallet_exposure_limit / n_positions
    required_balance = effective_min_cost / (wallet_exposure_per_position * entry_initial_qty_pct)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from decimal import Decimal, ROUND_UP
from typing import Dict, List, Any, Optional


def detect_exchange(config: dict) -> str:
    """Detect exchange from config user field."""
    user = config.get("live", {}).get("user", "")
    if user.startswith("lighter"):
        return "lighter"
    return "hyperliquid"


class LighterMarketProvider:
    """Fetch market info from Lighter SDK or cached metadata."""

    CACHE_PATH = os.path.join("caches", "lighter", "market_metadata.json")
    CACHE_MAX_AGE_S = 3600

    # Max leverage overrides — SDK doesn't expose this field
    MAX_LEVERAGE_OVERRIDES = {"HYPE": 20}
    MAX_LEVERAGE_DEFAULT = 20

    MIN_COST_FLOOR = 10.0
    EFFECTIVE_MIN_COST_MULTIPLIER = 1.01  # matches update_effective_min_cost

    def __init__(self):
        self.markets = {}

    def _load_cache(self) -> bool:
        try:
            if not os.path.exists(self.CACHE_PATH):
                return False
            with open(self.CACHE_PATH) as f:
                cache = json.load(f)
            age = time.time() - cache.get("timestamp", 0)
            if age > self.CACHE_MAX_AGE_S:
                return False
            self.markets = cache.get("markets", {})
            print(f"  Loaded market metadata from cache ({len(self.markets)} markets, {age:.0f}s old)")
            return bool(self.markets)
        except Exception:
            return False

    async def _fetch_live(self) -> bool:
        try:
            import lighter
        except ImportError:
            print("Error: lighter SDK not installed")
            return False

        config = lighter.Configuration(host="https://mainnet.zklighter.elliot.ai")
        api_client = lighter.ApiClient(configuration=config)
        order_api = lighter.OrderApi(api_client)
        try:
            resp = await order_api.order_books()
            for ob in resp.order_books:
                symbol_base = ob.symbol.upper()
                symbol = f"{symbol_base}/USDC:USDC"
                price_decimals = ob.supported_price_decimals
                size_decimals = ob.supported_size_decimals
                min_base = float(ob.min_base_amount) if getattr(ob, "min_base_amount", None) else 10 ** (-size_decimals)
                min_quote = float(ob.min_quote_amount) if getattr(ob, "min_quote_amount", None) else 1.0
                max_lev = self.MAX_LEVERAGE_OVERRIDES.get(symbol_base, self.MAX_LEVERAGE_DEFAULT)
                self.markets[symbol] = {
                    "symbol": symbol,
                    "base": symbol_base,
                    "quote": "USDC",
                    "market_id": int(ob.market_id),
                    "price_decimals": price_decimals,
                    "size_decimals": size_decimals,
                    "maxLeverage": max_lev,
                    "min_base": min_base,
                    "min_quote": min_quote,
                }
            print(f"  Fetched {len(self.markets)} markets from Lighter API")
            return bool(self.markets)
        finally:
            await api_client.close()

    async def load_markets(self):
        if not self._load_cache():
            if not await self._fetch_live():
                print("Error: Could not load Lighter market data")
                sys.exit(1)

    def get_symbol_info(self, coin: str, price: float) -> Optional[Dict[str, Any]]:
        symbol = f"{coin}/USDC:USDC"
        m = self.markets.get(symbol)
        if not m:
            return None

        min_base = float(m["min_base"])
        min_quote = max(self.MIN_COST_FLOOR, float(m["min_quote"]))
        amount_tick = 10 ** (-int(m["size_decimals"]))
        contract_size = 1.0

        # effective_min_cost = max(min_qty * price * c_mult, min_cost) * multiplier
        min_cost_from_qty = min_base * price * contract_size
        effective_min_cost = max(min_cost_from_qty, min_quote) * self.EFFECTIVE_MIN_COST_MULTIPLIER

        return {
            "symbol": coin,
            "symbol_formatted": symbol,
            "price": price,
            "min_order_price": effective_min_cost,
            "min_cost": min_quote,
            "min_amount": min_base,
            "contract_size": contract_size,
            "max_leverage": int(m["maxLeverage"]),
        }


class BalanceCalculator:
    def __init__(self, config_path: str, buffer: float = 0.1):
        self.config_path = Path(config_path)
        self.config = self.load_config()
        self.exchange_id = detect_exchange(self.config)
        self.buffer = buffer
        self.exchange = None  # ccxt exchange, only for hyperliquid
        self.lighter_provider = None

    def load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            print(f"Error: Config file not found: {self.config_path}")
            sys.exit(1)
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in config file: {e}")
            sys.exit(1)

    def init_ccxt_exchange(self):
        try:
            import ccxt
        except ImportError:
            print("Error: ccxt library not installed")
            sys.exit(1)
        try:
            exchange_class = getattr(ccxt, "hyperliquid")
            self.exchange = exchange_class({
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'}
            })
        except AttributeError:
            print("Error: Exchange 'hyperliquid' not found in ccxt")
            sys.exit(1)

    def _apply_exchange_min_cost_default(self, min_cost) -> float:
        if min_cost is None:
            min_cost = 10.0
        min_cost = round(min_cost * 1.01, 2)
        return min_cost

    def get_approved_coins(self) -> Dict[str, List[str]]:
        approved = self.config.get("live", {}).get("approved_coins", {})
        return {
            "long": approved.get("long", []),
            "short": approved.get("short", [])
        }

    def fetch_price_from_binance(self, coin: str) -> Optional[float]:
        """Fetch price from Binance as a fallback for Lighter."""
        try:
            import ccxt
            binance = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
            binance.load_markets()
            symbol = f"{coin}/USDT:USDT"
            if symbol not in binance.markets:
                symbol = f"{coin}/USDC:USDC"
            if symbol not in binance.markets:
                return None
            ticker = binance.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            print(f"  Warning: Could not fetch price from Binance for {coin}: {e}")
            return None

    def fetch_symbol_info_hyperliquid(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            coin = symbol
            if '/' not in coin:
                for suffix in ["USDT", "USDC", "BUSD", "USD", ":"]:
                    coin = coin.replace(suffix, "")
                symbol_formatted = f"{coin}/USDC:USDC"
            else:
                symbol_formatted = symbol

            self.exchange.load_markets()
            if symbol_formatted not in self.exchange.markets:
                symbol_formatted = f"{coin}/USDC"
                if symbol_formatted not in self.exchange.markets:
                    return None

            market = self.exchange.markets[symbol_formatted]
            ticker = self.exchange.fetch_ticker(symbol_formatted)
            price = ticker['last']

            min_cost = market.get('limits', {}).get('cost', {}).get('min')
            min_amount = market.get('limits', {}).get('amount', {}).get('min')
            contract_size = market.get('contractSize', 1) or 1
            min_cost = self._apply_exchange_min_cost_default(min_cost)

            min_cost_from_qty = (min_amount * price * contract_size) if (min_amount and min_amount > 0 and price) else 0
            min_cost_flat = min_cost if (min_cost and min_cost > 0) else 0
            min_order_price = max(min_cost_from_qty, min_cost_flat)
            if min_order_price <= 0:
                min_order_price = 5.0

            return {
                "symbol": symbol,
                "symbol_formatted": symbol_formatted,
                "price": price,
                "min_order_price": min_order_price,
                "min_cost": min_cost,
                "min_amount": min_amount,
                "contract_size": contract_size,
                "max_leverage": market.get('limits', {}).get('leverage', {}).get('max', 10)
            }
        except Exception as e:
            print(f"Warning: Could not fetch info for {symbol}: {e}")
            return None

    def fetch_symbol_info(self, coin: str) -> Optional[Dict[str, Any]]:
        if self.exchange_id == "lighter":
            price = self.fetch_price_from_binance(coin)
            if price is None:
                print(f"  Could not get price for {coin}")
                return None
            return self.lighter_provider.get_symbol_info(coin, price)
        else:
            return self.fetch_symbol_info_hyperliquid(coin)

    def calculate_balance_for_coin(self, symbol: str, side: str, symbol_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        bot_config = self.config.get("bot", {}).get(side, {})
        n_positions = bot_config.get("n_positions", 0)
        total_wallet_exposure_limit = bot_config.get("total_wallet_exposure_limit", 0)
        entry_initial_qty_pct = bot_config.get("entry_initial_qty_pct", 0.01)

        if n_positions == 0 or total_wallet_exposure_limit == 0:
            return None

        twe = Decimal(str(total_wallet_exposure_limit))
        n_pos = Decimal(str(n_positions))
        entry_pct = Decimal(str(entry_initial_qty_pct))
        min_price = Decimal(str(symbol_info['min_order_price']))

        we_per_position = twe / n_pos
        required_balance = min_price / (we_per_position * entry_pct)

        balance_with_buffer = required_balance * Decimal(str(1 + self.buffer))
        recommended = (balance_with_buffer / Decimal('10')).quantize(Decimal('1'), rounding=ROUND_UP) * Decimal('10')

        return {
            "symbol": symbol,
            "side": side,
            "min_order_price": float(min_price),
            "current_price": symbol_info['price'],
            "total_wallet_exposure_limit": float(twe),
            "n_positions": int(n_pos),
            "entry_initial_qty_pct": float(entry_pct),
            "wallet_exposure_per_position": float(we_per_position),
            "required_balance": float(required_balance),
            "recommended_balance": int(recommended),
            "buffer_pct": self.buffer * 100
        }

    async def _init_lighter(self):
        self.lighter_provider = LighterMarketProvider()
        await self.lighter_provider.load_markets()

    def calculate(self) -> List[Dict[str, Any]]:
        approved_coins = self.get_approved_coins()
        all_coins = set(approved_coins["long"] + approved_coins["short"])

        if not all_coins:
            print("Error: No approved coins found in config")
            sys.exit(1)

        if self.exchange_id == "lighter":
            asyncio.run(self._init_lighter())
        else:
            self.init_ccxt_exchange()

        results = []
        print(f"\nExchange: {self.exchange_id}")
        print(f"Approved coins: {', '.join(sorted(all_coins))}\n")

        for coin in sorted(all_coins):
            print(f"  Fetching {coin}...", end=" ")
            symbol_info = self.fetch_symbol_info(coin)

            if not symbol_info:
                print("FAILED")
                continue

            print(f"OK (${symbol_info['price']:.4f})")

            if coin in approved_coins["long"]:
                result = self.calculate_balance_for_coin(coin, "long", symbol_info)
                if result:
                    results.append(result)

            if coin in approved_coins["short"]:
                result = self.calculate_balance_for_coin(coin, "short", symbol_info)
                if result:
                    results.append(result)

        return results

    def print_results(self, results: List[Dict[str, Any]]):
        if not results:
            print("\nNo results to display")
            return

        highest = max(results, key=lambda x: x['required_balance'])
        quote = "USDC" if self.exchange_id == "lighter" else "USDT"

        print("\n" + "=" * 100)
        print("BALANCE CALCULATION RESULTS".center(100))
        print("=" * 100)
        print(f"\nConfig: {self.config_path.name}")
        print(f"Exchange: {self.exchange_id}")
        print(f"Buffer: {self.buffer * 100:.0f}%\n")

        print("-" * 100)
        print(f"HIGHEST REQUIREMENT: {highest['symbol']} ({highest['side'].upper()} side)".center(100))
        print("-" * 100)
        print(f"  Current Price:                     ${highest['current_price']:.4f}")
        print(f"  Effective Min Cost:                ${highest['min_order_price']:.2f}")
        print(f"  Total Wallet Exposure Limit:       {highest['total_wallet_exposure_limit']:.2f}")
        print(f"  Number of Positions:               {highest['n_positions']}")
        print(f"  Entry Initial Qty %:               {highest['entry_initial_qty_pct']:.4f} ({highest['entry_initial_qty_pct']*100:.2f}%)")
        print(f"  Wallet Exposure per Position:      {highest['wallet_exposure_per_position']:.4f}")
        print()
        print(f"  Formula: effective_min_cost / (wallet_exposure_per_position x entry_initial_qty_pct)")
        print(f"  Calculation: {highest['min_order_price']:.2f} / ({highest['wallet_exposure_per_position']:.4f} x {highest['entry_initial_qty_pct']:.4f})")
        print(f"  = {highest['min_order_price']:.2f} / {highest['wallet_exposure_per_position'] * highest['entry_initial_qty_pct']:.6f}")
        print(f"  = ${highest['required_balance']:.2f}")
        print()
        print(f"  -> Required Balance (minimum):      ${highest['required_balance']:.2f}")
        print(f"  -> Recommended Balance (+{self.buffer*100:.0f}%):      ${highest['recommended_balance']:.0f} {quote}")
        print("-" * 100)

        if len(results) > 1:
            print("\nALL COINS SUMMARY:")
            print("-" * 100)
            print(f"{'Symbol':<10} {'Side':<6} {'Price':<12} {'Min Cost':<12} {'Required':<14} {'Recommended':<14}")
            print("-" * 100)

            for r in sorted(results, key=lambda x: x['required_balance'], reverse=True):
                print(f"{r['symbol']:<10} {r['side']:<6} ${r['current_price']:<11.4f} ${r['min_order_price']:<11.2f} ${r['required_balance']:<13.2f} ${r['recommended_balance']:<13.0f}")

            print("-" * 100)

        print("\n" + "=" * 100)
        print(f"FINAL RECOMMENDATION: Start with at least ${highest['recommended_balance']:.0f} {quote}".center(100))
        print("=" * 100)
        print()
        print("Note: This calculation ensures you can place the initial entry order.")
        print("      Consider additional buffer for:")
        print("      - Grid entries (DCA)")
        print(f"      - Drawdown safety (backtest showed {self.config.get('analysis', {}).get('drawdown_worst', 0)*100:.1f}% max drawdown)")
        print("      - Multiple positions if n_positions > 1")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Calculate required balance for a passivbot configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python calculate_required_balance.py --config configs/hype_top.json
  python calculate_required_balance.py --config configs/config_hype.json --buffer 0.2

The calculator will:
  1. Read your config file and detect the exchange (lighter or hyperliquid)
  2. Fetch current market data
  3. Calculate minimum required balance
  4. Add a safety buffer (default 10%)
  5. Show detailed breakdown and recommendation
        """
    )

    parser.add_argument(
        "--config", "-c",
        required=True,
        help="Path to passivbot config file (e.g., configs/hype_top.json)"
    )

    parser.add_argument(
        "--buffer", "-b",
        type=float,
        default=0.1,
        help="Safety buffer percentage (default: 0.1 = 10%%)"
    )

    args = parser.parse_args()

    try:
        calculator = BalanceCalculator(
            config_path=args.config,
            buffer=args.buffer
        )
        results = calculator.calculate()
        calculator.print_results(results)

    except KeyboardInterrupt:
        print("\n\nCalculation interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
