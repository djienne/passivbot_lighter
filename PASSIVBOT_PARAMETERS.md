# Passivbot v7 — Trading Strategy Parameters Reference

All parameters live under `config.bot.long` / `config.bot.short` (each side is configured independently). Per-coin overrides are possible via `config.coin_overrides`.

| # | Category | Parameter | Type | Range | Description |
|:-:|:---------|:----------|:----:|:-----:|:------------|
| 1 | EMA | `ema_span_0` | float | 200 – 1440 | First EMA span in minutes. Forms one of three EMAs used to compute upper/lower bands that anchor entry and unstuck prices. |
| 2 | EMA | `ema_span_1` | float | 200 – 1440 | Second EMA span in minutes. A third is auto-derived as `sqrt(ema_span_0 × ema_span_1)`. Together they define `ema_band_upper = max(emas)` and `ema_band_lower = min(emas)`. |
| 3 | Position | `n_positions` | int | 1 – 20+ | Maximum concurrent positions per side. `0` disables the side. Each position gets `total_wallet_exposure_limit / n_positions` as its individual cap. |
| 4 | Position | `total_wallet_exposure_limit` | float | 0 – 10+ | Maximum total exposure as a ratio of unleveraged wallet balance (`2.0` = 200%). Bankruptcy distance ≈ `1 / TWEL`. |
| 5 | Position | `enforce_exposure_limit` | bool | — | If `true`, auto-places a market close when wallet exposure exceeds its limit by >1%. Protects against balance withdrawals or config changes. |
| 6 | Grid Entry | `entry_initial_ema_dist` | float | −0.1 – 0.003 | Distance offset from EMA band for initial entry price. Typically negative: places entry below the lower band (long) or above upper band (short). More negative = more conservative. |
| 7 | Grid Entry | `entry_initial_qty_pct` | float | 0.004 – 0.1 | Initial entry size as a fraction of `balance × wallet_exposure_limit` (e.g. `0.15` = 15% of max position value). |
| 8 | Grid Entry | `entry_grid_spacing_pct` | float | 0.001 – 0.06 | Base % spacing between consecutive grid re-entry levels, measured from position average price. Dynamically widened by exposure and volatility weights. |
| 9 | Grid Entry | `entry_grid_spacing_we_weight` | float | 0 – 10 | How much current wallet exposure widens grid spacing. Higher = wider spacing as position grows, preventing aggressive DCA when heavily loaded. |
| 10 | Grid Entry | `entry_grid_spacing_log_weight` | float | 0 – 400 | How much market volatility (hourly log-range) widens grid spacing. `0` = disabled. Higher values adapt the grid to volatile conditions. |
| 11 | Grid Entry | `entry_grid_spacing_log_span_hours` | float | 672 – 2688 | EMA span in hours (≈ 28–112 days) for smoothing the log-range volatility signal. Longer = smoother, less reactive to short-term spikes. |
| 12 | Grid Entry | `entry_grid_double_down_factor` | float | 0.01 – 4 | Each grid re-entry qty = `position_size × ddf`. `0.5` = half-size adds, `1.0` = same size, `2.0` = double (aggressive martingale). |
| 13 | Trailing Entry | `entry_trailing_grid_ratio` | float | −1 – 1 | Blend between trailing and grid entries. `0` = grid only, `±1` = trailing only, `0.3` = trailing first until 30% filled then grid, `−0.9` = grid first until 10% filled then trailing. |
| 14 | Trailing Entry | `entry_trailing_threshold_pct` | float | −0.01 – 0.1 | Price must move this % from position price to activate trailing tracking. `<= 0` = always tracking immediately. |
| 15 | Trailing Entry | `entry_trailing_retracement_pct` | float | 0.0001 – 0.1 | After price hits its extreme, it must bounce back by this % to trigger the trailing entry (e.g. for longs: price falls then must rise this amount from the low). |
| 16 | Trailing Entry | `entry_trailing_double_down_factor` | float | 0.01 – 4 | Same role as grid DDF but used exclusively for trailing entries, allowing independent sizing control. |
| 17 | Grid Close | `close_grid_markup_start` | float | 0.001 – 0.03 | First take-profit level as a markup % above (long) or below (short) the position's average entry price. |
| 18 | Grid Close | `close_grid_markup_end` | float | 0.001 – 0.03 | Last take-profit level markup %. TP orders are linearly spaced between `start` and `end`. If `start > end`, the grid is built backwards (higher profits closed first). |
| 19 | Grid Close | `close_grid_qty_pct` | float | 0.05 – 1.0 | Fraction of the full position to close at each TP level. Creates roughly `1 / qty_pct` TP orders. `>= 1.0` = single TP order at `markup_start`. |
| 20 | Trailing Close | `close_trailing_grid_ratio` | float | −1 – 1 | Blend between trailing and grid closes. Same logic as entry ratio: `0` = grid only, `±1` = trailing only, positive = trailing first, negative = grid first. |
| 21 | Trailing Close | `close_trailing_qty_pct` | float | 0.05 – 1.0 | Fraction of the full position to close per trailing trigger. Multiple triggers may be needed to fully close. |
| 22 | Trailing Close | `close_trailing_threshold_pct` | float | −0.01 – 0.1 | Profit % above position price required to activate trailing close tracking. `<= 0` = always tracking. E.g. `0.02` = start trailing only after 2% profit. |
| 23 | Trailing Close | `close_trailing_retracement_pct` | float | 0.0001 – 0.1 | After price reaches its profit peak, it must pull back by this % to trigger the close. Lets profits run while protecting against full reversal. |
| 24 | Unstuck | `unstuck_threshold` | float | 0.4 – 0.95 | Position is "stuck" when `wallet_exposure / wallet_exposure_limit > threshold` (e.g. `0.8` = stuck at 80%+ of max exposure with no profitable exit). |
| 25 | Unstuck | `unstuck_close_pct` | float | 0.001 – 0.1 | Quantity to close per unstuck order as a fraction of the full position. Small values = gradual loss-taking over many orders. |
| 26 | Unstuck | `unstuck_ema_dist` | float | −0.1 – 0.01 | Distance from EMA band for the unstuck close price. For longs: `ema_band_upper × (1 + unstuck_ema_dist)`. Near zero = closes around current "fair value." |
| 27 | Unstuck | `unstuck_loss_allowance_pct` | float | 0.001 – 0.05 | Maximum cumulative loss the unstuck system may realize, as a fraction of peak balance scaled by TWEL. Acts as a budget for loss-taking across all stuck positions. |
| 28 | Filter | `filter_volume_drop_pct` | float | 0 – 1.0 | Drop this fraction of lowest-volume coins from trading. `0` = no filter. E.g. `0.3` = exclude bottom 30% by volume. |
| 29 | Filter | `filter_volume_ema_span` | float | 360 – 2880 | EMA span in minutes for smoothing the volume ranking signal. Longer = more stable ranking, shorter = more reactive to recent volume changes. |
| 30 | Filter | `filter_log_range_ema_span` | float | 10 – 360 | EMA span in minutes for smoothing the log-range volatility ranking. After volume filtering, the top `n_positions` most volatile coins are selected. |
