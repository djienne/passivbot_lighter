# Passivbot Balance Guide

This guide explains key passivbot concepts, how available capital is determined, and how to calculate the minimum required balance for your configuration.

---

## Key Passivbot Concepts

### Wallet Exposure

**Wallet exposure** measures a position's size relative to your wallet balance.

**Formula:**
```
wallet_exposure = (position_size * position_price) / unleveraged_wallet_balance
```

**Examples:**
- `wallet_exposure = 0.0` → No position
- `wallet_exposure = 1.0` → Position value = 100% of wallet balance
- `wallet_exposure = 2.0` → Position value = 200% of wallet balance (2x leveraged)
- `wallet_exposure = 4.0` → Position value = 400% of wallet balance (4x leveraged)

**Concrete Example:**
```
Wallet balance: $1,000
Long position: 100 coins @ $35/coin
Position value: 100 * $35 = $3,500
Wallet exposure: $3,500 / $1,000 = 3.5
```

### Total Wallet Exposure Limit

The **`total_wallet_exposure_limit`** is the maximum wallet exposure allowed across all positions on one side (long or short).

**Key Points:**
- Set separately for long and short sides in config
- Controls maximum position size
- With multiple positions, each gets: `total_wallet_exposure_limit / n_positions`

**Example from config_hype.json:**
```json
"bot": {
    "long": {
        "total_wallet_exposure_limit": 2.0,  // Max 2x wallet in long positions
        "n_positions": 1                     // One position
    },
    "short": {
        "total_wallet_exposure_limit": 0.0,  // No short positions
        "n_positions": 0
    }
}
```

**Interpretation:**
- With $100 wallet and `total_wallet_exposure_limit = 2.0`
- Maximum long position value: $200
- Each position can reach: $200 / 1 = $200 (since n_positions = 1)

### Leverage

Passivbot uses **unleveraged wallet balance** in all calculations. Exchange leverage only affects margin requirements.

**Important:**
- **Minimum leverage needed:** `total_wallet_exposure_limit_long + total_wallet_exposure_limit_short`
- Higher leverage = less margin required, but same position sizes
- Changing leverage doesn't change bot behavior

**Example:**
```
Config: total_wallet_exposure_limit = 2.0 (long) + 0.0 (short)
Minimum leverage: 2x
Recommended: 10x (provides safety margin)

With $100 wallet and 2.0 exposure limit:
- Max position value: $200
- With 10x leverage: $20 margin required
- With 2x leverage: $100 margin required (risky - no room for error)
```

### Entry Initial Qty Pct

**`entry_initial_qty_pct`** determines the size of your first entry order.

**Formula:**
```
initial_entry_size = wallet_balance * wallet_exposure_per_position * entry_initial_qty_pct
```

**Example:**
```
Wallet: $100
Wallet exposure per position: 2.0
Entry initial qty pct: 0.14615 (14.615%)

Initial entry: $100 * 2.0 * 0.14615 = $29.23
```

This is the **first order** placed when entering a position. The position will grow through grid entries (DCA) up to the wallet exposure limit.

### Grid Entries (DCA - Dollar Cost Averaging)

Passivbot uses a **grid strategy** to average down (or up for shorts) when price moves against you.

**How it works:**
1. **Initial entry**: Small position (based on `entry_initial_qty_pct`)
2. **Grid entries**: Additional orders at lower prices (for longs)
3. **Position grows**: Each grid entry adds to position, averaging down the entry price
4. **Stops at limit**: When `wallet_exposure_limit` is reached

**Key parameters:**
- `entry_grid_spacing_pct`: Distance between grid levels
- `entry_grid_double_down_factor`: How much larger each grid entry is
- Position grows until hitting `total_wallet_exposure_limit`

### Getting Stuck

A position is **"stuck"** when:
- Wallet exposure limit is reached
- Price hasn't bounced back to profitable exit
- No more entries can be placed

**Unstuck mechanism:**
- Config parameter: `unstuck_threshold` (e.g., 0.79883 = 79.883%)
- When position is stuck and losing, bot may add to position to lower average entry price
- Allows closing on smaller bounces
- Risk: Increases position size further

### Bankruptcy and Liquidation

**Bankruptcy:** When `equity = balance + unrealized_PnL = 0`

**Liquidation distance examples:**
- `wallet_exposure = 1.0` → Bankruptcy at 100% price drop (price → $0)
- `wallet_exposure = 2.0` → Bankruptcy at 50% price drop
- `wallet_exposure = 3.0` → Bankruptcy at 33.33% price drop
- `wallet_exposure = 10.0` → Bankruptcy at 10% price drop

**Important:** Exchange liquidates *before* bankruptcy to cover fees/slippage.

