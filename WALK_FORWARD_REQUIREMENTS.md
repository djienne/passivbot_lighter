# Walk-Forward Optimization — Requirements

This document captures the requirements for the **walk-forward optimization (WFO) /
cross-validation** capability for passivbot, exactly as they were given (bit by bit),
together with the agreed interpretation, acceptance criteria, and where each is
implemented. It is the spec of record for the feature on branch `feature/walkforward`.

- **Primary module:** `src/walkforward.py` (orchestrator) + `src/tools/wfo_utils.py` (pure helpers)
- **Optimizer changes:** `src/optimize.py`, `src/config_utils.py` (all backward-compatible)
- **Default config:** `configs/wfo_hype.json` (HYPE on the `lighter` exchange)
- **Tests:** `tests/optimization/test_walkforward.py`

---

## 0. Context and goal

> "I would like to make a backtest and optimization of passivbot that is based on what
> it is doing now, but more in a cross-validation fashion."

Today the optimizer (`src/optimize.py`, DEAP NSGA-II) tunes a strategy over a single
fixed date range and reports in-sample metrics only. There is no out-of-sample (OOS)
validation, no reproducible seeding, and no convergence-based stopping. The goal is to
add a **rolling train → out-of-sample-test workflow** that:

- optimizes on a training window,
- evaluates the chosen parameters out-of-sample on the following window,
- rolls forward and repeats across the whole history,
- is **fully deterministic** so results can be trusted, cached, and reused, and
- biases each period's parameters toward the previous period's, since parameters are
  not expected to change much month to month.

"Based on what it is doing now" ⇒ reuse the existing optimizer, scoring, limits,
backtest engine, data pipeline, and config format; add the walk-forward layer on top
rather than replacing anything.

---

## 1. Functional requirements

### R1 — Training period + out-of-sample trading period
> "Choose an optimization period, like 6 months, and an out-of-sample trading period
> for 1 month after the 6 months."

- Each **window** consists of a **training window** (default **6 months**) immediately
  followed by an **out-of-sample (OOS) test window** (default **1 month**).
- The optimizer only ever sees the training window. The chosen configuration is then
  evaluated by a plain backtest over the OOS window, which the optimizer never saw.
- Windows are half-open and contiguous: train `[train_start, train_end)`, then
  test `[test_start = train_end, test_end)`.

**Acceptance:** for a history span and `train_months`/`test_months`, the module emits a
list of windows whose training spans equal `train_months` and whose test spans equal
`test_months` (the final test window may be clamped to the end of history — see R12).

**Where:** `wfo_utils.generate_windows(...)`; window dates are injected into the child
optimize/backtest configs as `backtest.start_date` / `backtest.end_date`.

### R2 — Roll the window forward, retrain every month
> "Roll the window (new training every month)."

- After each window, the whole train+test window advances by a **step** (default
  **1 month**), and a **new optimization** is run on the new training window.
- By default `step_months == test_months`, so the OOS test segments are **contiguous
  and non-overlapping** — they can be spliced into one continuous OOS track record (R11).
- The step is configurable independently of the test length (overlapping steps are
  allowed but then OOS stitching must de-duplicate overlapping timestamps).

**Acceptance:** with `step == test`, each window's `test_start` equals the previous
window's `test_end`; the number of windows matches the history length.

**Where:** `wfo_utils.generate_windows(...)` advances `train_start` by `step_months`.

### R3 — A self-contained "walk-forward module"
> "It should be kind of a walk-forward module or something."

- The feature is a distinct module/orchestrator, not changes scattered through the
  existing single-shot paths.
- The orchestrator drives the existing `optimize.py` and `backtest.py` per window as
  **isolated subprocesses** (clean DEAP global state, multiprocessing pool, and shared
  memory per window; reproducible child environments).

**Where:** `src/walkforward.py` (CLI + orchestration), `src/tools/wfo_utils.py`
(side-effect-free helpers).

### R4 — New git branch
> "Put these changes in a new branch, even if they should not in principle interfere
> with the rest."

- All work lands on a dedicated branch; changes to shared files
  (`optimize.py`, `config_utils.py`) must be **backward-compatible no-ops** when the
  walk-forward feature is not used.

