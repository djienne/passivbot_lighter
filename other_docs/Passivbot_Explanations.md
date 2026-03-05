# Passivbot Configuration Explanations

## General Parameters (Long & Short)

### ema_span_0 & ema_span_1
- Time spans in minutes for calculating Exponential Moving Averages
- Formula: `next_EMA = prev_EMA * (1 - alpha) + new_val * alpha`, where `alpha = 2 / (span + 1)`
- A third EMA is calculated as `(ema_span_0 * ema_span_1)^0.5`
- These create upper and lower EMA bands used for initial entries and auto-unstuck closes
- In your config: Long uses 1280.4 & 609.18 min; Short uses 615.45 & 1097.8 min

### n_positions
- Maximum concurrent positions allowed
- Set to 0 to disable long/short trading
- Your config: 5 long positions, 12 short positions

### total_wallet_exposure_limit
- Maximum total exposure as a ratio of wallet balance
- Example: 2.3255 = 232.55% of unleveraged balance
- Each position gets equal share: `wallet_exposure_limit = total_wallet_exposure_limit / n_positions`
- Your config: Long = 2.3255 (high leverage), Short = 0.021017 (very conservative)

### enforce_exposure_limit
- If true, forces position reduction at market price if exposure exceeds limit
- Protects against balance withdrawals or config changes
- Both set to true in your config

---

## Entry Grid Parameters

### entry_initial_qty_pct
- Initial entry size as percentage of balance * `wallet_exposure_limit`
- Your config: Long = 0.026377 (2.64%), Short = 0.017426 (1.74%)

### entry_initial_ema_dist
- Distance offset from EMA bands for initial entry price
- Long: entry at `lower_EMA_band * (1 + entry_initial_ema_dist)`
- Short: entry at `upper_EMA_band * (1 - entry_initial_ema_dist)`
- Your config: Long = -0.04566 (4.57% below EMA), Short = -0.098717 (9.87% below EMA)

### entry_grid_spacing_pct
- Base percentage spacing between grid re-entry orders
- Formula: `next_price = pos_price * (1 ± entry_grid_spacing_pct * multiplier)`
- Your config: Long = 0.085074 (8.5%), Short = 0.11689 (11.7%)

### entry_grid_spacing_we_weight
- Controls how spacing changes as wallet exposure approaches limit
- Positive values → wider spacing when near limit
- Negative values → tighter spacing when exposure is small
- Your config: Long = 0.0021584, Short = 0.8249 (very strong effect)

### entry_grid_spacing_log_weight & entry_grid_spacing_log_span_hours
- Adjusts grid spacing based on market volatility (log range)
- `log_weight` = strength of adjustment; 0 disables it
- `log_span_hours` = EMA smoothing window for volatility signal
- Your config: Both use 72-hour span with 0.0 weight (disabled)

### entry_grid_double_down_factor
- Size multiplier for each subsequent grid entry
- Next entry = `current_position_size * double_down_factor`
- Your config: Long = 1.8274, Short = 2.4146 (aggressive pyramiding)

### entry_trailing_grid_ratio
- Allocates between trailing and grid orders
- `> 0`: trailing first, then grid
- `< 0`: grid first, then trailing
- `0`: grid only; `±1`: trailing only
- Your config: Long = 0.056009 (5.6% trailing, 94.4% grid), Short = 0.094742 (9.5% trailing)

### entry_trailing_threshold_pct & entry_trailing_retracement_pct
- Threshold: Price must move this % from position price to activate tracking
- Retracement: Price must retrace this % from peak/trough to trigger order
- Tracked using 1-minute OHLCV candles
- Your config varies significantly between long/short

### entry_trailing_double_down_factor
- Similar to grid double down, but for trailing entries
- Your config: Long = 1.5071, Short = 0.36779

---

## Close Grid Parameters

### close_grid_markup_start & close_grid_markup_end
- Take-profit price range as % markup from position price
- Direction depends on relationship:
  - `start > end`: Backwards grid (higher prices first for longs)
  - `start < end`: Forwards grid (lower prices first for longs)
- Your config:
  - Long: `start=0.019475`, `end=0.019066` (backwards, 1.9% profit)
  - Short: `start=0.001849`, `end=0.029152` (forwards, 0.18%-2.92% profit range)

### close_grid_qty_pct
- Position size percentage per take-profit order
- Creates `1 / close_grid_qty_pct` orders
- Your config: Long = 0.88344 (88% per order), Short = 0.91395 (91% per order)

### close_trailing_grid_ratio
- Same as entry trailing, but for closing
- Your config: Long = 0.76575 (76% trailing), Short = 0.24271 (24% trailing)

### close_trailing_qty_pct
- Position size for each trailing close order
- Your config: Long = 0.73205 (73%), Short = 0.85509 (85.5%)

### close_trailing_threshold_pct & close_trailing_retracement_pct
- Same logic as entry trailing, but for take-profits
- Waits for price to reach threshold, then retracement to trigger
- Your config: Short has negative threshold (-0.079601), always active

---

## Unstuck Mechanism

When position exceeds `unstuck_threshold`, bot uses profits from other positions to close the stuck position.

### unstuck_threshold
- Trigger point as ratio of `wallet_exposure / wallet_exposure_limit`
- Your config: Long = 0.42148 (42%), Short = 0.88485 (88%)

### unstuck_close_pct
- Percentage of max position size to close per unstuck order
- Your config: Long = 0.045821 (4.58%), Short = 0.056151 (5.62%)

### unstuck_ema_dist
- Distance from EMA band for unstuck close price
- Long: `upper_EMA * (1 + unstuck_ema_dist)`
- Short: `lower_EMA * (1 - unstuck_ema_dist)`
- Your config: Long = 0.0019503, Short = -0.017458

### unstuck_loss_allowance_pct
- Maximum loss below peak balance before stopping unstucking
- Formula: `loss_allowance = peak * (1 - unstuck_loss_allowance_pct * total_wallet_exposure_limit)`
- Your config: Long = 0.0047865, Short = 0.025208

---

## Filter Parameters

### filter_volume_drop_pct
- Drops lowest-volume coins by this percentage
- 0.90675 = drop bottom 90.7% of coins (very aggressive filtering)
- Your config: Long = 0.90675, Short = 0.84903

### filter_volume_ema_span
- EMA span (in minutes) for smoothing volume calculations
- Your config: Long = 303.6 min, Short = 320.18 min

### filter_log_range_ema_span
- EMA span for smoothing log-range volatility: `ln(high/low)`
- Higher values favor more volatile coins
- Your config: Long = 43.162 min, Short = 66.351 min

## Key Observations for Your Config

1.  **Asymmetric strategy:** Long is highly aggressive (232% exposure, 5 positions), Short is ultra-conservative (2% exposure, 12 positions)
2.  **Aggressive pyramiding:** High double-down factors mean positions grow quickly
3.  **Volume filtering:** Drops 84-90% of coins, focusing only on highest volume pairs
4.  **HYPE focus:** Only approved for long trading of HYPEUSDT

This appears to be an optimized config for trading HYPE token with heavy long bias and minimal short exposure.