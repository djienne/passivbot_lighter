# WFO Live Deployment — Architecture & Operations Runbook

Operational companion to **`WALK_FORWARD_REQUIREMENTS.md`** (the feature spec, R1–R19).
That document explains *what* walk-forward optimization is and *why*; this one explains
the *deployed system* as it actually runs today — the moving parts, how to monitor it,
the things that have bitten us, and how to fix them.

> **TL;DR for "is it healthy?"**
> ```powershell
> $env:PYTHONPATH="C:\Users\david\Desktop\freqtrade\passivbot_lighter\src"
> & "C:\Users\david\miniconda3\envs\passivbot\python.exe" src\tools\wfo_status.py
> ```
> Want `OVERALL: OK`. It checks **both** local and the remote VPS in one shot.

---

## 1. The big picture

```
        LOCAL (Windows + Docker Desktop)                 VPS (Linux, ubuntu@REDACTED-HOST)
   ┌─────────────────────────────────────────┐      ┌──────────────────────────────────────┐
   │  passivbot-wfo-local-manager (container) │      │  passivbot-lighter-live (container)    │
   │  └ wfo_vps_manager.py watch              │      │  └ src/main.py  (live trading only)    │
   │     every 15 min:                        │      │     PASSIVBOT_REMOTE_LIVE=1  ← guard   │
   │      1. optimize-once (subprocess →      │ SSH  │     live.wfo_rolling.enabled = true    │
   │         wfo_scheduler.py --once)         │ ───► │       └ watcher reads active.json,     │
   │         replays window chain via CACHE   │ scp  │         soft-restarts to ADOPT new cfg │
   │      2. upload-active  (only if the      │      │                                        │
   │         config actually changed)         │      │  runs/walkforward/live/active.json     │
   └─────────────────────────────────────────┘      └──────────────────────────────────────┘
                                                                  ▲ reads pnls + debug log over SSH
   ┌─────────────────────────────────────────┐                   │
   │  STREAMING_LIVE_PASSIBOT dashboard       │ ──────────────────┘
   │  (streaming_live_passibot-dashboard-1)   │
   └─────────────────────────────────────────┘
```

**Core principle (R12/R18):** a window's optimization result is fully determined by its
meta-parameters, so it is **content-addressed and cached**. The manager never re-optimizes
a month that is already cached — it just *replays the chain* (instant cache hits) and only
does real CPU work when a genuinely new training window appears. The VPS only ever *consumes*
finished configs; it is hard-blocked from optimizing.

---

## 2. Components

| Component | Where | Role |
|---|---|---|
| **Local manager** | container `passivbot-wfo-local-manager` (`docker-compose.wfo-local.yml`, image `passivbot-lighter-wfo:latest` from `Dockerfile_wfo`) | Always-on. Every 15 min: optimize-once → upload-if-changed. The **only** thing that runs the optimizer. |
| **Scheduler** | `src/wfo_scheduler.py` (run as a subprocess by the manager) | Resolves the current live window, replays the warm-start chain through the cache, publishes `active.json` + `active_config.json`. |
| **Optimization cache** | `runs/walkforward/_cache/<key>/candidates.json` | Content-addressed Pareto fronts. Shared by backtest **and** live. A cache HIT = no recompute. |
| **Published artifacts** | `runs/walkforward/live/` (`active.json`, `active_config.json`, `configs_history/`) | The portable, deployable handoff. Uploaded to the VPS. |
| **VPS live bot** | container `passivbot-lighter-live` on `REDACTED-HOST` | Trades live only. Watcher adopts new configs via soft-restart. Never optimizes. |
| **Dashboard** | `streaming_live_passibot-dashboard-1` (`C:\Users\david\Desktop\freqtrade\STREAMING_LIVE_PASSIBOT`) | Reads account-keyed files (`lighter_01_pnls.json`, `passivbot_debug.log`) over SSH. Unaffected by config rotation → history is continuous. |
| **Health sweep** | `src/tools/wfo_status.py` | Read-only, no side effects. Checks the whole chain (local + remote) in one command. |
| **Manager CLI** | `src/tools/wfo_vps_manager.py` | All the operator verbs (see §6). |

**Profiles** (`configs/wfo_profiles/`): `hype_t6`, **`hype_t8` (live)**, `hype_t10`, `hype_t12`
— the number is the training-window length in months. Meta defaults live in
`configs/wfo_meta.json`.

---

## 3. The monthly lifecycle — what happens on the 24th

Windows are anchored at **2025-02-24** with a 1-month step, so the boundary is always the
**24th**. Current state: **window 7**, live `2026-05-24..2026-06-24`.

