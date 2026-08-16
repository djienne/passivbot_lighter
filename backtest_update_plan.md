# Backtest-Only Upstream Parity Plan

## Goal

Bring the local backtest path into parity with
`ref/passivbot-7.11.0/passivbot-7.11.0` for the Rust backtest engine and the
Python backtest wrapper that consumes it.

Scope is intentionally limited to backtesting:

- Rust modules used by `passivbot_rust.run_backtest*`.
- Python code in the local backtest execution path.
- Tests and scripts that validate backtest behavior.

Out of scope:

- Live exchange execution.
- Lighter exchange adapter behavior.
- UI or dashboard work.
- Broad optimizer rewrites, except where optimizer calls the shared backtest
  wrapper and must continue to work.
- Vendored/reference files under `ref/`.

## Current Parity State

Runtime-equivalent or effectively equivalent:

- `passivbot-rust/src/risk.rs`
- `passivbot-rust/src/closes.rs`
- `passivbot-rust/src/trailing.rs`
- `passivbot-rust/src/analysis.rs`, except local-only dead-code attributes

Known non-parity areas:

- `backtest.rs`
- `orchestrator.rs`
- `python.rs`
- `types.rs`
- `entries.rs`
- `coin_selection.rs`
- `equity_hard_stop_loss.rs`
- Python `src/backtest.py` handling of result shape, fills, equities, analysis,
  and hard-stop plot payload

## Parity Requirements

### 1. Rust Backtest Engine Surface

Target:

- Match upstream `passivbot-rust/src` behavior for the backtest modules.
- Preserve local buildability and Python importability.
- Avoid editing the upstream reference folder.

Concrete requirements:

- `run_backtest` and `run_backtest_bundle` must accept the same backtest inputs
  as upstream.
- The Rust return value must support upstream's 5-value return shape:
  fills, equities, USD analysis, BTC analysis, hard-stop plot data.
- The Rust engine must return/propagate runtime errors the same way upstream
  does where upstream returns `Result`.
- Backtest fills must include upstream liquidity tagging where upstream exposes
  it.
- Equities output must include upstream strategy-equity column when available.

### 2. Entry Grid Behavior

Target:

- Remove local-only entry-grid inflation behavior and match upstream entry
  sizing.

Concrete requirements:

- `calc_grid_entry_long` and `calc_grid_entry_short` must no longer preview the
  next reentry and inflate the current reentry.
- Local order type names should match upstream behavior for normal/cropped grid
  entries.
- Existing Python callers must still parse returned fills.

### 3. Liquidation And Equity Sampling

Target:

- Match upstream liquidation semantics.

Concrete requirements:

- Default liquidation threshold is upstream-like.
- Backtest stops when USD equity reaches the configured floor.
- Final USD equity is clamped to the liquidation floor.
- BTC equity is clamped consistently with upstream.
- `analysis.liquidated` is set from the engine.
- Strategy-equity samples are recorded in the same order as upstream relative
  to liquidation handling.

### 4. Equity Hard Stop Loss

Target:

- Match upstream HSL behavior as used by the backtest.

Concrete requirements:

- Per-side HSL config fields exist in `BotParams`.
- Legacy/common HSL aliases hydrate the per-side config the same way upstream
  does.
- Runtime HSL state updates, halt, restart, panic close, and metrics follow
  upstream.
- Hard-stop plot data is returned to Python as the fifth backtest result value.
- Disabled HSL must not change ordinary backtests beyond upstream's normal
  defaults.

### 5. BTC Collateral

Target:

- Match upstream BTC collateral initialization.

Concrete requirements:

- BTC collateral initializes at the trade start price, not immediately from the
  first BTC/USD sample.
- BTC-denominated equity and analysis follow upstream behavior.

### 6. Effective Minimum Cost

Target:

- Match upstream effective minimum cost behavior.

Concrete requirements:

- Effective min cost uses both exchange `min_cost` and executable `min_qty` at
  price.
- Diagnostics and skipped order behavior follow upstream.

### 7. Dynamic WEL And Forager Selection

Target:

- Match upstream behavior for backtest multi-symbol selection and exposure
  sizing.

Concrete requirements:

- Side-specific eligible counts are supported.
- Grow-only tradable count behavior matches upstream.
- Forager scoring uses upstream score weights, EMA readiness, hysteresis, and
  incumbent retention.
