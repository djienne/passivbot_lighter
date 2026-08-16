# How to read the DCA (re-entry) behavior straight from a config

Goal: given any passivbot config JSON, answer "under what condition does the bot
add to an open position, and by how much?" in under a minute, without running
anything.

All parameters below live under `bot.long` (or `bot.short`). The currently
deployed config is always `runs/walkforward/live/active_config.json` (it is a
copy of the last window's `test/test_config.json` from the active WFO run).

---

## Step 1 — Which entry mode is active? Look at `entry_trailing_grid_ratio`

This single parameter decides everything else you need to read:

| `entry_trailing_grid_ratio` | DCA mode | Which params matter |
|---|---|---|
| `0` | **Grid only** | `entry_grid_*` |
| `>= 1` or `<= -1` | **Trailing only** | `entry_trailing_*` |
| `0 < r < 1` | Trailing **first** (until WE/WE_limit reaches `r`), then grid | both |
| `-1 < r < 0` | Grid **first** (until WE/WE_limit reaches `1+r`), then trailing | both |

(Source: `calc_next_entry_long` in `passivbot-rust/src/entries.rs` — same
pattern as the close logic in `closes.rs:418`.)

> Example: the June 2026 live config has `entry_trailing_grid_ratio = -1`
> → trailing-only. Ignore all `entry_grid_*` values; they are dead.

## Step 2a — If trailing: the DCA trigger is "dip below threshold, then bounce"

Both conditions must hold (relative to the position's average price and the
price extremes since the last fill):

1. **Dip**: low since last position change `< pos_price × (1 − threshold_pct)`
2. **Bounce**: price recovers off that low by more than `retracement_pct`

where the two percentages are scaled up by current wallet exposure and 1h
log-range volatility:

```
threshold_pct   = entry_trailing_threshold_pct
                  × (1 + (WE/WE_limit) × entry_trailing_threshold_we_weight
                       + vol_ema       × entry_trailing_threshold_volatility_weight)

retracement_pct = entry_trailing_retracement_pct
                  × (1 + (WE/WE_limit) × entry_trailing_retracement_we_weight
                       + vol_ema       × entry_trailing_retracement_volatility_weight)
```

`vol_ema` is the EMA (span = `entry_volatility_ema_span_hours`) of the 1h
log range. The `*_pct` base values are the "calm market, empty position"
numbers; the weights tell you how much they inflate.

Edge cases (read the base values):
- `threshold_pct <= 0` → no dip required, bounce alone triggers.
- `retracement_pct <= 0` → no bounce required, fills as a limit at the threshold.

Entry price = `min(current bid, pos_price × (1 − threshold + retracement))`.

> Example (live config): base dip ≈ 2.4% (`0.023995`), base bounce ≈ 10%
> (`0.10062`); volatility weight 7.34 on the threshold means the required dip
> widens a lot in volatile regimes.

## Step 2b — If grid: the DCA trigger is "price reaches the next grid level"

Re-entry limit orders sit below the position at spacing:

```
spacing = entry_grid_spacing_pct
          × (1 + (WE/WE_limit) × entry_grid_spacing_we_weight
               + vol_ema       × entry_grid_spacing_volatility_weight)
```

i.e. next re-entry ≈ `pos_price × (1 − spacing)`, with spacing growing as the
position fills and as volatility rises.

## Step 3 — How much does it add? `entry_*_double_down_factor`

Re-entry qty ≈ `position_size × double_down_factor` (use
`entry_trailing_double_down_factor` or `entry_grid_double_down_factor`
matching the active mode), but never less than the initial entry qty
(`entry_initial_qty_pct` × balance × WE_limit / price).

> Example: `entry_trailing_double_down_factor = 1.24` → each DCA roughly
> 2.24×'s the position. Contrast with a grid-style config where
> `entry_grid_double_down_factor = 0.06` adds only ~6% per level.

## Step 4 — When does it stop? Exposure caps

```
WE_limit_effective = total_wallet_exposure_limit × (1 + risk_we_excess_allowance_pct)
```

No new entry once wallet exposure > `0.999 × WE_limit_effective`; quantities
are cropped so a fill never exceeds it (order type shows as `...Cropped...`).

---

## One-glance checklist

1. `entry_trailing_grid_ratio` → grid, trailing, or hybrid?
2. Read the matching trigger params (`threshold`/`retracement` or `spacing`)
   plus their `we_weight` / `volatility_weight` multipliers.
3. `*_double_down_factor` → size of each add.
4. `total_wallet_exposure_limit` (+ `risk_we_excess_allowance_pct`) → hard stop.

Code references: `passivbot-rust/src/entries.rs`
(`calc_trailing_entry_long` ~line 410, `calc_grid_entry_long`,
`wallet_exposure_limit_with_allowance` line 10).
