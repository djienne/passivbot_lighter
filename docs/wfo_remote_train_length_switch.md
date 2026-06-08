# Switching the live WFO train length (tX) on the Lighter VPS

How to change the live HYPE bot from one walk-forward train length to another
(e.g. **t8 → t12**). Verified end-to-end on 2026-06-08 when we switched the live
deployment from t8 to t12.

## Architecture (who does what)

- **Local optimizer/manager** — docker container `passivbot-wfo-local-manager`
  (compose file `docker-compose.wfo-local.yml`, image `passivbot-lighter-wfo:latest`).
  Runs `src/tools/wfo_vps_manager.py watch --profile <profile>` on a 15-min loop:
  replays the WFO window chain (cache HITs), publishes the current window's config to
  `runs/walkforward/live/{active.json,active_config.json}`, and **uploads to the VPS
  only when the config hash changes**.
- **VPS bot** — `ubuntu@<VPS>` runs `passivbot-lighter-live`; optimization is hard-blocked
  there (`PASSIVBOT_REMOTE_LIVE=1`). It re-reads the active config every minute and
  soft-adopts (the VPS host lives in gitignored `deploy_target.json` / `PASSIVBOT_REMOTE_HOST`).
- The **train length is selected by the `--profile`** the manager runs. Profiles are
  `configs/wfo_profiles/hype_t6.json`, `hype_t8.json`, `hype_t10.json`, `hype_t12.json`
  (each is a WFO meta file; the only meaningful difference is `train_months`).

The manager CLI (`wfo_vps_manager.py`) subcommands used below:
`status`, `optimize-once`, `upload-active [--force]`, `stop-live`, `start-live`,
`preflight-remote`, `watch`. All are run **inside the manager image** via
`docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager …`
so SSH key + deploy target + optimizer deps are all present (don't run them from the
host shell — `ssh.exe` is fussy about the `lighter.pem` permissions).

> **Safety:** switch only when the live position is **flat**. A config swap while a
> position is open can leave an underwater carry the new config manages differently.
> Check the dashboard / `docker logs passivbot-lighter-live` first.

---

## Procedure (controlled switch, e.g. t8 → t12)

Run from the repo root `C:\Users\david\Desktop\freqtrade\passivbot_lighter`.
Replace `hype_t12` with the target profile.

### 1. Baseline — confirm current local & remote, and SSH works
```powershell
docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager `
  python src/tools/wfo_vps_manager.py status --profile hype_t12
```
Shows `Local active:` and `Remote active:` (both the OLD hash before switching) and
`Train months: 12` for the target profile.

### 2. Stop the manager (so it can't re-publish the OLD profile mid-switch)
```powershell
docker compose -f docker-compose.wfo-local.yml stop
```

### 3. Publish the NEW profile's config locally
```powershell
docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager `
  python src/tools/wfo_vps_manager.py optimize-once --profile hype_t12
```
Replays the chain and writes `runs/walkforward/live/active_config.json` for the new
train length. All-cache-HIT → instant. A genuine cache MISS (e.g. brand-new training
window) runs a real ~10–20 min optimize — that's expected.

### 4. Verify the published config is what you expect (optional but recommended)
Compare the published bot hash to the backtest run dir for that tX
(`runs/walkforward/full_history_confighype_t12_stateful_*/window_<last>/test/test_config.json`):
```powershell
$env:PYTHONHASHSEED=0; $env:PYTHONPATH="$PWD\src"
# python: walkforward.calc_hash(active_config["bot"]) == calc_hash(bt_window["bot"])
```

### 5. Stop the remote bot
```powershell
docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager `
  python src/tools/wfo_vps_manager.py stop-live
```

### 6. Upload the new config to the VPS (`--force` = upload even if period label unchanged)
```powershell
docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager `
  python src/tools/wfo_vps_manager.py upload-active --profile hype_t12 --force
```
Saves a remote + local rollback backup (`backups/.../pre_wfo_update_*.tar.gz`) then
uploads. Confirms `Uploaded active WFO artifact: <period> / <new-hash>`.

### 7. Start the remote bot (loads the new config)
```powershell
docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager `
  python src/tools/wfo_vps_manager.py start-live
```

### 8. Point the persistent manager at the new profile
Edit **`docker-compose.wfo-local.yml`**, in the `command:` list change
`- hype_t8` → `- hype_t12`, then recreate the manager:
```powershell
docker compose -f docker-compose.wfo-local.yml up -d
```

### 9. Verify
```powershell
# local == remote on the NEW hash:
docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager `
  python src/tools/wfo_vps_manager.py status --profile hype_t12
# manager loop is on the new profile:
docker logs passivbot-wfo-local-manager --tail 15
# remote bot is Up (healthy):
docker compose -f docker-compose.wfo-local.yml run --rm passivbot-wfo-local-manager `
  python src/tools/wfo_vps_manager.py preflight-remote
```
Success looks like: `Local active` == `Remote active` (new hash); manager log shows
`Local WFO watch started for hype_t12` + `Remote already has … skipping upload`;
`passivbot-lighter-live  Up … (healthy)`. (`passivbot-hype-live` is a **separate**
strategy — leave it alone.)

---

## Shortcut (less control)

If you don't need the explicit remote stop/start, just do **step 8** (edit the compose
profile) then `docker compose -f docker-compose.wfo-local.yml up -d`. The watch loop
will publish + upload the new config on its first tick, and the VPS bot soft-adopts
within ~1 min. The controlled procedure above is preferred for a clean swap.

## Rollback

`upload-active` saves `backups/remote/pre_wfo_update_<ts>.tar.gz` (and one on the VPS).
To revert: switch the profile back (`hype_t8`) and repeat the procedure, or restore the
backup tarball on the VPS and `start-live`.

## Notes / gotchas

- All tX test windows are calendar-aligned to the 24th, so the live `period` label may
  be identical across profiles (e.g. `2026-05-24..2026-06-24`) even though the config
  differs — that's why `upload-active` needs `--force` on a same-period swap.
- `lighter.pem` must stay owner-only on Windows or host `ssh.exe` rejects it; running
  through the container (as above) sidesteps this.
- The 24th-of-month rollover (e.g. 2026-06-24, window N+1) is a genuine cache MISS and
  triggers a real local optimize on the manager's next tick — watch the first one.