**Acceptance:** running the normal optimizer/backtester with no new flags or config
keys produces unchanged behavior.

**Where:** branch `feature/walkforward`. New optimizer keys default to inert values
(`optimize.seed=None`, `optimize.stop.patience=0`, `optimize.proximity.weight=0.0`).

---

## 2. Determinism and reproducibility

### R5 — Fixed random seeds for cross-validation
> "Have a rule to always have the same random seeds when we want to do a cross-validation
> backtest, if that makes sense."

- The optimization must be **seeded** so that a given window is reproducible.
- The walk-forward run uses a single `base_seed`; each window derives a deterministic
  per-window seed as `seed(window_i) = base_seed + i`. This keeps each window
  individually reproducible while differing across windows, and makes the whole run
  reproducible.

**Where:** `optimize.py` seeds both `random` and `numpy.random` in the main process
before population creation (sufficient because all stochastic operations —
initial population, DEAP `varOr`/mate/mutate, perturbations — run in the main process,
and per-candidate backtests are deterministic and assigned back by index). Exposed via
`optimize.seed` / the `--seed` flag; the orchestrator sets the per-window seed.

### R6 — Determinism by design (same inputs ⇒ same result)
> "If rerun with the same criteria and data and time period, it should always give the
> same result."

- For identical (base config, seed, training period, stop criteria, warm-start config,
  and historical data), the optimization must produce the **same** chosen configuration
  and the **same** OOS result, every time.
- Child subprocesses run with `PYTHONHASHSEED=0` for hash-order stability; the Pareto
  front is read in sorted order with explicit tie-breaks; metric/objective reductions
  are order-independent.

**Acceptance (verified):** two runs of the same config produce identical per-window
`objectives`, identical chosen configuration, and identical stitched OOS metrics.

**Where:** seeding (R5), `PYTHONHASHSEED=0` in `_child_env()`, deterministic Pareto
selection in `wfo_utils.select_best_from_pareto(...)`.

---

## 3. Stopping criteria

### R7 — Clear, deterministic stop rule ("converged good enough")
> "Have clear criteria to stop the optimization for the given 6-month period … some
> parameter to determine when the training should stop, when it converged good enough
> (for this one I am not sure what to use, feel free to suggest)."

- **Agreed approach (selected from the offered options): convergence early-stop with a
  hard budget cap.** A window's optimization stops at whichever comes first:
  1. **Convergence:** the best penalized objective signal improves by less than a
     relative threshold (`min_rel_improvement`) for `patience` consecutive generations; or
  2. **Budget cap:** the evaluation budget (`iters`, optionally capped by `max_evals`)
     is exhausted.
- The convergence signal is the sum of the per-objective minimums compiled each
  generation (deterministic, order-independent). Objectives are minimized, so a drop in
  that sum is an improvement.
- Must remain fully deterministic given the seed.
- **No-op default:** `patience = 0` and `max_evals = 0` reproduce today's fixed-budget
  behavior, so existing optimizer runs are unchanged.

**Meta-parameters:** `stop.patience`, `stop.min_rel_improvement`, `stop.max_evals`.

**Where:** `ea_mu_plus_lambda_stream(...)` in `optimize.py` (the generation loop
breaks on the patience condition and logs the stop reason); `max_evals` caps `ngen`.

---

## 4. Parameter stability between periods

### R8 — Bias new parameters toward the previous period's
> "I am not expecting parameters to move a lot at every 1-month slide, so the algorithm
> should somehow bias towards new parameters that are not too far from the previous ones."

- **Agreed approach (selected from the offered options): warm-start + proximity
  penalty.**
  1. **Warm-start:** each window's initial population is seeded with the previous
     window's chosen configuration (using the existing `--start` mechanism), so the
     search begins near the last solution.
  2. **Proximity penalty:** a tunable penalty proportional to the **normalized**
     parameter distance from the previous window's chosen config is added to every
     objective, softly pulling the search toward staying close.
- The penalty is normalized per parameter by its bound range (scale-invariant) and is
  computed as an RMS distance, scaled by `proximity_weight`.