- Backtest diagnostics remain available where upstream provides them.

### 8. Market/Taker Execution

Target:

- Match upstream market and limit execution modeling.

Concrete requirements:

- Limit fills retain upstream high/low crossing semantics and maker fee.
- Market fills use close price plus configured slippage and taker fee.
- Panic market closes behave like upstream.
- Liquidity tags identify maker/taker fills.

### 9. Analysis And Python Backtest Wrapper

Target:

- Python backtest path consumes upstream-compatible Rust outputs.

Concrete requirements:

- `src/backtest.py::execute_backtest` handles both 4-value and 5-value Rust
  returns during transition, but final engine should use upstream 5-value form.
- Fills dataframe creation tolerates the upstream fill columns.
- Equities processing supports the fourth strategy-equity column.
- Expanded analysis includes upstream liquidation, strategy-equity, hard-stop,
  and per-side metrics.
- Existing local configs continue to run through `src/backtest.py`.

## Implementation Sequence

### Phase 0: Snapshot And Guardrails

1. Confirm branch and dirty tree.
2. Keep `ref/` and this plan untracked.
3. Run a baseline compile/test check if the tree is expected to be buildable.
4. Keep changes limited to backtest engine, backtest wrapper, and tests.

### Phase 1: Replace Rust Engine Modules With Upstream Parity Target

Use upstream as the source of truth for these modules:

- `analysis.rs`
- `backtest.rs`
- `closes.rs`
- `coin_selection.rs`
- `entries.rs`
- `equity_hard_stop_loss.rs`
- `orchestrator.rs`
- `python.rs`
- `risk.rs`
- `trailing.rs`
- `types.rs`
- `utils.rs`
- `constants.rs`
- `lib.rs`, if required for exported HSL/backtest functions

After replacement:

- Run `cargo fmt`.
- Run `cargo test --lib`.
- Fix only local compatibility issues required for the local backtest path.

### Phase 2: Restore Local Backtest Integration

Update only local backtest-facing Python as needed:

- `src/backtest.py`
- `src/config_utils.py`, only for backtest defaults/schema if required
- Backtest tests under `tests/`
- Existing local backtest sweep scripts only if they parse changed analysis
  fields

Do not update live exchange code for parity unless the Rust API would otherwise
fail to compile or import.

### Phase 3: Verify Behavioral Parity With Upstream Source

Compare local Rust files against upstream:

- Runtime diffs should be zero for copied parity files, or every remaining diff
  must be documented as local integration-only.
- Function sets should match for the backtest modules.
- Entry-grid inflation must be gone.
- `Backtest::run` result/error behavior must match upstream.
- Python bridge return shape must match upstream.

### Phase 4: Test Matrix

Required checks:

```powershell
Set-Location "C:\Users\david\Desktop\freqtrade\passivbot_lighter\passivbot-rust"
cargo fmt --check
cargo test
```

```powershell
Set-Location "C:\Users\david\Desktop\freqtrade\passivbot_lighter"
$env:PYTHONPATH = "C:\Users\david\Desktop\freqtrade\passivbot_lighter\src"
& "C:\Users\david\miniconda3\envs\passivbot\python.exe" -m pytest `
  tests\test_backtest_analysis.py `
  tests\test_backtest_maker_fee_override.py `
  tests\test_rust_utils.py
```

Backtest behavior checks:

- HYPE TOP TWEL 2.0 must report liquidation.
- Sweep with unstuck on/off and TWEL 1.0 to 2.0 should still run.
- Gain and liquidation columns should be present in summary output.

### Phase 5: Final Audit

Before completion:

1. `git diff --no-index` local Rust backtest modules against upstream.
2. Classify all remaining diffs:
   - exact parity
   - local integration-only
   - known non-parity requiring more work
3. Run the test matrix.
4. Confirm no `ref/` files are staged.
5. Commit only code/test changes if the user wants a commit.

## Completion Criteria

The goal is complete only when:

- Backtest Rust engine behavior is upstream-parity or every remaining diff is
  proven integration-only.
- Local Python backtest execution works with the upstream-compatible engine.
- HYPE liquidation behavior is handled by the engine, not by the sweep script.
- Verification commands pass or any pre-existing unrelated failure is clearly
  identified.
- No reference/vendor folder code is committed.