**Next rollover: 2026-06-24 → window 8** (train `2025-10-24→2026-06-24`, live
`2026-06-24→2026-07-24`).

What *should* happen automatically, with no human action:

1. On/after the 24th, the scheduler resolves window 8 as the current live window.
2. Window 8 has a **new training window** → **new `cache_key`** → **cache MISS** → the manager
   runs a real local optimize (~10–20 min, full CPU).
3. The new config is published locally and **uploaded** to the VPS (config changed → no skip).
4. The VPS watcher sees the new `active.json`, runs the state machine, and **ADOPTs** via
   soft-restart. During the seam (before the upload lands) it is in **WIND_DOWN** (no new
   entries; small winners/losers flattened, larger drawdowns carried — R15), then NORMAL on
   the new month. **See §3.1 for exactly what the trader does during that gap.**

**How to confirm it landed (run the sweep, or check logs):**
- `docker logs passivbot-wfo-local-manager` shows `window 08 | optimized` then an upload
  (not "skipping upload").
- Remote `active.json` flips to `period 2026-06-24..2026-07-24 / window_index 8`
  (the sweep's `remote == local config` row).
- VPS bot stays `Up (healthy)`.

> Until then, every 15-min tick is a no-op: all windows cache-HIT, config unchanged,
> `Remote already has … skipping upload`. That is the correct idle state.

### 3.1 What the live trader does during the seam (no fresh config yet)

There is a gap on the 24th between *the calendar rolling into the new month* (00:00 UTC)
and *the new config being uploaded* (after the local optimize finishes — typically
~10–30 min, occasionally longer). **The VPS bot never goes dark and never needs the new
config to keep itself safe.** It keeps running the *old* (e.g. window-7) config but flips
into a deliberately conservative mode. The watcher ticks every **1 minute** on the VPS
(`--live.wfo_rolling.check_interval_minutes 1`) and runs a 3-state machine
(`decide_rolling_state`, `src/tools/wfo_handoff.py`):

| Phase | When | Comparison | State | Behaviour |
|---|---|---|---|---|
| **Before** | up to 00:00 UTC Jun 24 | calendar window == published window | **NORMAL** | trades the window-7 config normally |
| **The gap** | 00:00 Jun 24 → window-8 upload | calendar (`today_test_start`) **>** published (`published_test_start`), but `active.json` still window 7 | **WIND_DOWN** | runs window-7 config in safe mode (below) |
| **Adopted** | window-8 `active.json` arrives | published period **!=** loaded period | **ADOPT** | soft-restart, swap in new `bot` section → back to NORMAL |

**What WIND_DOWN does to the account** (`_wfo_wind_down`, `src/passivbot.py`):

1. **No new entries** — `forced_mode_long/short = "tp_only"`. Existing positions can still
   take profit; nothing new opens. This is the point: it will **not** put on fresh exposure
   into the un-trained new month using a stale config.
2. **Closes winners and small losers** — per position, `should_flatten` (`wfo_handoff.py`):
   in profit **OR** unrealized loss **< `max_loss_flatten_frac` (2%) of equity** → market-close.
3. **Keeps big losers** — anything losing **> 2% of equity** is held and handed off so the
   incoming window-8 config inherits and manages it. (Mirrors the backtest's stateful carry,
   so live ≈ sim — R15.)

So during the seam: profits are taken, small losers cleared, large drawdowns parked, and no
new positions open — until the new config lands and the bot ADOPTs it.

**The one real risk:** the gap closes **only if the local manager publishes the new window.**
If `passivbot-wfo-local-manager` is down/crashed on the 24th, the bot stays in **WIND_DOWN
indefinitely** — which is *safe* (no new trades, sits on any held losers) but means **no
trading until the optimizer is fixed**. The failure mode is "stuck in tp_only," never "trades
the wrong config." On the 24th, if the sweep's `remote == local config` row is still
`window 7` more than ~30–45 min past 00:00 UTC, the manager didn't deliver — see §8.

---

## 4. Monitoring

**On demand (the main tool):**
```powershell
$env:PYTHONPATH="C:\Users\david\Desktop\freqtrade\passivbot_lighter\src"
& "C:\Users\david\miniconda3\envs\passivbot\python.exe" src\tools\wfo_status.py
```
It reports, with `OK`/`WARN`/`FAIL` and an `OVERALL`:
local `active.json`, local manager container + log tail, dashboard, days to next rollover,
**VPS** bot health, the remote optimize-guard, remote-vs-local config match, VPS debug-log
freshness (liveness), and last-fill age. Exit code 0 unless something FAILed.
`--no-remote` skips SSH (local-only); `--profile hype_tN` for another profile.

**Tail the manager loop:**
```powershell
docker logs passivbot-wfo-local-manager --tail 60 -f
```

**Scheduled:** a daily session cron (≈9:13 AM) runs the sweep and alerts on WARN/FAIL.
It is **session-bound and auto-expires after 7 days** (Claude Code limit) — it is a
convenience, not the safety net. The actual rollover does **not** depend on it; the
always-on container does that. Re-arm it any time by asking for a daily WFO health check.

---

## 5. ⚠️ Important points to watch out for

These are the non-obvious traps. Most are already fixed in code (commit `056c5d2`); they are
listed so a future change doesn't reintroduce them and so symptoms are recognizable.

1. **Cache validation must ignore the test window.** The candidate cache is keyed on the
   *training* window; the final (live) window's `test_end` advances with wall-clock as OOS
   data accrues. `_cache_header_ok` (in `src/walkforward.py`) compares **train-relevant
   fields only** (`index`/`train_start`/`train_end`). *If you ever see the current month's
   window "recomputing" every tick, this guard regressed* — it triggers a needless (and
   cross-platform non-deterministic) re-optimize of the live config mid-month.

2. **The SSH key permissions, two ways.**
   - *Inside the container:* the bind-mounted `lighter.pem` shows as `0777`, which Linux
     OpenSSH refuses. `secure_ssh_key()` (in `wfo_vps_manager.py`) copies it to a private
     `0600` file. It is **copy-only and must stay that way** — `chmod`-ing the bind-mounted
     original can corrupt the Windows host ACL.
   - *On the Windows host:* `ssh.exe` rejects the key if anyone but the owner can read it
     ("Bad permissions … UNKNOWN\UNKNOWN"). Fix:
     ```powershell
     icacls "C:\Users\david\Desktop\freqtrade\passivbot_lighter\lighter.pem" /inheritance:r /grant:r "DJIENNE\david:F"
     ```
     Symptom in the sweep: `VPS ssh … probe incomplete (rc=255): Bad permissions`.

3. **Cross-platform determinism is not guaranteed.** Windows-conda vs Linux-container float/
   BLAS differences mean a *recompute* of the same window could pick a slightly different
   config. The cache makes this moot **as long as we reuse cached candidates** rather than
   recomputing — which is exactly why point #1 matters.

4. **The VPS must NEVER optimize.** Enforced three ways: `PASSIVBOT_REMOTE_LIVE=1` in the
   VPS compose → `abort_if_remote_live()`; an SSH command regex guard
   (`FORBIDDEN_REMOTE_PATTERNS`) in `run_ssh`; and the VPS image lacks optimizer deps.
   The sweep's `remote opt guard` row must stay `OK`.

5. **`restart: unless-stopped` ≠ "always comes back".** A *manual* `docker stop` keeps it
   stopped across reboots. After deliberately stopping the manager, bring it back with
   `docker compose -f docker-compose.wfo-local.yml up -d`.

6. **Code changes need the right kind of restart.** The repo is bind-mounted (`./:/app/`),
   so `wfo_scheduler.py`/`walkforward.py` edits are picked up next tick (it runs as a
   subprocess). But `wfo_vps_manager.py` edits (the long-running `watch` process) need a
   container **restart** to reload. Compose-file edits need `up -d` (recreate).

7. **Log buffering.** `PYTHONUNBUFFERED=1` is set in the compose file so upload/skip lines
   appear in `docker logs` in real time. Without it, success messages lag a tick.

8. **`[health]` lines don't exist in this build.** The bot's liveness signal is the
   **debug-log mtime** (it streams ticker data continuously). The sweep uses that; don't
   re-add a `[health]` grep (it would WARN forever).

---

## 6. Manager CLI reference

`python src/tools/wfo_vps_manager.py <cmd> [--profile hype_t8] [--dry-run]`
(globals: `--ssh-key`, `--remote-user`, `--remote-host`, `--remote-path`)

| Command | What it does | Side effects |
|---|---|---|
| `status` | Print local + remote active config summary | none |
| `optimize-once` | One scheduler tick: replay chain, publish `active.json` locally | writes local artifacts; **does NOT upload** |
| `publish-local` | Validate local active is deployable (or `--from-run-dir <dir> --window-index N` to publish from a backtest run) | writes local artifacts |
| `upload-active` | Upload local active to the VPS **iff it differs** (`--force` to override) | backs up remote, scp, bot adopts |
| `watch` | The loop: optimize-once → upload-if-changed, every `--check-interval-minutes` (`--no-upload` to disable upload) | both of the above |
| `preflight-remote` | Inspect the VPS (containers, cron, compose, optimizer-reference grep) | read-only |
| `deploy-code` | Push code to the VPS | writes remote |
| `stop-live` / `start-live` | `docker compose stop/up -d passivbot-lighter-live` on the VPS | controls the VPS bot |

The container runs `watch --profile hype_t8 --check-interval-minutes 15`.

---

## 7. Switching train length (t8 → t6 / t10 / t12)

```powershell
# 1. Backfill + publish the new profile locally (first time = heavy, all windows re-optimize
#    because the cache key changes; subsequent runs are cache hits):
python src\tools\wfo_vps_manager.py optimize-once  --profile hype_t6
# 2. Upload it (bot adopts on next watcher tick):
python src\tools\wfo_vps_manager.py upload-active   --profile hype_t6
# 3. Point the always-on watcher at it: edit docker-compose.wfo-local.yml
#    (--profile hype_t6) then:
docker compose -f docker-compose.wfo-local.yml up -d
```
No VPS-side edits are needed. The VPS just adopts the new `active_config.json`.

---

## 8. Troubleshooting (symptom → cause → fix)

| Symptom (in the sweep or logs) | Likely cause | Fix |
|---|---|---|
| `local WFO manager … container not created` / `not running` | Manager was never started or was `docker stop`-ped | `docker compose -f docker-compose.wfo-local.yml up -d` |
| Manager logs show the **current** month `… -> recomputing` every tick | Cache `test_end` guard regressed (point #1) | Restore train-only compare in `_cache_header_ok`; confirm a tick is all cache-HIT |
| `upload-active failed: … UNPROTECTED PRIVATE KEY FILE … Permission denied` | Container can't use the `0777` bind-mounted key | Ensure `secure_ssh_key()` is present & copy-only; restart the container |
| Sweep `VPS ssh … Bad permissions … UNKNOWN\UNKNOWN` | Host key ACL too open | `icacls … /inheritance:r /grant:r "DJIENNE\david:F"` (point #2) |
| `remote == local config` shows **WARN** mid-month | Mid-month recompute changed the live config (bad — point #1) or a legit rollover happened | If not the 24th: investigate the recompute; if the 24th: expected, verify window index advanced |
| `remote opt guard` **FAIL** | `PASSIVBOT_REMOTE_LIVE=1` missing on the VPS | Re-add to the VPS `docker-compose.yml` env and recreate the live container |
| `VPS debug log` stale (>10 min) | Bot lost the websocket / crashed | Check `docker logs passivbot-lighter-live`; restart if needed |
| `ModuleNotFoundError: deap` in manager | Manager built from `Dockerfile_live` (live deps only) | Build/run from `Dockerfile_wfo` (full deps) — `docker compose -f docker-compose.wfo-local.yml build` |
| Manager `up -d` fails: "all predefined address pools … fully subnetted" | Too many docker networks on the host | `network_mode: bridge` is set in the compose file; keep it |
| The 24th passed but no new config on the VPS | Manager down, or upload blocked (key), or optimize failed | **Bot is safe meanwhile — stuck in WIND_DOWN/tp_only, no new entries (§3.1), not trading the wrong config.** Check manager logs around the 24th; run `optimize-once` then `upload-active` manually. Bot ADOPTs within ~1 min of the upload. |

**Never touch `passivbot-hype-live`** — that's a separate strategy (per `CLAUDE.md`).

---

## 9. Key files

```
Dockerfile_wfo                     full-deps image for the local manager (NOT for the VPS)
docker-compose.wfo-local.yml       the always-on manager service (bridge net, 8G, unbuffered)
src/tools/wfo_vps_manager.py       operator CLI (optimize/upload/watch/status/…)
src/tools/wfo_status.py            read-only health sweep (local + remote)
src/wfo_scheduler.py               publishes the current live window's config
src/walkforward.py                 orchestrator + the content-addressed cache
configs/wfo_meta.json              WFO meta defaults
configs/wfo_profiles/hype_t{6,8,10,12}.json   train-length profiles (t8 is live)
runs/walkforward/live/             published active.json / active_config.json / history
runs/walkforward/_cache/<key>/     content-addressed optimization cache
lighter.pem                        SSH key to the VPS (keep owner-only on the host)
```

See `WALK_FORWARD_REQUIREMENTS.md` for the feature design and `CLAUDE.md` for environment
conventions (conda env, docker usage).