**Why lower exposure = safer:**
Higher wallet exposure means less room for adverse price movement before liquidation.

### Number of Positions

**`n_positions`**: How many concurrent positions allowed on each side.

**Example:**
```json
"n_positions": 1  // One position at a time
"total_wallet_exposure_limit": 2.0
```
→ Single position can use full 2.0x exposure

```json
"n_positions": 5  // Five positions simultaneously
"total_wallet_exposure_limit": 2.0
```
→ Each position gets 2.0 / 5 = 0.4x exposure limit

**Benefits of multiple positions:**
- Diversification across coins
- Reduces risk of one bad position destroying account
- Spreads risk across different price actions

---

## How Available Capital is Determined

### Live Trading (Real Exchange)

**The balance is AUTOMATICALLY fetched from your exchange account** - no manual configuration needed!

#### How it works:
1. Bot connects to exchange via API credentials (from `api-keys.json`)
2. Fetches current balance from exchange using `fetch_positions()`
3. Updates internal balance with your actual USDT balance
4. Balance is continuously monitored and updated via websocket

#### Code Reference:
```python
# src/passivbot.py:1974-1977
positions_list_new, balance_new = res  # Fetched from exchange
self.handle_balance_update({self.quote: {"total": balance_new}}, source="REST")
self.balance = max(upd[self.quote]["total"], 1e-12)  # Updated
```

**You only need to:**
- Ensure you have sufficient USDT in your exchange account
- Configure API keys in `api-keys.json`
- The bot will automatically use your available balance

### Backtest Mode

Uses the `starting_balance` parameter from your config file:

```json
"backtest": {
    "starting_balance": 1000,  // Only used for backtesting simulations
    ...
}
```

This parameter **does NOT affect live trading** - it's only for backtesting historical data.

---

## Calculating Required Balance

### Formula

The minimum required balance is calculated using the formula from pbgui:

```
wallet_exposure_per_position = total_wallet_exposure_limit / n_positions

required_balance = min_order_notional / (wallet_exposure_per_position * entry_initial_qty_pct)
```

### What Each Parameter Means

**From your config (`bot.long` or `bot.short` section):**

- **`total_wallet_exposure_limit`**: Maximum position size as multiple of wallet balance
  - Example: 2.0 means position can grow up to 2x your wallet balance

- **`n_positions`**: Number of concurrent positions on this side
  - Example: 1 means one position at a time

- **`entry_initial_qty_pct`**: Percentage of wallet exposure used for initial entry
  - Example: 0.14615 means initial entry is 14.615% of max wallet exposure

**From the exchange:**

- **`min_order_notional`**: Minimum order value in USD required by the exchange
  - Example: For HYPE on Bybit, this is approximately $11

### Example Calculation

Using `configs/config_hype.json`:

```json
"bot": {
    "long": {
        "total_wallet_exposure_limit": 2.0,
        "n_positions": 1,
        "entry_initial_qty_pct": 0.14615,
        ...
    }
}
```

**Step 1: Calculate wallet exposure per position**
```
wallet_exposure_per_position = 2.0 / 1 = 2.0
```

**Step 2: Calculate required balance**
```
required_balance = 11 / (2.0 * 0.14615)
                 = 11 / 0.2923
                 = $37.63 USDT
```

**Step 3: Add safety buffer (recommended 10%)**
```
recommended_balance = $37.63 * 1.10
                    = $41.39
                    ≈ $50 USDT (rounded up to nearest $10)
```

### Interpretation

- **Minimum**: $37.63 USDT to place the initial entry order
- **Recommended**: $50 USDT (includes 10% safety buffer)

**Important Notes:**
- This covers the **INITIAL ENTRY** only
- Position can grow through grid entries (DCA) up to the wallet exposure limit
- With `total_wallet_exposure_limit = 2.0`, position can reach 2x your wallet balance
- With 10x leverage, you need ~20% of position size as margin (2.0 / 10 = 0.20)

---

## Using the Balance Calculator

### Command-Line Tool

A standalone calculator script is provided: `calculate_balance_simple.py`

**Usage:**
```bash
python calculate_balance_simple.py --config configs/config_hype.json --min-price 11
```

**Options:**
- `--config` or `-c`: Path to your config file (required)
- `--min-price` or `-m`: Minimum order notional in USD (required)
- `--buffer` or `-b`: Safety buffer percentage (default: 0.1 = 10%)

**Example with custom buffer:**
```bash
python calculate_balance_simple.py --config configs/config_hype.json --min-price 11 --buffer 0.2
```

This will add a 20% safety buffer instead of the default 10%.

### Output Example