- `proximity_weight = 0` ⇒ **pure warm-start** (no penalty); higher values enforce more
  stickiness. The penalty is added to each minimized objective (not only the constraint
  term) to preserve NSGA-II front diversity.
- **Window 0 is the exception:** it has no previous window, so it is only *warm-started*
  from the initial config (R9) and optimizes **without** a proximity penalty (its
  `proximity.weight` is forced to 0 regardless of `proximity_weight`). The penalty
  applies from window 1 onward, biasing each window toward the previous *optimized*
  window — not toward the hand-tuned initial config, which was fit on other data.

**Meta-parameter:** `proximity_weight` (and, internally per window,
`optimize.proximity.reference_config` pointing at the previous window's config).

**Where:** `Evaluator._proximity_penalty(...)` / `_init_proximity(...)` in `optimize.py`;
the orchestrator sets the reference to the previous window's `train_best.json`.

---

## 5. Starting state and history

### R9 — Base/starting config for the first window
> "There should be a selection of a base config (starting state) for the starting point
> of the first walk-forward segment (at start of lighter HYPE history here); and have
> `hype_top.json` for it by default. This is just a starting point, of course, and it
> will change as periods rotate."

- The **first** window has no "previous window", so it warm-starts from an explicit
  **initial config**, default `configs/hype_top.json`.
- This is only a seed for window 0; from window 1 on, the warm-start is the previous
  window's chosen config, so the strategy "rotates" forward on its own.
- The default history start is the **start of lighter HYPE history** (`2025-02-24`,
  inherited from `configs/config_hype.json`), overridable via `start_date`.

**Meta-parameter:** `initial_config` (default `configs/hype_top.json`).

**Where:** `walk_forward.initial_config`; window 0 passes it to `--start`; later windows
pass the previous `train_best.json`.

### R10 — Save every period's config (history)
> "Of course the different configs after each month should all be saved one by one so we
> can keep track of history."

- Each window's chosen configuration is saved individually and **chronologically**, so
  the full rotation history is auditable.
- Saved as: `window_NN/train_best.json`, plus a dated copy
  `configs_history/window_NN_<train_end>.json`; the most recent is also written as
  `latest_config.json`.

**Where:** the per-window loop in `walkforward.py`.

---

## 6. Usability for backtest and live

### R11 — Outputs usable for both backtest and live
> "It should work both for backtest and live run, of course."

- Every saved configuration is a **complete, valid passivbot config** (full `bot`,
  `live`, `backtest`, `optimize` sections), so it can be:
  - re-backtested over any range, and
  - deployed to the live bot unchanged.
- The walk-forward run also produces a continuous **out-of-sample track record** by
  splicing consecutive OOS test segments into one equity curve (compounding each
  segment's return), plus aggregate OOS metrics and an in-sample-vs-OOS comparison to
  surface overfitting.

**Outputs:** `latest_config.json` (current deployable config), per-window
`train_best.json` + `window_summary.json`, `configs_history/`,
`walkforward_summary/walkforward_summary.json`, `stitched_equity.csv` + `.png`.

**Where:** `wfo_utils.stitch_oos_equity(...)`, the aggregation section of `walkforward.py`.

### R12 — Determinism enables reuse/caching across backtest and live
> "If a given optimization is done for a given walk-forward period (given month), it
> should be used for backtests or for the live bot, since it should be deterministic by
> design. No matter if the optimization calculation came from a live run or a backtest
> originally, it should be detected and used by any backtest and live-bot rerun as long
> as the meta-parameters (initial config, random seeds, given-period optimization
> completion criteria, …) are the same."

- Because a window's result is fully determined by its meta-parameters, each window's
  optimization is **content-addressed and cached**. Any later **walk-forward rerun**
  with the **same meta-parameters** — whether you run it to produce a backtest track
  record or to refresh the live config — **detects and reuses** the cached result
  instead of recomputing. The chosen config is then deployed / re-backtested as an
  ordinary config file; the **live bot loads that file** (`latest_config.json` or a
  `configs_history/` entry) and does not query the cache directly. There is no
  standalone "fetch this window from the cache" path because a window's key depends on
  all prior windows via the warm-start chain — so reuse is realized by replaying the
  chain through the cache, which is exactly what a rerun does (cheap: matched windows
  are cache hits).
- The **cache key** is a hash of the meta-parameters that determine the result:
  - the cleaned training config — bot **parameter bounds**, scoring, limits, **seed**,
    **stop criteria**, **proximity weight**, **training period dates**, exchanges,
    starting balance, iters, population — and
  - the **content of the warm-start config** (its strategy parameters).
- The key deliberately **excludes** values that do not affect the result: output
  directories, cpu count, plotting, machine-specific data paths, and config metadata
  (per-run transform logs/timestamps, `live.base_config_path`). This makes the key
  **run-invariant** so reuse works across runs, backtests, and live.
- The cache is shared across runs (default `runs/walkforward/_cache/<key>/choice.json`);
  `--no-cache` disables it.

**Acceptance (verified):** a first run computes and stores each window's result; a
second run with the same config produces **cache hits** for every window, with identical
`cache_key`, chosen config, objectives, and OOS results.

**Where:** `walkforward.window_cache_key(...)`, `_hash_config_file(...)` (hashes the
warm-start `bot` section only), and the cache load/save in the per-window loop.

---

## 7. Meta-parameters (reference)

All live in the top-level `walk_forward` block of the WFO config and are overridable by
CLI flags (CLI > config block > default). Precedence and names:

| Meta-parameter | Default | CLI flag | Meaning |
|---|---|---|---|
| `train_months` | 6 | `--train-months` | Length of the training window |
| `test_months` | 1 | `--test-months` | Length of the OOS test window |
| `step_months` | 1 | `--step-months` | How far the window rolls each iteration |
| `start_date` | config `backtest.start_date` | `--start-date` | History span start (default = start of lighter HYPE history, 2025-02-24) |
| `end_date` | config `backtest.end_date` ("now") | `--end-date` | History span end (resolved once and pinned) |
| `base_seed` | 0 | `--base-seed` | Per-window seed = `base_seed + window_index` |
| `calendar_months` | true | `--calendar-months` / `--fixed-30day` | Calendar-month vs fixed-30-day deltas |
| `min_test_days` | 7 | — | Drop a trailing partial OOS window shorter than this |
| `proximity_weight` | 0.0 | `--proximity-weight` | Strength of the bias toward the previous config (0 = pure warm-start) |
| `initial_config` | `configs/hype_top.json` | `--initial-config` | Warm-start config for window 0 |
| `stop.patience` | 0 | `--patience` | Generations without improvement before early-stop (0 = off) |
| `stop.min_rel_improvement` | 0.0 | `--min-rel-improvement` | Relative-improvement threshold for "improved" |
| `stop.max_evals` | 0 | `--max-evals` | Hard cap on evaluations per window (0 = no cap) |
| `run_id` | timestamp | `--run-id` | Output folder name under `runs/walkforward/` |
| (cache dir) | `runs/walkforward/_cache` | `--cache-dir` / `--no-cache` | Shared optimization cache |

Note: `iters`, `population_size`, scoring, limits, and bounds come from the base config's
`optimize` block (e.g. `configs/wfo_hype.json`) and are reused as-is.

---

## 8. Output layout

```
runs/walkforward/<run_id>/
  windows.json                 # generated windows (also printed by --dry-run)
  walkforward_config.json      # resolved config + walk_forward block (provenance)
  configs_history/
    window_00_<train_end>.json # chronological copies of each chosen config (R10)
    window_01_<train_end>.json
    ...
  latest_config.json           # most recent chosen config (deployable, R11)
  window_NN/
    train/{train_config.json, optimize.log, optimize_results/pareto/*.json}
    train_best.json            # chosen scalarized config for the window
    test/{test_config.json, backtest.log, bt/.../analysis.json, balance_and_equity.csv.gz}
    window_summary.json        # dates, seed, cache key/hit, chosen hash, IS-vs-OOS, drift
  walkforward_summary/
    walkforward_summary.json   # per-window records + aggregate OOS + overfit + drift
    stitched_equity.csv / .png # continuous out-of-sample track record (R11)
runs/walkforward/_cache/<key>/choice.json   # content-addressed optimization cache (R12)
```

---

## 9. Constraints and non-goals

- **Backward compatibility:** new optimizer config keys and flags are no-ops when unset;
  the existing single-shot optimize/backtest behavior is unchanged (R4).
- **Reuse, don't rewrite:** the feature builds on the existing DEAP optimizer, scoring,
  limits, backtest engine, data pipeline, and config format (Context).
- **Single config per window:** NSGA-II yields a Pareto front; the **scalarized best**
  (closest to the normalized ideal, feasible solutions preferred, deterministic
  tie-break) is chosen as the one config carried to the OOS test and to the next
  window's warm-start.
- **Data assumption:** the cache assumes stable historical data for a given
  (exchange, coins, date range); raw candles are downloaded once into a shared cache.
- **Suite mode** (multi-scenario per candidate) is orthogonal; OOS evaluation uses a
  plain backtest.

---

## 10. How to run

Use the `passivbot` conda env (per `CLAUDE.md`), with `PYTHONPATH` set to `src`:

```powershell
$env:PYTHONPATH="C:\Users\david\Desktop\freqtrade\passivbot_lighter\src"
$env:PYTHONHASHSEED="0"
$py = "C:\Users\david\miniconda3\envs\passivbot\python.exe"

# Preview the windows without running anything:
& $py src\walkforward.py --config configs\wfo_hype.json --dry-run

# Full run (6mo train / 1mo OOS, monthly roll), with parameter stickiness and early-stop:
& $py src\walkforward.py --config configs\wfo_hype.json `
    --train-months 6 --test-months 1 --step-months 1 `
    --base-seed 0 --proximity-weight 0.05 `
    --patience 20 --min-rel-improvement 0.001
```

A rerun with the same config reuses cached optimizations automatically. Deploy
`runs/walkforward/<run_id>/latest_config.json` (or any `configs_history/` entry) to the
live bot or re-backtest it directly.

---

## 11. Acceptance criteria summary

| # | Requirement | Verified by |
|---|---|---|
| R1 | 6mo train + 1mo OOS windows | `generate_windows` unit tests; dry-run on `wfo_hype.json` (10 windows) |
| R2 | Monthly roll, contiguous OOS | unit tests assert `test_start == prev test_end` |
| R3 | Walk-forward module | `src/walkforward.py` + `src/tools/wfo_utils.py` |
| R4 | New branch, no-op when off | branch `feature/walkforward`; inert defaults; regression suites pass |
| R5 | Fixed seeds | `--seed`/`optimize.seed`; per-window `base_seed + i` |
| R6 | Same inputs ⇒ same result | two-run smoke: identical objectives & OOS |
| R7 | Convergence stop + cap | early-stop unit test; `stop.*` keys; `patience=0` = legacy |
| R8 | Bias toward previous params | warm-start chain + proximity penalty (weight 0 = pure warm-start) |
| R9 | Starting config default | `initial_config` = `configs/hype_top.json` |
| R10 | Save every config chronologically | `configs_history/` + `latest_config.json` |
| R11 | Backtest- and live-ready outputs | complete configs; stitched OOS equity + overfit table |
| R12 | Deterministic reuse/caching | two-run smoke: all windows cache-hit with identical results |
| R13 | Live auto-rolls monthly | `wfo_scheduler.py` + bot watcher; `current_live_window` unit tests |
| R14 | Trading off until month's optimize done | scheduler publishes only on completion; bot bootstraps to ADOPT |
| R15 | Boundary handoff (tp_only + small-loss flatten) | `should_flatten` unit tests; `_wfo_wind_down` |
| R16 | Restart recovery (local + docker) | soft-restart in-process; positions re-read from exchange |
| R17 | Stateful OOS carry (continuous equity) | `advance_carry` unit tests; carry-parity backtest |
| R18 | Cross-platform VPS pickup | portable JSON artifacts; relative `config_path`; bot swaps `bot` only |
| R19 | Trade-count overfit guard | `select_with_trade_guard` unit tests; real-front sanity check |

---

## R19 — Trade-count overfit guard at selection

A window's optimizer returns a Pareto front and the scalarized winner can be **overfit to a
few very profitable trades** — trading far less than the previous month. To guard against this,
after the front is produced the chosen config is filtered on its **in-sample trade rate**
(`positions_held_per_day`, the per-candidate trade-frequency metric): if the top candidate's rate
is below `min_trade_ratio` (default **0.5**) of the **previous window's chosen config**, walk
down the ranked front to the next-best candidate that holds up. If none qualify, the
**highest-trade-rate** candidate is taken (closest to passing) with a logged warning. Window 0
(no previous month) and `min_trade_ratio = 0` disable the guard.

- **In-sample only** (no peek at the OOS month) — selection stays fully out-of-sample.
- **Smart cache:** the Pareto **front** is cached (`candidates.json`), so the guard re-selects
  cheaply on a cache hit; changing `min_trade_ratio` never re-optimizes a front, but a changed
  choice cascades through the warm-start chain to re-optimize downstream windows.
- The decision (`trade_guard`) and chosen `trade_rate` are recorded per window in
  `window_summary.json`.

**Where:** `wfo_utils.rank_pareto_candidates` / `pareto_trade_rate` / `select_with_trade_guard`;
the per-window loop in `walkforward.run` and the chain replay in `wfo_scheduler.run_once`.
**Meta-parameter:** `min_trade_ratio` (CLI `--min-trade-ratio`).

---

## 12. Live rolling cross-validation

The backtest walk-forward above (R1–R12) and the **live** bot run the *same* rolling
cross-validation idea: at any wall-clock moment the bot trades the config that was
optimized on the **6 months immediately before the current test month** and has never
seen the current month — so **all live trading is out-of-sample by construction**, just
as every OOS test segment is in backtest.

### Architecture: decoupled scheduler ↔ bot watcher

Two cooperating processes, so a resource-heavy optimization never blocks or destabilizes
the live trading loop (and, per R18, can even run on a *different machine*):

- **Scheduler** (`src/wfo_scheduler.py`) — resolves the *current live window* from a fixed
  anchor (`current_live_window`), replays the warm-start window chain through the shared
  content-addressed cache (R12) so each month optimizes **once**, and **publishes** the
  result as portable JSON artifacts under `live.wfo_rolling.active_dir`
  (default `runs/walkforward/live/`):
  - `active_config.json` — the full, deployable config for the current month;
  - `active.json` — a small pointer `{period, test_start, test_end, config_path}` where
    `config_path` is the **relative, co-located** name `"active_config.json"` (R18);
  - `configs_history/` — chronological copies.
  Writes are atomic (tmp + `fsync` + `os.replace`) so the bot never reads a half-written
  file. Runs as a loop or one-shot (`--once` / `--dry-run`).
- **Bot watcher** (`src/passivbot.py`) — when `live.wfo_rolling.enabled`, the bot adds a
  `maintain_wfo_rolling` data-maintainer that periodically (`check_interval_minutes`)
  reads `active.json` and runs the **state machine** (`decide_rolling_state`):

  | State | Trigger | Bot action |
  |---|---|---|
  | **NORMAL** | published period == loaded period, calendar aligned | trade normally |
  | **WIND_DOWN** | calendar has rolled past the loaded period, next config not yet published | stop opening; apply the **boundary handoff rule** to existing positions |
  | **ADOPT** | a *different* period is published (or nothing loaded yet) | **soft-restart** to load the new config |

### R13 — Live auto-rolls every month (retrain → use → repeat)
> "if we start a new month … it will automatically move to next period and train model to
> converged; model is used during this month, until next month where a new optimization occurs."

- The current live window is a deterministic function of `today` and a fixed anchor
  (`current_live_window(anchor, train_months, test_months, step_months, today)`), so the
  scheduler and bot always agree on which month is live without shared mutable state.
- On a new month the scheduler optimizes the new 6-month training window **to convergence**
  (R7 early-stop) and publishes; the bot adopts it for that month, then repeats.

**Where:** `wfo_handoff.current_live_window`, `wfo_scheduler.run_once`/`run`.

### R14 — Trading off until the month's optimization is complete
> "at start of month there should be a period where trading is off and not reactivated
> until the new optimization for the given month is completed."

- The scheduler **only publishes a new `active.json` when the month's optimize+OOS chain
  has finished**. Until then the bot stays in **WIND_DOWN** (no new entries) for the prior
  config, and flips to **ADOPT** (which soft-restarts into NORMAL) the moment the new
  artifact appears. There is no window in which the bot opens positions on a not-yet-trained
  month.

**Where:** `decide_rolling_state` (WIND_DOWN vs ADOPT), `_wfo_rolling_tick`.

### R15 — Boundary handoff for open positions (tp_only + small-loss flatten)
> "there is a little problem if at end of month a position is open … the rule could be to
> only close it if it is in profit or a fairly small unrealized drawdown (<5% of total TWE)."

- During **WIND_DOWN** the bot:
  1. sets `forced_mode_long/short = "tp_only"` globally (no new entries; let take-profits run), and
  2. for each open position that **`should_flatten`** — `uPnL ≥ 0` **or**
     `|uPnL| < max_loss_flatten_frac × total_wallet_equity` (default **5%**) — forces that
     symbol to `"panic"` via a per-symbol `coin_overrides`, so winners and small losers are
     flattened at the seam while a position in a larger drawdown is **kept** and carried into
     the next month rather than realized at a bad time.
- The identical rule (`should_flatten`) is reused by the backtest stateful carry (R17), so
  live and backtest treat the boundary the same way.

**Where:** `wfo_handoff.should_flatten`, `passivbot._wfo_wind_down`.

### R16 — Recover from any restart (local or docker)
> "it must be able to recover any restart, running local or with docker."

- Adoption uses an **in-process soft-restart** (`soft_restart_requested` breaks the
  execution loop; `main()` re-reads the active config and rebuilds the bot **without**
  counting against the hard-restart budget), which behaves identically whether the bot runs
  as a local process or inside the `restart: unless-stopped` docker container.
- On any restart the bot **re-reads open positions from the exchange** (`fetched_positions`)
  and re-derives the live window from the anchor + `active.json`, so it resumes the correct
  month and the correct NORMAL/WIND_DOWN/ADOPT state with no local checkpoint required.
- The bot adopts by **swapping only the `bot` section** from `active_config.json` into a
  deep copy of its pristine startup config (`_maybe_adopt_wfo_config`), so operator-managed
  `live`/keys settings are preserved across rolls.

**Where:** `passivbot.maintain_wfo_rolling`/`_wfo_rolling_tick`, `_maybe_adopt_wfo_config`,
the soft-restart handling in `main()`.

---

## 13. Stateful OOS carry-over (R17)

> "all trading is always done out of sample during backtest … (and obviously during live)"

By default the backtest stitches consecutive OOS segments by **compounding returns** from a
flat start each month (R11) — correct for a track record, but it does not model an open
position surviving a month boundary the way live does. Opt-in **`walk_forward.stateful_oos`**
makes the backtest carry **balance + open positions** across OOS seams using the **same**
handoff rule live uses, producing one **value-continuous** OOS equity curve:

- The engine emits an **`end_state.json`** per segment (final equity + open positions with
  uPnL) when `backtest.wfo_write_end_state` is set (`compute_end_state`).
- `advance_carry(end_state, max_loss_flatten_frac)` applies `should_flatten` (R15): kept
  positions carry forward; flattened positions' mark-to-market becomes cash, so
  `next_balance = equity − Σ uPnL(kept)`.
- The next segment is seeded via `backtest.starting_balance` + `backtest.initial_positions`
  (the Rust engine seeds `Positions` from these), and `stitch_oos_equity(..., stateful=True)`
  **concatenates** raw equity (de-duping the overlapping seam timestamp) instead of compounding.
- **`stateful_oos` off ⇒ byte-identical to the historical behavior** (default off).

**Fidelity model & its bound.** Seeding reconstructs the boundary state exactly; the only
divergence from a hypothetical *continuous* run is that per-coin **EMA state resets** at each
seam (each segment is a fresh backtest process). Measured on a 2-month HYPE carry-parity check
(one fixed config, a real open long carried across the seam, no flatten — to isolate the
plumbing from the handoff rule):

| Check | Result |
|---|---|
| `end_state.equity` vs continuous equity at the boundary | rel **1.8e-4** (residual = 60-min balance sampling) |
| seg-2 first-bar equity vs `end_state.equity` (MTM reconstruction) | rel **9.6e-4** |
| seg-2 final equity vs continuous final equity (full stitched fidelity) | rel **5.3e-5** |

So the carry plumbing is exact to sampling granularity, and full-curve fidelity is bounded by
the EMA-warmup transient at each seam (small here; grows with shorter test windows / longer EMAs).

**Not yet modeled:** `walk_forward.retrain_delay_days` (a knob for the real-world lag between
month start and the new config being ready) exists but is **not** reflected in the stateful
stitch — the stitch assumes the new config is live from the first bar of the test month.

**Where:** `backtest.compute_end_state` + gated `end_state.json` write in `post_process`;
`wfo_handoff.advance_carry`; `wfo_utils.stitch_oos_equity(stateful=True)` / `_concat_oos_equity`;
the stateful OOS loop in `walkforward.run`; Rust `BacktestParams.initial_positions_{long,short}`.

---

## 14. VPS deployment flow (R18)

> "live (not backtest) will run on a VPS with limited resources … beginning-of-month
> optimizations will probably have to be run locally and uploaded to the VPS. The VPS live
> (clone of current code) should pick them up automatically. VPS is Linux; here we are on Windows."

The decoupled design (Section 12) supports a **split deployment** with no code changes:

1. **Optimize locally** (Windows, full CPU): run the scheduler one-shot for the current month
   — `python src/wfo_scheduler.py --config configs/wfo_hype.json --once`. It writes the
   portable artifacts under `runs/walkforward/live/`.
2. **Upload** that directory's `active_config.json` + `active.json` (+ `configs_history/`) to
   the VPS's `live.wfo_rolling.active_dir`.
3. **VPS auto-adopts**: the live bot's watcher sees the new `active.json`, runs
   `decide_rolling_state` → **ADOPT**, and soft-restarts into the new config — no scheduler
   needed on the VPS.

Portability guarantees that make this safe across OS:
- Artifacts are **plain JSON** with **relative, co-located** paths (`active.json.config_path
  = "active_config.json"`), so an absolute Windows path is never baked in.
- Adoption swaps **only the `bot` section** into the VPS bot's own pristine config, so the
  VPS keeps its own keys, exchange wiring, and `live` settings.
- The live window is derived from a **fixed anchor + `today`**, identical on both machines, so
  local and VPS always agree on which month is live.

To instead run everything on one capable host, enable the bundled
`passivbot-wfo-scheduler` docker service (`profiles: ["wfo"]`) alongside the live bot.

### New config keys (live rolling + stateful carry)

| Key | Default | Meaning |
|---|---|---|
| `live.wfo_rolling.enabled` | `false` | Turn on the bot-side watcher |
| `live.wfo_rolling.active_dir` | `runs/walkforward/live` | Where the bot reads `active.json` / `active_config.json` |
| `live.wfo_rolling.max_loss_flatten_frac` | `0.05` | Boundary handoff threshold (R15) |
| `live.wfo_rolling.check_interval_minutes` | `60.0` | How often the watcher polls `active.json` |
| `walk_forward.min_trade_ratio` | `0.5` | Overfit guard: reject a chosen config trading below this fraction of last month (R19); 0 disables |
| `walk_forward.stateful_oos` | `false` | Carry balance+positions across OOS seams (R17) |
| `walk_forward.max_loss_flatten_frac` | `0.05` | Backtest copy of the handoff threshold |
| `walk_forward.retrain_delay_days` | `0` | Intended month-start lag (knob exists; **not yet** modeled in the stitch) |
| `backtest.initial_positions` | `{}` | Seed open positions (coin → side → {size, price}); engine input for R17 |
| `backtest.wfo_write_end_state` | `false` | Emit `end_state.json` for the next segment's carry |
