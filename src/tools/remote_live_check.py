#!/usr/bin/env python3
"""Read-only health check of the live VPS bot (passivbot-lighter-live).

Confirms (a) the live bot is up and actively trading the published WFO config and
(b) NOTHING training-related (optimize / walkforward / scheduler / passivbot-wfo
container, cron, systemd timer) is running on the underpowered VPS. Every command
is read-only; it reuses the manager's own SSH helpers so the lighter.pem perms and
remote-command quoting are handled correctly.

Run it inside the manager container (recommended — key + deploy target present):

    docker compose -f docker-compose.wfo-local.yml run --rm \
        passivbot-wfo-local-manager python src/tools/remote_live_check.py

Optional: --tail N  (default 120) lines of bot logs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]  # .../src
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.wfo_vps_manager import (  # noqa: E402
    DEFAULT_REMOTE_HOST,
    DEFAULT_REMOTE_PATH,
    DEFAULT_REMOTE_USER,
    DEFAULT_SSH_KEY,
    Remote,
    print_remote_output,
    run_ssh,
)


def section(remote: Remote, title: str, cmd: str) -> None:
    print(f"\n===== {title} =====")
    print(f"$ {cmd}")
    res = run_ssh(remote, cmd, capture=True)
    if res.stdout:
        print_remote_output(res.stdout)
    if res.stderr:
        print_remote_output(res.stderr, file=sys.stderr)
    print(f"[rc={res.returncode}]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tail", type=int, default=120, help="bot log lines (default 120)")
    parser.add_argument("--remote-user", default=DEFAULT_REMOTE_USER)
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST)
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH)
    parser.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    args = parser.parse_args()

    remote = Remote(
        user=args.remote_user,
        host=args.remote_host,
        path=args.remote_path,
        ssh_key=Path(args.ssh_key).resolve(),
    )

    # 1. Running containers (expect passivbot-lighter-live Up; NO *wfo* container).
    section(remote, "RUNNING CONTAINERS", "docker ps --format '{{.Names}}\\t{{.Status}}'")

    # 2. Live bot env: optimization must be hard-blocked, no scheduler.
    section(
        remote,
        "LIVE BOT ENV (optimization guard)",
        "docker inspect passivbot-lighter-live "
        "--format '{{range .Config.Env}}{{println .}}{{end}}' "
        "| grep -iE 'REMOTE_LIVE|OPTIMIZE|WFO|SCHEDUL' || echo '(no optimize/wfo/scheduler env)'",
    )

    # 3. No cron / no systemd timer kicking off training.
    section(remote, "CRON", "crontab -l 2>/dev/null || echo '(no crontab)'")
    section(
        remote,
        "SYSTEMD USER TIMERS",
        "systemctl --user list-timers --all 2>/dev/null || echo '(none)'",
    )

    # 4. Recent live trading activity.
    section(
        remote,
        f"LIVE BOT LOGS (last {args.tail} lines)",
        f"docker logs passivbot-lighter-live --tail {int(args.tail)}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