```
================================================================================
                          PASSIVBOT BALANCE CALCULATOR
================================================================================

Config: config_hype.json
Min Order Price: $11.0
Buffer: 10%
Approved Coins (Long): HYPE
Approved Coins (Short): None

================================================================================
                              CALCULATION RESULTS
================================================================================

--------------------------------------------------------------------------------
                                   LONG SIDE
--------------------------------------------------------------------------------
  Minimum Order Price:               $11.00
  Total Wallet Exposure Limit:       2.00
  Number of Positions:               1
  Entry Initial Qty %:               0.146150 (14.6150%)
  Wallet Exposure per Position:      2.0000

  Formula:
    required_balance = min_order_price / (wallet_exposure_per_position * entry_initial_qty_pct)

  Calculation:
    = 11.00 / (2.0000 * 0.146150)
    = 11.00 / 0.29230000
    = $37.63

  => Required Balance (minimum):      $37.63 USDT
  => Recommended Balance (+10%):      $50 USDT

================================================================================
               FINAL RECOMMENDATION: Start with at least $50 USDT
================================================================================

Additional Considerations:
  - Backtest max drawdown: 19.80%
  - Consider adding extra buffer for drawdowns
  - This covers the INITIAL ENTRY order only
  - Grid entries (DCA) will use more capital as position grows
  - Position can grow up to 2.00x wallet balance
  - With leverage 10x, you need ~20.0% of exposure as margin
```

---

## Risk Considerations

### Drawdown Buffer

Your backtest results show a maximum drawdown. Consider adding extra capital to handle drawdowns safely:

**Example from `config_hype.json`:**
- Max drawdown: 19.80%
- If starting with $50, a 19.8% drawdown = $9.90 loss
- Account would be at $40.10 (still above minimum $37.63)

**Recommended approach:**
- Minimum for initial order: $37.63
- With 10% buffer: $50
- **With drawdown safety (30% total buffer): $65-$100**

### Position Growth

Remember that positions grow through grid entries:

1. **Initial entry**: 14.615% of wallet exposure = $14.62 (with $50 wallet)
2. **Max position size**: 2.0x wallet = $100 (with $50 wallet)
3. **Actual capital needed**: $100 / 10 (leverage) = $10 in margin

The bot will add to the position (average down) if price moves against you, up to the wallet exposure limit.

### Multiple Positions

If running multiple positions (`n_positions > 1`):
- Each position gets `total_wallet_exposure_limit / n_positions`
- Calculate required balance for the coin with highest min_order_notional
- All positions share the same wallet balance

---

## Finding Min Order Notional

The minimum order size varies by exchange and coin:

### Method 1: Exchange Documentation
- **Bybit**: Check Trading Rules for each symbol
- **Binance**: Usually $5-$10 for most perpetuals
- **OKX**: Check contract specifications

### Method 2: pbgui Balance Calculator (with live data)
```bash
cd ../pbgui
streamlit run BalanceCalculator.py
```
This fetches live data from the exchange API.

### Method 3: Full Calculator Script (requires ccxt)
```bash
pip install ccxt
python calculate_required_balance.py --config configs/config_hype.json --exchange bybit
```
This automatically fetches min_order_notional from the exchange.

### Common Values
- Most perpetuals: $5-$20
- HYPE on Bybit: ~$11
- Major coins (BTC, ETH): Often higher ($10-$50)

---

## Quick Reference

| Config Parameter | Description | Example Value |
|-----------------|-------------|---------------|
| `total_wallet_exposure_limit` | Max position size as multiple of wallet | 2.0 |
| `n_positions` | Number of concurrent positions | 1 |
| `entry_initial_qty_pct` | Initial entry size as % of wallet exposure | 0.14615 (14.615%) |
| `leverage` | Exchange leverage setting | 10 |
| Min Order Notional | Exchange minimum order value | $11 (HYPE) |

### Calculation Formula Summary

```
required_balance = min_order_notional / ((total_wallet_exposure_limit / n_positions) * entry_initial_qty_pct)

recommended_balance = required_balance * (1 + buffer)
```

---

## Summary

1. **Live trading**: Balance is automatically fetched from your exchange account
2. **Backtesting**: Uses `starting_balance` from config (doesn't affect live trading)
3. **Minimum balance**: Calculated based on config parameters and exchange minimums
4. **Recommended**: Add 10-30% buffer for safety and drawdowns
5. **Position growth**: Initial entry is small; position grows through DCA up to exposure limit

**For config_hype.json with HYPE:**
- Absolute minimum: $37.63 USDT
- Recommended start: $50-$100 USDT
- Comfortable operation: $100-$200 USDT (allows for drawdowns and multiple entry attempts)
