#!/usr/bin/env python3
"""Read-only health sweep for the WFO live deployment.

Checks the whole chain in one shot, with NO side effects:

  * local published artifact     (runs/walkforward/live/active.json)
  * local WFO manager container  (passivbot-wfo-local-manager)
  * local dashboard container    (streaming_live_passibot-dashboard-1)
  * VPS live bot                 (passivbot-lighter-live: up/healthy + remote guard)
  * remote published artifact    (matches local period + chosen_hash)
  * VPS health heartbeat + last fill freshness
  * days until the next monthly rollover (the 24th)

Exit code 0 if nothing FAILed, 1 otherwise. Run:

    $env:PYTHONPATH = "C:\\Users\\david\\Desktop\\freqtrade\\passivbot_lighter\\src"
    python src/tools/wfo_status.py            # default profile hype_t8
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.wfo_vps_manager import (  # noqa: E402
    DEFAULT_PROFILE,
    DEFAULT_REMOTE_HOST,
    DEFAULT_REMOTE_PATH,
    DEFAULT_REMOTE_USER,
    DEFAULT_SSH_KEY,
    REPO_ROOT,
    Remote,
    active_dir_for,
    q,
    run_local,
    run_ssh,
)

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_RANK = {OK: 0, WARN: 1, FAIL: 2}
_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}

DASHBOARD_CONTAINER = "streaming_live_passibot-dashboard-1"
MANAGER_CONTAINER = "passivbot-wfo-local-manager"
LIVE_CONTAINER = "passivbot-lighter-live"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str) -> None:
        self.rows.append((level, name, detail))

    @property
    def worst(self) -> str:
        return max((lvl for lvl, _, _ in self.rows), key=lambda l: _RANK[l], default=OK)

    def render(self) -> str:
        width = max((len(n) for _, n, _ in self.rows), default=0)
        lines = [f"{_MARK[lvl]} {name.ljust(width)}  {detail}" for lvl, name, detail in self.rows]
        return "\n".join(lines)


def _local_container(name: str) -> tuple[bool, str]:
    res = run_local(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}"],
        capture=True,
    )
    status = (res.stdout or "").strip().splitlines()
    return (bool(status), status[0] if status else "not created")


def _next_24th(today: datetime) -> datetime:
    if today.day < 24:
        return today.replace(day=24, hour=0, minute=0, second=0, microsecond=0)
    month = today.month + 1
    year = today.year + (1 if month > 12 else 0)
    month = 1 if month > 12 else month
    return today.replace(year=year, month=month, day=24, hour=0, minute=0, second=0, microsecond=0)


def _age_str(epoch_s: float, now_s: float) -> str:
    delta = max(0.0, now_s - epoch_s)
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


# NOTE: each marker is preceded by a bare `echo` (newline) so a previous
# command whose output lacks a trailing newline (e.g. `cat active.json`) cannot
# glue the next marker onto its last line and break section parsing.
REMOTE_PROBE = (
    "echo @@LIVE@@; "
    f"docker ps -a --filter name=^{LIVE_CONTAINER}$ --format '{{{{.Status}}}}'; "
    "echo; echo @@GUARD@@; "
    f"docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {LIVE_CONTAINER} "
    "2>/dev/null | grep PASSIVBOT_REMOTE_LIVE || echo MISSING; "
    "echo; echo @@ACTIVE@@; "
    "cat runs/walkforward/live/active.json 2>/dev/null; "
    "echo; echo @@HEALTH@@; "
    "grep -aF '[health]' logs/passivbot_debug.log 2>/dev/null | tail -n 1; "
    "echo; echo @@PNLS@@; "
    "stat -c %Y caches/lighter/lighter_01_pnls.json 2>/dev/null || echo 0; "
    "echo; echo @@END@@"
)


def _parse_sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    key = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("@@") and line.endswith("@@"):
            if key is not None:
                out[key] = "\n".join(buf).strip()
            key = line.strip("@")
            buf = []
        elif key is not None:
            buf.append(line)
    if key is not None:
        out[key] = "\n".join(buf).strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only WFO deployment health sweep")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    ap.add_argument("--remote-user", default=DEFAULT_REMOTE_USER)
    ap.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST)
    ap.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH)
    ap.add_argument("--no-remote", action="store_true", help="skip SSH checks (local only)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    now_s = now.timestamp()
    rep = Report()

    print(f"WFO status sweep @ {now.strftime('%Y-%m-%d %H:%M:%SZ')}  profile={args.profile}\n")

    # ---- local published artifact ----------------------------------------
    local_active = None
    try:
        ap_path = active_dir_for(args.profile) / "active.json"
        local_active = json.loads(ap_path.read_text(encoding="utf-8"))
        period = local_active.get("period", "?")
        widx = local_active.get("window_index", "?")
        gen_age = _age_str(local_active.get("generated_ts", 0) / 1000.0, now_s)
        rep.add(OK, "local active.json", f"period {period} (window {widx}), published {gen_age}")
    except Exception as exc:  # noqa: BLE001
        rep.add(FAIL, "local active.json", f"unreadable: {exc}")

    # ---- local containers ------------------------------------------------
    mgr_up, mgr_status = _local_container(MANAGER_CONTAINER)
    if mgr_up and mgr_status.lower().startswith("up"):
        rep.add(OK, "local WFO manager", mgr_status)
    elif mgr_up:
        rep.add(FAIL, "local WFO manager", f"not running: {mgr_status}")
    else:
        rep.add(FAIL, "local WFO manager", "container not created (24th rollover will be MISSED)")

    if mgr_up:
        logs = run_local(["docker", "logs", "--tail", "40", MANAGER_CONTAINER], capture=True)
        blob = (logs.stdout or "") + (logs.stderr or "")
        bad = [
            ln for ln in blob.splitlines()
            if any(t in ln for t in ("Traceback", "ModuleNotFoundError", "Error", "ERROR", "rc="))
            and "rc=0" not in ln
        ]
        if bad:
            rep.add(WARN, "manager log tail", f"{len(bad)} error-ish line(s); last: {bad[-1][:120]}")
        else:
            rep.add(OK, "manager log tail", "no errors in last 40 lines")

    dash_up, dash_status = _local_container(DASHBOARD_CONTAINER)
    rep.add(OK if (dash_up and dash_status.lower().startswith("up")) else WARN,
            "dashboard container", dash_status)

    # ---- next rollover ---------------------------------------------------
    nxt = _next_24th(now)
    days = (nxt - now).total_seconds() / 86400.0
    rep.add(OK, "next rollover", f"{nxt.strftime('%Y-%m-%d')} (in {days:.1f} days)")

    # ---- remote (VPS) ----------------------------------------------------
    if not args.no_remote:
        remote = Remote(
            user=args.remote_user,
            host=args.remote_host,
            path=args.remote_path,
            ssh_key=(REPO_ROOT / args.ssh_key).resolve()
            if not Path(args.ssh_key).is_absolute() else Path(args.ssh_key),
        )
        cmd = f"cd {q(remote.path)} && {REMOTE_PROBE}"
        try:
            res = run_ssh(remote, cmd, capture=True)
        except Exception as exc:  # noqa: BLE001
            rep.add(FAIL, "VPS ssh", f"connection failed: {exc}")
            res = None

        if res is not None and "@@END@@" not in (res.stdout or ""):
            rep.add(FAIL, "VPS ssh", f"probe incomplete (rc={res.returncode}): {res.stderr.strip()[:120]}")
        elif res is not None:
            sec = _parse_sections(res.stdout)

            live_status = sec.get("LIVE", "").strip() or "not created"
            if live_status.lower().startswith("up") and "healthy" in live_status.lower():
                rep.add(OK, "VPS live bot", live_status)
            elif live_status.lower().startswith("up"):
                rep.add(WARN, "VPS live bot", live_status)
            else:
                rep.add(FAIL, "VPS live bot", live_status)

            guard = sec.get("GUARD", "")
            if "PASSIVBOT_REMOTE_LIVE=1" in guard:
                rep.add(OK, "remote opt guard", "PASSIVBOT_REMOTE_LIVE=1 (optimizer disabled on VPS)")
            else:
                rep.add(FAIL, "remote opt guard", f"guard not set ({guard or 'MISSING'})")

            try:
                ractive = json.loads(sec.get("ACTIVE", "") or "{}")
            except json.JSONDecodeError:
                ractive = {}
            if ractive and local_active:
                same_period = ractive.get("period") == local_active.get("period")
                same_hash = ractive.get("chosen_hash") == local_active.get("chosen_hash")
                if same_period and same_hash:
                    rep.add(OK, "remote == local config", f"period {ractive.get('period')}")
                else:
                    rep.add(WARN, "remote == local config",
                            f"remote {ractive.get('period')}/{str(ractive.get('chosen_hash'))[:8]} "
                            f"vs local {local_active.get('period')}/{str(local_active.get('chosen_hash'))[:8]}")
            elif not ractive:
                rep.add(FAIL, "remote active.json", "missing/unreadable on VPS")

            health = sec.get("HEALTH", "").strip()
            rep.add(OK if health else WARN, "VPS health line",
                    health[:120] if health else "no [health] line found (debug log may be verbose)")

            try:
                pnls_mtime = float(sec.get("PNLS", "0") or "0")
            except ValueError:
                pnls_mtime = 0.0
            if pnls_mtime > 0:
                rep.add(OK, "last fill (pnls)", _age_str(pnls_mtime, now_s))
            else:
                rep.add(WARN, "last fill (pnls)", "pnls file missing or empty")

    print(rep.render())
    worst = rep.worst
    print(f"\nOVERALL: {worst}")
    return 0 if worst != FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
