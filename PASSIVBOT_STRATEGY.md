# Passivbot Trading Strategy Reference

This document is a comprehensive reference for the passivbot v7 trading strategy. It covers the core philosophy, entry/exit mechanics, every configurable parameter, risk management, the optimization system, and key formulas with code references.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Strategy Fundamentals](#2-strategy-fundamentals)
3. [EMA System](#3-ema-system)
4. [Entry Logic](#4-entry-logic)
5. [Exit Logic](#5-exit-logic)
6. [Parameter Reference](#6-parameter-reference)
7. [Risk Management](#7-risk-management)
8. [Forager Mode](#8-forager-mode)
9. [Optimization System](#9-optimization-system)
10. [Version History](#10-version-history)
11. [Key Formulas](#11-key-formulas)

---

## 1. Overview

Passivbot is an automated cryptocurrency trading bot designed for perpetual futures markets. It operates as a **contrarian market maker**: it buys when prices fall and sells when prices rise, profiting from mean-reversion behavior rather than attempting to predict trends.

### Core Philosophy

- **Grid DCA (Dollar-Cost Averaging)**: Passivbot builds positions incrementally. When price moves against a position, additional entries are placed at progressively lower (long) or higher (short) prices, reducing the average entry price.
- **Not a trend follower**: The bot assumes prices will eventually revert toward the mean. It does not try to catch breakouts or ride momentum.
- **Passive income focus**: Designed for continuous, hands-off operation with configurable risk limits.
- **Multi-coin diversification**: Can trade many coins simultaneously, spreading risk across uncorrelated price movements.

### Supported Exchanges

Binance, Bybit, Bitget, OKX, GateIO, Hyperliquid.

### Architecture

- **Rust backtester** (`passivbot-rust/src/`): High-performance simulation engine for backtesting and optimization.
- **Python live bot** (`src/passivbot.py`): Manages live exchange connections, order placement, and position tracking.
- **Python optimizer** (`src/optimize.py`): NSGA-II genetic algorithm wrapper around the Rust backtester.

---

## 2. Strategy Fundamentals

### Martingale-Inspired Grid DCA

Passivbot uses a strategy inspired by martingale systems, but with configurable limits to prevent unbounded risk:

1. **Initial entry**: A small position is opened near the EMA bands.
2. **Grid re-entries**: If price moves against the position, additional orders are placed at widening intervals (DCA), increasing position size and improving the average entry price.
3. **Take profit**: When price reverts favorably, the position is closed in profit through a grid of take-profit orders or trailing close logic.
4. **Exposure cap**: Unlike pure martingale, position growth is capped by `wallet_exposure_limit`, preventing infinite doubling.

### Hybrid Grid + Trailing System

Passivbot supports two complementary order mechanisms:

- **Grid orders**: Pre-calculated limit orders at specific price levels. Deterministic and always present on the order book.
- **Trailing orders**: Dynamic orders that track price movement and trigger on retracement. React to real-time conditions.

The `trailing_grid_ratio` parameter controls the blend between these two systems for both entries and closes.

### Position Lifecycle

```
No Position
    |
    v
Initial Entry (EMA-based price, small qty)
    |
    v
Grid/Trailing Re-entries (position grows as price moves against)
    |
    v
Position reaches wallet_exposure_limit (fully loaded, "stuck" if no bounce)
    |
    v
Grid/Trailing Close (take profit on favorable price movement)
    |
    v
Position fully closed -> cycle repeats
```

If a position becomes stuck (at max exposure with no profitable exit), the **unstuck mechanism** can gradually close it at a loss using profits from other positions.

---

## 3. EMA System

Passivbot uses Exponential Moving Averages (EMAs) as the foundation for determining entry prices, unstuck close prices, and as a general measure of the "fair value" zone.

### Three EMAs

Two EMA spans are configured directly:
- `ema_span_0` (in minutes)
- `ema_span_1` (in minutes)

A third span is derived as the geometric mean:
- `ema_span_2 = sqrt(ema_span_0 * ema_span_1)`

### EMA Calculation

The standard EMA formula is used:

```
alpha = 2 / (span + 1)
next_EMA = prev_EMA * (1 - alpha) + new_value * alpha
```

Passivbot uses a bias-corrected variant internally (adjusted EMA) to handle warm-up periods correctly. See `update_adjusted_ema` in `passivbot-rust/src/backtest.rs:127`.

### EMA Bands

The three EMAs form upper and lower bands:

```
ema_band_upper = max(ema_0, ema_1, ema_2)
ema_band_lower = min(ema_0, ema_1, ema_2)
```

These bands are computed separately for long and short sides (each side has its own `ema_span_0`/`ema_span_1`). See `EMAs::compute_bands` in `passivbot-rust/src/backtest.rs:94`.

### Usage

| Purpose | Long | Short |
|---------|------|-------|
| Initial entry price | `ema_band_lower * (1 - entry_initial_ema_dist)` | `ema_band_upper * (1 + entry_initial_ema_dist)` |
| Unstuck close price | `ema_band_upper * (1 + unstuck_ema_dist)` | `ema_band_lower * (1 - unstuck_ema_dist)` |

Note: `entry_initial_ema_dist` is typically negative, placing the initial entry *below* the lower EMA band for longs (and *above* the upper band for shorts).

---

## 4. Entry Logic

Entry orders are managed by `calc_next_entry_long` / `calc_next_entry_short` in `passivbot-rust/src/entries.rs:342` / `entries.rs:898`.

### 4.1 Initial Entry

When no position exists (or position is smaller than 80% of the initial entry qty):

**Price:**
```
long:  min(order_book_bid, round_dn(ema_band_lower * (1 - entry_initial_ema_dist), price_step))
short: max(order_book_ask, round_up(ema_band_upper * (1 + entry_initial_ema_dist), price_step))
```

**Quantity:**
```
initial_entry_qty = max(
    min_entry_qty,
    round(balance * wallet_exposure_limit * entry_initial_qty_pct / entry_price, qty_step)
)
```

See `calc_initial_entry_qty` in `entries.rs:9` and `calc_ema_price_bid`/`calc_ema_price_ask` in `utils.rs:235`/`utils.rs:247`.

### 4.2 Grid Re-entries

Once a position exists, grid re-entry prices are calculated using dynamic spacing:

**Spacing multiplier:**
```
we_multiplier = (wallet_exposure / wallet_exposure_limit) * entry_grid_spacing_we_weight
log_multiplier = grid_log_range * entry_grid_spacing_log_weight
spacing_multiplier = max(0, 1 + we_multiplier + log_multiplier)
```

**Re-entry price:**
```
long:  min(round_dn(pos_price * (1 - entry_grid_spacing_pct * spacing_multiplier), price_step), bid)
short: max(round_up(pos_price * (1 + entry_grid_spacing_pct * spacing_multiplier), price_step), ask)
```

**Re-entry quantity:**
```
reentry_qty = max(
    min_entry_qty,
    round(max(
        position_size * entry_grid_double_down_factor,
        balance * wallet_exposure_limit * entry_initial_qty_pct / reentry_price
    ), qty_step)
)
```

The re-entry qty is always at least as large as the initial entry qty. See `calc_reentry_price_bid` at `entries.rs:112` and `calc_reentry_qty` at `entries.rs:87`.

**Cropping and inflation**: If a re-entry would exceed the wallet exposure limit, it is cropped. If the *next* re-entry after the current one would be too small (< 25% of the double-down factor), the current re-entry is inflated to fill the remaining exposure budget. See `calc_grid_entry_long` at `entries.rs:180`.

### 4.3 Trailing Entries

Trailing entries use a two-condition trigger system based on price tracking since the last position change:

**Long trailing entry conditions:**

| Scenario | Threshold condition | Retracement condition | Entry price |
|----------|--------------------|-----------------------|-------------|
| `threshold <= 0` | Always met | `max_since_min > min_since_open * (1 + retracement_pct)` | `bid` (market) |
| `threshold > 0, retracement <= 0` | Always placed as limit | N/A | `min(bid, pos_price * (1 - threshold_pct))` |
| `threshold > 0, retracement > 0` | `min_since_open < pos_price * (1 - threshold_pct)` | `max_since_min > min_since_open * (1 + retracement_pct)` | `min(bid, pos_price * (1 - threshold + retracement))` |

The trailing price tracker resets on every position change (entry or partial close). Tracking is based on 1-minute OHLCV candles. See `calc_trailing_entry_long` at `entries.rs:442`.

The trailing entry uses `entry_trailing_double_down_factor` instead of `entry_grid_double_down_factor` for sizing.

### 4.4 Grid/Trailing Blending (`entry_trailing_grid_ratio`)

The `entry_trailing_grid_ratio` controls how the position is built:

| Value | Behavior |
|-------|----------|
| `0.0` | Grid orders only |
| `1.0` or `-1.0` | Trailing orders only |
| `> 0` (e.g. `0.3`) | Trailing first until 30% of exposure filled, then grid for the rest |
| `< 0` (e.g. `-0.9`) | Grid first until (1 - 0.9) = 10% of exposure filled, then trailing for the rest |

See `calc_next_entry_long` at `entries.rs:342` for the branching logic.

---

## 5. Exit Logic

Close orders are managed by `calc_next_close_long` / `calc_next_close_short` in `passivbot-rust/src/closes.rs:220` / `closes.rs:534`.

### 5.1 Auto-Reduce (Enforce Exposure Limit)

Before any grid/trailing close logic, if `enforce_exposure_limit = true` and the position's wallet exposure exceeds the limit by more than 1%, the bot places a market-price close order to reduce the position back to within limits. This protects against balance withdrawals or config changes. See `closes.rs:242`.

### 5.2 Grid Close (Take Profit)

Take-profit prices are linearly spaced between `markup_start` and `markup_end`:

**Long close prices:**
```
close_price_start = round_up(pos_price * (1 + close_grid_markup_start), price_step)
close_price_end   = round_up(pos_price * (1 + close_grid_markup_end), price_step)
```

**Short close prices:**
```
close_price_start = round_dn(pos_price * (1 - close_grid_markup_start), price_step)
close_price_end   = round_dn(pos_price * (1 - close_grid_markup_end), price_step)
```

**Direction:** If `markup_start > markup_end`, the TP grid is built backwards (higher prices first for longs). If `markup_start < markup_end`, it is built forwards.

**Close quantity per grid level:**
```
close_qty = min(position_size, max(min_entry_qty, round_up(full_psize * close_grid_qty_pct + leftover, qty_step)))
```

Where `full_psize = cost_to_qty(balance * wallet_exposure_limit, pos_price, c_mult)` and `leftover = max(0, position_size - full_psize)`.

**Price selection based on exposure ratio:**
The active close price walks from `markup_start` toward `markup_end` proportionally to `wallet_exposure / wallet_exposure_limit`. If the position exceeds full size, closing begins at the price closest to the position price (minimum markup). See `calc_grid_close_long` at `closes.rs:43`.

### 5.3 Trailing Close

Trailing closes mirror the entry trailing logic but in the profitable direction:

**Long trailing close conditions:**

| Scenario | Threshold condition | Retracement condition | Close price |
|----------|--------------------|-----------------------|-------------|
| `threshold <= 0` | Always met | `min_since_max < max_since_open * (1 - retracement_pct)` | `ask` (market) |
| `threshold > 0, retracement <= 0` | Always placed as limit | N/A | `max(ask, pos_price * (1 + threshold_pct))` |
| `threshold > 0, retracement > 0` | `max_since_open > pos_price * (1 + threshold_pct)` | `min_since_max < max_since_open * (1 - retracement_pct)` | `max(ask, pos_price * (1 + threshold - retracement))` |

See `calc_trailing_close_long` at `closes.rs:121`.

### 5.4 Grid/Trailing Close Blending (`close_trailing_grid_ratio`)

Works identically to entry blending:

| Value | Behavior |
|-------|----------|
| `0.0` | Grid close only |
| `1.0` or `-1.0` | Trailing close only |
| `> 0` (e.g. `0.3`) | Trailing close for first 30% of position, then grid close for the rest |
| `< 0` (e.g. `-0.8`) | Grid close for first 20% of position, then trailing close for the rest |

When blending, the non-active portion of the position is reserved. For example, with `close_trailing_grid_ratio = 0.3`, the grid close only sees 70% of the position size, leaving 30% for trailing close. See `calc_next_close_long` at `closes.rs:220`.

### 5.5 Unstuck Mechanism

The unstuck system acts as a soft stop-loss, gradually closing stuck positions using profits from other positions.

**Activation conditions:**
1. `unstuck_loss_allowance_pct > 0` (feature is enabled)
2. The position is stuck: `wallet_exposure / wallet_exposure_limit > unstuck_threshold`
3. The auto-unstuck allowance is positive (there are accumulated profits to spend)
4. The current price is at or beyond the EMA-based unstuck close price

**Allowance calculation:**
```
balance_peak = balance + (pnl_cumsum_max - pnl_cumsum_running)
drop_since_peak_pct = balance / balance_peak - 1
allowance = max(0, balance_peak * (loss_allowance_pct * total_wallet_exposure_limit + drop_since_peak_pct))
```

See `calc_auto_unstuck_allowance` in `utils.rs:222`.

**Unstuck close price:**
```
long:  round_up(ema_band_upper * (1 + unstuck_ema_dist), price_step)
short: round_dn(ema_band_lower * (1 - unstuck_ema_dist), price_step)
```

**Close quantity:** Based on `unstuck_close_pct * wallet_exposure_limit * balance`, capped by the allowance.

**Priority:** When multiple positions are stuck, the position with the smallest price-action distance (closest to being profitable) is unstucked first. See `calc_unstucking_close` in `backtest.rs:1469`.

### 5.6 Panic Close

In live trading, `forced_mode = "panic"` causes the bot to immediately close the position at market price. This generates `ClosePanicLong` / `ClosePanicShort` order types.

---

## 6. Parameter Reference

### 6.1 Backtest Settings

Located under `config.backtest`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `base_dir` | string | Directory to save backtest results |
| `compress_cache` | bool | `true` saves disk space; `false` enables faster loading |
| `end_date` | string | End date of backtest (e.g. `"2024-06-23"` or `"now"`) |
| `exchanges` | list | Exchanges for OHLCV data: `binance`, `bybit`, `gateio`, `bitget`, `hyperliquid` |
| `start_date` | string | Start date of backtest |
| `starting_balance` | float | Starting balance in USD (backtest only, not used in live) |
| `use_btc_collateral` | bool | Simulate BTC-denominated accounting (buy BTC with profits, go into USD debt on losses) |

### 6.2 Logging

Located under `config.logging`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `level` | int | Verbosity: `0` (warnings), `1` (info), `2` (debug), `3` (trace) |

### 6.3 EMA Parameters

Located under `config.bot.long` / `config.bot.short`.

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `ema_span_0` | float | [200, 1440] | First EMA span in minutes |
| `ema_span_1` | float | [200, 1440] | Second EMA span in minutes |

A third EMA span is derived as `sqrt(ema_span_0 * ema_span_1)`. The three EMAs form `ema_band_upper = max(emas)` and `ema_band_lower = min(emas)`.

### 6.4 Position/Exposure Settings

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `n_positions` | int | [1, 20+] | Maximum concurrent positions. Set to `0` to disable side. |
| `total_wallet_exposure_limit` | float | [0, 10+] | Maximum total exposure as ratio of wallet balance. `1.0` = 100% of balance. |
| `enforce_exposure_limit` | bool | - | If `true`, auto-reduce position at market when exposure exceeds limit by >1% |

Per-position exposure: `wallet_exposure_limit = total_wallet_exposure_limit / n_positions`.

### 6.5 Grid Entry Parameters

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `entry_initial_ema_dist` | float | [-0.1, 0.003] | Offset from EMA band for initial entry. Negative = further from price. |
| `entry_initial_qty_pct` | float | [0.004, 0.1] | Initial entry size as `balance * WEL * qty_pct`. |
| `entry_grid_spacing_pct` | float | [0.001, 0.06] | Base spacing between grid levels as percentage of position price. |
| `entry_grid_spacing_we_weight` | float | [0, 10] | How much wallet exposure widens grid spacing. |
| `entry_grid_spacing_log_weight` | float | [0, 400] | How much volatility (log range) widens grid spacing. `0` = disabled. |
| `entry_grid_spacing_log_span_hours` | float | [672, 2688] | EMA span in hours for smoothing the log-range volatility signal. |
| `entry_grid_double_down_factor` | float | [0.01, 4] | Each grid re-entry qty = `position_size * ddf`. |

### 6.6 Trailing Entry Parameters

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `entry_trailing_grid_ratio` | float | [-1, 1] | Blend between trailing and grid entries. See Section 4.4. |
| `entry_trailing_threshold_pct` | float | [-0.01, 0.1] | Price must move this % from position price to activate trailing. `<= 0` = immediate. |
| `entry_trailing_retracement_pct` | float | [0.0001, 0.1] | Price must retrace this % from extreme to trigger entry. `<= 0` = trigger at threshold. |
| `entry_trailing_double_down_factor` | float | [0.01, 4] | DDF for trailing entries (uses this instead of grid DDF). |

### 6.7 Grid Close Parameters

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `close_grid_markup_start` | float | [0.001, 0.03] | First TP level as markup % from position price. |
| `close_grid_markup_end` | float | [0.001, 0.03] | Last TP level as markup % from position price. |
| `close_grid_qty_pct` | float | [0.05, 1] | Each TP order = `full_pos_size * qty_pct`. Creates `1/qty_pct` orders. |

If `close_grid_qty_pct >= 1.0` or `< 0`, a single TP order is placed at `markup_start`.

### 6.8 Trailing Close Parameters

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `close_trailing_grid_ratio` | float | [-1, 1] | Blend between trailing and grid closes. See Section 5.4. |
| `close_trailing_qty_pct` | float | [0.05, 1] | Close quantity per trailing close = `full_pos_size * qty_pct`. |
| `close_trailing_threshold_pct` | float | [-0.01, 0.1] | Price must reach this profit % to activate. `<= 0` = immediate. |
| `close_trailing_retracement_pct` | float | [0.0001, 0.1] | Price must retrace this % from profit peak to trigger close. |

### 6.9 Unstuck Parameters

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `unstuck_threshold` | float | [0.4, 0.95] | Position is "stuck" when `WE / WEL > threshold`. |
| `unstuck_close_pct` | float | [0.001, 0.1] | Qty per unstuck order = `full_pos_size * WEL * close_pct`. |
| `unstuck_ema_dist` | float | [-0.1, 0.01] | Distance from EMA band for unstuck close price. |
| `unstuck_loss_allowance_pct` | float | [0.001, 0.05] | Max loss below peak balance as `peak * (pct * TWEL)`. |

### 6.10 Filter / Forager Parameters

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `filter_volume_drop_pct` | float | [0.5, 1] | Drop this % of lowest-volume coins. `0` = allow all. |
| `filter_volume_ema_span` | float | [360, 2880] | EMA span (minutes) for smoothing volume ranking. |
| `filter_log_range_ema_span` | float | [10, 360] | EMA span (minutes) for smoothing log-range volatility ranking. |

### 6.11 Live Trading Settings

Located under `config.live`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `approved_coins` | list/path | `[]` | Coins approved for trading. Can be a file path or inline list. Split by `long`/`short`. |
| `auto_gs` | bool | `true` | Auto graceful-stop for disapproved coins. If `false`, manual mode instead. |
| `empty_means_all_approved` | bool | `true` | If `true`, empty `approved_coins` means all coins approved. |
| `execution_delay_seconds` | float | `2` | Wait time between exchange API calls. |
| `filter_by_min_effective_cost` | bool | `true` | Disallow coins where initial order would be below exchange minimum. |
| `forced_mode_long` / `forced_mode_short` | string | `""` | Force all coins to a mode: `n` (normal), `m` (manual), `gs` (graceful_stop), `t` (tp_only), `p` (panic). |
| `ignored_coins` | list/path | `[]` | Coins to never trade. Can be split by `long`/`short`. |
| `leverage` | int | `10` | Exchange leverage. Must be >= `TWEL_long + TWEL_short`. |
| `market_orders_allowed` | bool | `true` | Allow market orders when limit price is near market price. |
| `max_n_cancellations_per_batch` | int | `5` | Max order cancellations per execution cycle. |
| `max_n_creations_per_batch` | int | `3` | Max new orders per execution cycle. |
| `max_n_restarts_per_day` | int | `10` | Auto-restart limit on crashes. |
| `minimum_coin_age_days` | int | `180` | Ignore coins listed less than N days. |
| `pnls_max_lookback_days` | int | `30` | PnL history fetch window. |
| `price_distance_threshold` | float | `0.002` | Min distance to market for EMA-based limit orders. |
| `time_in_force` | string | `"good_till_cancelled"` | Order TIF policy. |
| `user` | string | - | API key identifier from `api-keys.json`. |
| `max_memory_candles_per_symbol` | int | `200000` | Max 1m candles retained in RAM per symbol. |
| `max_disk_candles_per_symbol_per_tf` | int | `2000000` | Max candles persisted on disk per symbol/timeframe. |
| `memory_snapshot_interval_minutes` | int | `30` | Interval between memory telemetry snapshots. |
| `max_warmup_minutes` | int | `0` | Hard ceiling on warmup window. `0` = uncapped. |
| `warmup_ratio` | float | `0.2` | Multiplier on longest indicator span for warmup duration. |

### 6.12 Coin Overrides

Located under `config.coin_overrides`.

Allows per-coin configuration overrides. Format: `{"COIN": {overrides}}`.

**Eligible override parameters:**

Bot parameters (per long/short):
`close_grid_markup_end`, `close_grid_markup_start`, `close_grid_qty_pct`, `close_trailing_grid_ratio`, `close_trailing_qty_pct`, `close_trailing_retracement_pct`, `close_trailing_threshold_pct`, `ema_span_0`, `ema_span_1`, `enforce_exposure_limit`, `entry_grid_double_down_factor`, `entry_grid_spacing_pct`, `entry_grid_spacing_we_weight`, `entry_grid_spacing_log_weight`, `entry_grid_spacing_log_span_hours`, `entry_initial_ema_dist`, `entry_initial_qty_pct`, `entry_trailing_double_down_factor`, `entry_trailing_grid_ratio`, `entry_trailing_retracement_pct`, `entry_trailing_threshold_pct`, `unstuck_close_pct`, `unstuck_ema_dist`, `unstuck_threshold`, `wallet_exposure_limit`

Live parameters:
`forced_mode_long`, `forced_mode_short`, `leverage`

Overrides can reference an external config file via `override_config_path`. Specific parameter overrides take precedence over file-loaded overrides.

### 6.13 Forced Modes

| Mode | Code | Behavior |
|------|------|----------|
| Normal | `n` | Full bot management |
| Manual | `m` | Bot ignores the position entirely |
| Graceful Stop | `gs` | Manages existing position but opens no new ones |
| Take Profit Only | `t` | Only manages closing orders |
| Panic | `p` | Immediately closes position at market price |

### 6.14 Optimization Settings

Located under `config.optimize`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `bounds` | object | - | Min/max ranges for each optimizable parameter. Format: `{param_name: [min, max]}`. |
| `iters` | int | `300000` | Number of backtests per optimization run. |
| `n_cpus` | int | `2` | Parallel CPU cores. |
| `population_size` | int | `300` | GA population size. |
| `scoring` | list | `["mdg", "sharpe_ratio"]` | Objective metrics for multi-objective optimization. |
| `limits` | object | `{}` | Penalty thresholds. Format: `{penalize_if_greater_than_X: val}`. |
| `crossover_probability` | float | `0.64` | Probability of crossover between two individuals. |
| `crossover_eta` | float | `20.0` | SBX crowding factor. Lower = more exploration. |
| `mutation_probability` | float | `0.34` | Probability of mutating an individual. |
| `mutation_eta` | float | `20.0` | Polynomial mutation crowding factor. |
| `mutation_indpb` | float | `0` | Per-attribute mutation probability. `0` = auto-scale to `1/n_params`. |
| `offspring_multiplier` | float | `1.0` | Offspring count = `population_size * multiplier`. |
| `compress_results_file` | bool | `true` | Compress output binary log. |
| `enable_overrides` | list | `[]` | Custom optimizer overrides from `optimizer_overrides.py`. |

---

## 7. Risk Management

### 7.1 Wallet Exposure

The primary risk metric. Measures position size relative to unleveraged balance:

```
wallet_exposure = (position_size * position_price) / unleveraged_wallet_balance
```

| WE | Meaning | Bankruptcy distance (long) |
|----|---------|--------------------------|
| 0.0 | No position | N/A |
| 1.0 | 100% of balance | Price goes to $0 (100% drop) |
| 2.0 | 200% of balance | 50% drop |
| 3.0 | 300% of balance | 33% drop |
| 10.0 | 1000% of balance | 10% drop |

### 7.2 Leverage

Passivbot uses **unleveraged wallet balance** for all calculations. Exchange leverage only affects margin requirements. Changing leverage does not change bot behavior.

**Minimum leverage required:** `total_wallet_exposure_limit_long + total_wallet_exposure_limit_short`

Setting leverage too low causes "insufficient margin" errors. Setting it higher than needed provides a safety margin.

### 7.3 Bankruptcy and Liquidation

- **Bankruptcy**: `equity = balance + unrealized_PnL = 0`
- **Liquidation**: Exchange force-closes before bankruptcy to cover slippage

The distance from position price to bankruptcy is `1 / wallet_exposure`:
- WE = 2.0: 50% price drop triggers bankruptcy
- WE = 5.0: 20% price drop triggers bankruptcy

### 7.4 Diversification

Trading multiple coins reduces single-coin risk. Each position gets `total_wallet_exposure_limit / n_positions` as its individual exposure cap.

From `docs/risk_management.md`: "It may be more desirable to end up with 3 out of 10 bots stuck, each with wallet_exposure==0.1, than with 1 single bot stuck with wallet_exposure==1.0."

### 7.5 Dynamic Wallet Exposure Limits

When the number of tradeable coins drops below `n_positions` (e.g. due to delistings or filter changes), the backtester dynamically recalculates per-position WEL:

```
effective_n_positions = min(n_positions, eligible_coins)
dynamic_WEL = total_wallet_exposure_limit / effective_n_positions
```

See `update_n_positions_and_wallet_exposure_limits` in `backtest.rs:542`.

### 7.6 Minimum Balance

```
required_balance = min_order_notional / (wallet_exposure_per_position * entry_initial_qty_pct)
```

Example: With WEL=2.0, n_positions=1, initial_qty_pct=0.15, and min_order=$11:
`$11 / (2.0 * 0.15) = $36.67` minimum balance.

---

## 8. Forager Mode

When `n_positions` is set and `approved_coins` allows multiple coins, passivbot operates in "forager mode" -- dynamically selecting which coins to trade based on market conditions.

### Selection Process

1. **Volume filter**: Compute EMA of quote volume per coin. Drop the bottom `filter_volume_drop_pct` by volume. Uses `filter_volume_ema_span` for smoothing.

2. **Log-range ranking**: Among remaining coins, rank by EMA of log-range volatility `ln(high/low)`. Uses `filter_log_range_ema_span` for smoothing. Select the most volatile coins up to `n_positions`.

3. **Active set**: Currently held positions are always included in the active set (never dropped mid-trade). Remaining slots are filled from the ranked candidates.

See `calc_preferred_coins`, `filter_by_relative_volume`, and `rank_by_log_range` in `backtest.rs:645`.

### EMA-Based Indicators

Volume and log-range are tracked as EMAs (not rolling windows), providing smoother signals:

```
vol_alpha = 2 / (filter_volume_ema_span + 1)
log_range_alpha = 2 / (filter_log_range_ema_span + 1)
```

The grid-spacing log range uses a separate hourly EMA with span `entry_grid_spacing_log_span_hours`.

---

## 9. Optimization System

### 9.1 Algorithm: NSGA-II

Passivbot uses the **Non-dominated Sorting Genetic Algorithm II** (NSGA-II) for multi-objective optimization. This finds the Pareto front of non-dominated solutions across multiple scoring metrics.

### 9.2 Scoring Metrics

The full list of available scoring metrics:

**Returns & Growth:**
| Metric | Description |
|--------|-------------|
| `adg`, `adg_w` | Average Daily Gain (smoothed geometric) and recency-biased variant |
| `mdg`, `mdg_w` | Median Daily Gain and recency-biased variant |
| `gain` | Final balance gain (end/start ratio) |

**Risk:**
| Metric | Description |
|--------|-------------|
| `drawdown_worst` | Maximum peak-to-trough drawdown |
| `drawdown_worst_mean_1pct` | Mean of worst 1% daily drawdowns |
| `expected_shortfall_1pct` | Mean of worst 1% daily losses (CVaR) |
| `equity_balance_diff_neg_max` | Largest negative divergence between equity and balance |
| `equity_balance_diff_neg_mean` | Average negative equity-balance divergence |
| `equity_balance_diff_pos_max` | Largest positive equity-balance divergence |
| `equity_balance_diff_pos_mean` | Average positive equity-balance divergence |

**Ratios & Efficiency:**
| Metric | Description |
|--------|-------------|
| `sharpe_ratio`, `sharpe_ratio_w` | Return / volatility |
| `sortino_ratio`, `sortino_ratio_w` | Return / downside volatility |
| `calmar_ratio`, `calmar_ratio_w` | Return / max drawdown |
| `sterling_ratio`, `sterling_ratio_w` | Return / mean worst 1% drawdowns |
| `omega_ratio`, `omega_ratio_w` | Sum of gains / sum of losses |
| `loss_profit_ratio`, `loss_profit_ratio_w` | Total losses / total profits |

**Position & Execution:**
| Metric | Description |
|--------|-------------|
| `positions_held_per_day` | Average positions opened per day |
| `position_held_hours_mean` | Mean holding time in hours |
| `position_held_hours_median` | Median holding time in hours |
| `position_held_hours_max` | Longest single position duration |
| `position_unchanged_hours_max` | Longest span without modifying a position |
| `volume_pct_per_day_avg`, `volume_pct_per_day_avg_w` | Daily traded volume as % of balance |
| `flat_btc_balance_hours` | Hours with flat BTC balance (BTC collateral mode) |

**Equity Curve Quality:**
| Metric | Description |
|--------|-------------|
| `equity_choppiness`, `equity_choppiness_w` | Normalized total variation (lower = smoother) |
| `equity_jerkiness`, `equity_jerkiness_w` | Normalized mean absolute second derivative |
| `exponential_fit_error`, `exponential_fit_error_w` | MSE from log-linear equity fit |

**Suffix `_w`:** Mean across 10 overlapping temporal subsets (full, last 1/2, last 1/3, ..., last 1/10), biasing toward recent performance. See `analyze_backtest` in `analysis.rs:367`.

**Prefix `btc_`:** BTC-denominated variants when `use_btc_collateral = true`.

### 9.3 Scoring Weights & Selection

The optimizer assigns direction weights to each metric:
- **Negative weight** = maximize (value is negated before minimization)
- **Positive weight** = minimize

The optimal configuration is selected from the Pareto front using the lowest **Euclidean distance to the ideal point** (best value seen for each objective).

### 9.4 Limits (Penalties)

Limits do not disqualify configurations but add penalties to fitness scores:

```json
"limits": {
    "penalize_if_greater_than_drawdown_worst": 0.3,
    "penalize_if_lower_than_adg": 0.001
}
```

Penalty grows with the severity of violation.

### 9.5 Genetic Algorithm Parameters

| Parameter | Effect |
|-----------|--------|
| `population_size` | Larger = more diversity, slower convergence |
| `crossover_probability` | How often parents exchange parameters |
| `crossover_eta` | SBX distribution index. Low (<20) = wider exploration |
| `mutation_probability` | How often random changes occur |
| `mutation_eta` | Polynomial mutation index. Low (<20) = larger mutations |
| `mutation_indpb` | Per-gene mutation rate. `0` = auto (`1/n_params`) |
| `offspring_multiplier` | Children per generation = `pop_size * multiplier` |

### 9.6 Fine-Tuning

Lock all but a few parameters using `--fine_tune_params`:

```bash
python3 src/optimize.py configs/template.json \
    --fine_tune_params long_entry_grid_spacing_pct,long_entry_initial_qty_pct
```

Unlisted parameters are locked to their current values by setting their bounds to `[value, value]`.

### 9.7 Output Structure

```
optimize_results/YYYY-MM-DDTHH_MM_SS_{exchanges}_{n_days}days_{coin_label}_{hash}/
    all_results.bin        # Binary log of all evaluated configs (msgpack)
    pareto/                # Pareto-optimal configurations
        {distance}_{hash}.json
    index.json             # List of Pareto member hashes
```

---

## 10. Version History

### v5 (Legacy)

Introduced three distinct strategy modes:
- **Recursive Grid**: Grid entries where each level is calculated recursively from the previous one.
- **Static Grid**: Fixed grid levels pre-calculated from initial entry.
- **Neat Grid**: A cleaner variant with simplified spacing logic.

Each mode had separate parameter sets and behaviors.

### v6

Evolved from v5 with:
- Unified parameter structure across modes.
- Introduction of trailing entry/close mechanisms alongside grids.
- EMA-based price anchoring for initial entries.
- Auto-unstuck mechanism using cross-position profit reallocation.

### v7 (Current)

Major improvements:
- **Unified strategy**: Single set of parameters controls both grid and trailing behavior via `trailing_grid_ratio` blending.
- **High-performance Rust backtester**: Orders of magnitude faster than Python backtesting.
- **Dynamic grid spacing**: `entry_grid_spacing_we_weight` and `entry_grid_spacing_log_weight` add exposure-aware and volatility-aware spacing.
- **Log-range volatility signal**: Hourly EMA of `ln(high/low)` dynamically adjusts grid spacing.
- **EMA-based volume/volatility filters** replacing rolling-window calculations.
- **BTC collateral mode**: Simulate holding BTC as base collateral with USD debt on losses.
- **Multi-exchange backtesting**: Combine OHLCV data from multiple exchanges.
- **NSGA-II optimizer**: Multi-objective Pareto optimization with configurable metrics and penalty limits.
- **Hysteresis-based balance rounding**: Prevents order churn from tiny balance fluctuations.
- **Per-coin config overrides**: Fine-tune parameters for specific coins.
- **Dynamic WEL adjustment**: Automatically redistributes exposure when available coins change.

---

## 11. Key Formulas

### EMA Update

```
alpha = 2 / (span + 1)
EMA_new = EMA_prev * (1 - alpha) + value * alpha
```

Source: `passivbot-rust/src/backtest.rs:127` (bias-corrected variant)

### Wallet Exposure

```
wallet_exposure = (position_size * position_price * c_mult) / balance
```

Source: `passivbot-rust/src/utils.rs:112`

### Initial Entry Quantity

```
qty = max(min_entry_qty, round(balance * WEL * entry_initial_qty_pct / price, qty_step))
```

Source: `passivbot-rust/src/entries.rs:9`

### Grid Re-entry Price (Long)

```
multiplier = 1 + (WE / WEL) * we_weight + grid_log_range * log_weight
price = min(round_dn(pos_price * (1 - spacing_pct * max(0, multiplier)), price_step), bid)
```

Source: `passivbot-rust/src/entries.rs:112`

### Grid Re-entry Quantity

```
qty = max(min_entry_qty, round(max(pos_size * ddf, balance * WEL * initial_qty_pct / price), qty_step))
```

Source: `passivbot-rust/src/entries.rs:87`

### New Position Price (Weighted Average)

```
new_psize = round(old_psize + entry_qty, qty_step)
new_pprice = old_pprice * (old_psize / new_psize) + entry_price * (entry_qty / new_psize)
```

Source: `passivbot-rust/src/utils.rs:140`

### Close Quantity

```
full_psize = balance * WEL / (pos_price * c_mult)
leftover = max(0, position_size - full_psize)
close_qty = min(position_size, max(min_qty, round_up(full_psize * close_qty_pct + leftover, qty_step)))
```

Source: `passivbot-rust/src/closes.rs:7`

### Auto Unstuck Allowance

```
balance_peak = balance + (pnl_cumsum_max - pnl_cumsum_running)
drop_since_peak_pct = balance / balance_peak - 1
allowance = max(0, balance_peak * (loss_allowance_pct * TWEL + drop_since_peak_pct))
```

Source: `passivbot-rust/src/utils.rs:222`

### PnL Calculation

```
pnl_long  = |qty| * c_mult * (close_price - entry_price)
pnl_short = |qty| * c_mult * (entry_price - close_price)
```

Source: `passivbot-rust/src/utils.rs:191`

### ADG (Average Daily Gain)

Daily equities are EMA-smoothed (span=3), then geometric gain is computed:

```
gain = smoothed_end / smoothed_start
adg = gain^(1/n_days) - 1
```

Source: `passivbot-rust/src/analysis.rs:592`

### Equity Choppiness

```
choppiness = sum(|equity[i+1] - equity[i]|) / |equity[last] - equity[0]|
```

Source: `passivbot-rust/src/analysis.rs:522`

### Equity Jerkiness

```
jerkiness = mean(|equity[i+2] - 2*equity[i+1] + equity[i]| / mean(equity[i:i+3]))
```

Source: `passivbot-rust/src/analysis.rs:536`

### Exponential Fit Error

Log-linear least squares regression on daily equity, returning MSE:

```
log_y = ln(equity)
slope, intercept = linear_regression(x, log_y)
error = mean((slope * x + intercept - log_y)^2)
```

Source: `passivbot-rust/src/analysis.rs:556`

---

## Appendix: Order Types

All order types defined in `passivbot-rust/src/types.rs:164`:

| Order Type | ID | Description |
|------------|-----|-------------|
| `EntryInitialNormalLong` | 0 | First long entry, full initial qty |
| `EntryInitialPartialLong` | 1 | Long entry topping up to initial qty |
| `EntryTrailingNormalLong` | 2 | Trailing long re-entry |
| `EntryTrailingCroppedLong` | 3 | Trailing long re-entry, cropped to WEL |
| `EntryGridNormalLong` | 4 | Grid long re-entry |
| `EntryGridCroppedLong` | 5 | Grid long re-entry, cropped to WEL |
| `EntryGridInflatedLong` | 6 | Grid long re-entry, inflated to fill remaining WEL |
| `CloseGridLong` | 7 | Grid take-profit (long) |
| `CloseTrailingLong` | 8 | Trailing take-profit (long) |
| `CloseUnstuckLong` | 9 | Unstuck close (long) |
| `CloseAutoReduceLong` | 10 | Auto-reduce for exposure enforcement (long) |
| `EntryInitialNormalShort` | 11 | First short entry |
| `EntryInitialPartialShort` | 12 | Short entry topping up to initial qty |
| `EntryTrailingNormalShort` | 13 | Trailing short re-entry |
| `EntryTrailingCroppedShort` | 14 | Trailing short re-entry, cropped |
| `EntryGridNormalShort` | 15 | Grid short re-entry |
| `EntryGridCroppedShort` | 16 | Grid short re-entry, cropped |
| `EntryGridInflatedShort` | 17 | Grid short re-entry, inflated |
| `CloseGridShort` | 18 | Grid take-profit (short) |
| `CloseTrailingShort` | 19 | Trailing take-profit (short) |
| `CloseUnstuckShort` | 20 | Unstuck close (short) |
| `CloseAutoReduceShort` | 21 | Auto-reduce for exposure enforcement (short) |
| `ClosePanicLong` | 22 | Panic close (long) |
| `ClosePanicShort` | 23 | Panic close (short) |
