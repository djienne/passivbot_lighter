#!/usr/bin/env python3
"""
Passivbot Balance Calculator - Simple Version (No Exchange API Required)

Calculates the minimum required balance for a given passivbot configuration.
Uses manual min_order_price instead of fetching from exchange.

Usage:
    python calculate_balance_simple.py --config configs/config_hype.json
    python calculate_balance_simple.py --config configs/config_hype.json --min-price 11
    python calculate_balance_simple.py --config configs/config_hype.json --buffer 0.2

The calculator uses the formula:
    wallet_exposure_per_position = total_wallet_exposure_limit / n_positions
    required_balance = min_order_price / (wallet_exposure_per_position * entry_initial_qty_pct)

NOTE: This simple calculator cannot verify min_qty constraints or apply exchange-specific
min_cost defaults without live exchange data. The real trading engine computes:
    effective_min_cost = max(min_qty * price * contract_size, min_cost)
For accurate results, prefer the full calculator: calculate_required_balance.py
"""

import argparse
import json
import sys
from pathlib import Path
from decimal import Decimal, ROUND_UP
from typing import Dict, Any


class SimpleBalanceCalculator:
    def __init__(self, config_path: str, min_order_price: float = None, buffer: float = 0.1):
        self.config_path = Path(config_path)
        self.config = self.load_config()
        self.min_order_price = min_order_price
        self.buffer = buffer

    def load_config(self) -> Dict[str, Any]:
        """Load and parse the configuration file."""
        if not self.config_path.exists():
            print(f"Error: Config file not found: {self.config_path}")
            sys.exit(1)

        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in config file: {e}")
            sys.exit(1)

    def get_approved_coins(self) -> Dict[str, list]:
        """Get approved coins from config."""
        approved = self.config.get("live", {}).get("approved_coins", {})
        return {
            "long": approved.get("long", []),
            "short": approved.get("short", [])
        }

    def calculate_balance_for_side(self, side: str, min_price: float) -> Dict[str, Any]:
        """Calculate required balance for a specific side."""
        bot_config = self.config.get("bot", {}).get(side, {})

        n_positions = bot_config.get("n_positions", 0)
        total_wallet_exposure_limit = bot_config.get("total_wallet_exposure_limit", 0)
        entry_initial_qty_pct = bot_config.get("entry_initial_qty_pct", 0.01)

        if n_positions == 0 or total_wallet_exposure_limit == 0:
            return None

        # Use Decimal for precise calculations
        twe = Decimal(str(total_wallet_exposure_limit))
        n_pos = Decimal(str(n_positions))
        entry_pct = Decimal(str(entry_initial_qty_pct))
        min_order_price_dec = Decimal(str(min_price))

        # Calculate wallet exposure per position
        we_per_position = twe / n_pos

        # Calculate required balance
        # Formula: min_order_price / (wallet_exposure_per_position * entry_initial_qty_pct)
        required_balance = min_order_price_dec / (we_per_position * entry_pct)

        # Add buffer and round up to nearest 10
        balance_with_buffer = required_balance * Decimal(str(1 + self.buffer))
        recommended = (balance_with_buffer / Decimal('10')).quantize(Decimal('1'), rounding=ROUND_UP) * Decimal('10')

        return {
            "side": side,
            "min_order_price": float(min_order_price_dec),
            "total_wallet_exposure_limit": float(twe),
            "n_positions": int(n_pos),
            "entry_initial_qty_pct": float(entry_pct),
            "wallet_exposure_per_position": float(we_per_position),
            "required_balance": float(required_balance),
            "recommended_balance": int(recommended),
            "buffer_pct": self.buffer * 100
        }

    def calculate(self) -> Dict[str, Any]:
        """Calculate required balance for both sides."""
        approved_coins = self.get_approved_coins()

        # Determine min_order_price
        if self.min_order_price is None:
            # Try to get from backtest starting_balance or use default
            self.min_order_price = 10.0  # Default
            print(f"Warning: No min_order_price provided, using default ${self.min_order_price}")

        print(f"\n{'='*80}")
        print(f"PASSIVBOT BALANCE CALCULATOR".center(80))
        print(f"{'='*80}\n")
        print(f"Config: {self.config_path.name}")
        print(f"Min Order Price: ${self.min_order_price}")
        print(f"Buffer: {self.buffer * 100:.0f}%")
        print(f"Approved Coins (Long): {', '.join(approved_coins['long']) if approved_coins['long'] else 'None'}")
        print(f"Approved Coins (Short): {', '.join(approved_coins['short']) if approved_coins['short'] else 'None'}")

        results = {}

        # Calculate for long side
        if approved_coins["long"]:
            long_result = self.calculate_balance_for_side("long", self.min_order_price)
            if long_result:
                results["long"] = long_result

        # Calculate for short side
        if approved_coins["short"]:
            short_result = self.calculate_balance_for_side("short", self.min_order_price)
            if short_result:
                results["short"] = short_result

        return results

    def print_results(self, results: Dict[str, Any]):
        """Print calculation results in a formatted table."""
        if not results:
            print("\nNo active positions configured (n_positions = 0 for both sides)")
            return

        print(f"\n{'='*80}")
        print(f"CALCULATION RESULTS".center(80))
        print(f"{'='*80}\n")

        max_required = 0
        max_side = None

        for side, result in results.items():
            print(f"{'-'*80}")
            print(f"{side.upper()} SIDE".center(80))
            print(f"{'-'*80}")
            print(f"  Minimum Order Price:               ${result['min_order_price']:.2f}")
            print(f"  Total Wallet Exposure Limit:       {result['total_wallet_exposure_limit']:.2f}")
            print(f"  Number of Positions:               {result['n_positions']}")
            print(f"  Entry Initial Qty %:               {result['entry_initial_qty_pct']:.6f} ({result['entry_initial_qty_pct']*100:.4f}%)")
            print(f"  Wallet Exposure per Position:      {result['wallet_exposure_per_position']:.4f}")
            print()
            print(f"  Formula:")
            print(f"    required_balance = min_order_price / (wallet_exposure_per_position * entry_initial_qty_pct)")
            print()
            print(f"  Calculation:")
            print(f"    = {result['min_order_price']:.2f} / ({result['wallet_exposure_per_position']:.4f} * {result['entry_initial_qty_pct']:.6f})")
            print(f"    = {result['min_order_price']:.2f} / {result['wallet_exposure_per_position'] * result['entry_initial_qty_pct']:.8f}")
            print(f"    = ${result['required_balance']:.2f}")
            print()
            print(f"  => Required Balance (minimum):      ${result['required_balance']:.2f} USDT")
            print(f"  => Recommended Balance (+{result['buffer_pct']:.0f}%):      ${result['recommended_balance']:.0f} USDT")
            print()

            if result['required_balance'] > max_required:
                max_required = result['required_balance']
                max_side = result

        # Print final recommendation
        print(f"{'='*80}")
        if max_side:
            print(f"FINAL RECOMMENDATION: Start with at least ${max_side['recommended_balance']:.0f} USDT".center(80))
        print(f"{'='*80}\n")

        # Additional notes
        drawdown = self.config.get('analysis', {}).get('drawdown_worst', 0)
        if drawdown > 0:
            print("Additional Considerations:")
            print(f"  - Backtest max drawdown: {drawdown*100:.2f}%")
            print(f"  - Consider adding extra buffer for drawdowns")

        print("  - This covers the INITIAL ENTRY order only")
        print("  - Grid entries (DCA) will use more capital as position grows")
        print(f"  - Position can grow up to {max_side['wallet_exposure_per_position']:.2f}x wallet balance")
        print(f"  - With leverage {self.config.get('live', {}).get('leverage', 10)}x, you need ~{max_side['wallet_exposure_per_position']/self.config.get('live', {}).get('leverage', 10)*100:.1f}% of exposure as margin")
        print()
        print("  NOTE: This simple calculator does not verify min_qty * price constraints")
        print("  or apply exchange-specific min_cost defaults. For accurate results,")
        print("  use: python calculate_required_balance.py --config <config>")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Calculate required balance for a passivbot configuration (simple version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python calculate_balance_simple.py --config configs/config_hype.json --min-price 11
  python calculate_balance_simple.py --config configs/config_hype.json --min-price 11 --buffer 0.2

The calculator will:
  1. Read your config file
  2. Use the provided min_order_price (or default $10)
  3. Calculate minimum required balance
  4. Add a safety buffer (default 10%)
  5. Show detailed breakdown and recommendation
        """
    )

    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Path to passivbot config file (e.g., configs/config_hype.json)"
    )

    parser.add_argument(
        "--min-price",
        "-m",
        type=float,
        help="Minimum order price in USDT (e.g., 11 for HYPE)"
    )

    parser.add_argument(
        "--buffer",
        "-b",
        type=float,
        default=0.1,
        help="Safety buffer percentage (default: 0.1 = 10%%)"
    )

    args = parser.parse_args()

    try:
        calculator = SimpleBalanceCalculator(
            config_path=args.config,
            min_order_price=args.min_price,
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
