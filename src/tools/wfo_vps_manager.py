#!/usr/bin/env python3
"""Local walk-forward operator for the Lighter VPS.

This tool is intentionally local-first: monthly WFO optimization happens on the
operator machine, and the VPS only receives completed live artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

SRC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from tools.wfo_meta import load_wf_meta  # noqa: E402
from tools.wfo_utils import _advance, _parse_date  # noqa: E402


DEFAULT_PROFILE = "hype_t8"


def _resolve_deploy_target() -> dict:
    """Resolve the VPS connection target without hard-coding it in tracked code.

    Precedence: environment variables (``PASSIVBOT_REMOTE_{HOST,USER,PATH}``) >
    a gitignored ``deploy_target.json`` at the repo root > safe non-secret
    fallbacks. The real host therefore lives only in the gitignored file (or the
    environment), never in version control. The ``--remote-host`` / ``--remote-user``
    / ``--remote-path`` CLI flags still override these defaults.
    """
    target = {
        "remote_host": "",
        "remote_user": "ubuntu",
        "remote_path": "/home/ubuntu/passivbot_lighter",
    }
    cfg_path = REPO_ROOT / "deploy_target.json"
    try:
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            for key in target:
                if data.get(key):
                    target[key] = data[key]
    except (OSError, ValueError):
        pass
    for key, env in (
        ("remote_host", "PASSIVBOT_REMOTE_HOST"),
        ("remote_user", "PASSIVBOT_REMOTE_USER"),
        ("remote_path", "PASSIVBOT_REMOTE_PATH"),
    ):
        val = os.environ.get(env)
        if val:
            target[key] = val
    return target


_DEPLOY_TARGET = _resolve_deploy_target()
DEFAULT_REMOTE_USER = _DEPLOY_TARGET["remote_user"]
DEFAULT_REMOTE_HOST = _DEPLOY_TARGET["remote_host"]
DEFAULT_REMOTE_PATH = _DEPLOY_TARGET["remote_path"]
DEFAULT_SSH_KEY = "lighter.pem"

FORBIDDEN_REMOTE_PATTERNS = (
    re.compile(r"\bpython(?:3)?\b[^;&|]*(?:src/)?(?:optimize|walkforward|wfo_scheduler)\.py\b"),
    re.compile(r"\bdocker\b[^;&|]*(?:passivbot-wfo|wfo_scheduler|wfo-scheduler)"),
    re.compile(r"\bdocker\s+compose\b[^;&|]*(?:--profile\s+wfo|passivbot-wfo)"),
)

MINIMAL_DEPLOY_FILES = (
    "docker-compose.yml",
    "Dockerfile_live",
    "requirements-live.txt",
    "requirements-rust.txt",
    "broker_codes.hjson",
    "configs/config_hype.json",
    "configs/wfo_hype.json",
    "configs/wfo_meta.json",
    "configs/wfo_profiles/hype_t6.json",
    "configs/wfo_profiles/hype_t8.json",
    "configs/wfo_profiles/hype_t10.json",
    "configs/wfo_profiles/hype_t12.json",
    "src/main.py",
    "src/passivbot.py",
    "src/config_utils.py",
    "src/optimize.py",
    "src/walkforward.py",
    "src/wfo_scheduler.py",
    "src/tools/runtime_guards.py",
    "src/tools/wfo_handoff.py",
    "src/tools/wfo_meta.py",
    "src/tools/wfo_utils.py",
    "src/tools/wfo_vps_manager.py",
)


@dataclass(frozen=True)
class Remote:
    user: str
    host: str
    path: str
    ssh_key: Path

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def q(value: str | Path) -> str:
    return shlex.quote(str(value))


def resolve_profile(profile: str) -> Path:
    path = Path(profile)
    if path.exists():
        return path.resolve()
    name = profile if profile.endswith(".json") else f"{profile}.json"
    path = REPO_ROOT / "configs" / "wfo_profiles" / name
    if not path.exists():
        raise FileNotFoundError(f"WFO profile not found: {profile}")
    return path.resolve()


def load_profile(profile: str) -> tuple[Path, dict[str, Any], str]:
    profile_path = resolve_profile(profile)
    wf, base_config_path = load_wf_meta(str(profile_path))
    return profile_path, wf, base_config_path


def active_dir_for(profile: str) -> Path:
    _, wf, _ = load_profile(profile)
    active = Path(wf.get("live_rolling", {}).get("active_dir", "runs/walkforward/live"))
    if not active.is_absolute():
        active = REPO_ROOT / active
    return active.resolve()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return data


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def validate_local_active(profile: str) -> tuple[Path, dict[str, Any]]:
    active_dir = active_dir_for(profile)
    pointer_path = active_dir / "active.json"
    config_path = active_dir / "active_config.json"
    if not pointer_path.exists():
        raise FileNotFoundError(f"missing local active pointer: {pointer_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"missing local active config: {config_path}")

    pointer = read_json(pointer_path)
    config = read_json(config_path)
    if not pointer.get("period") or pointer.get("window_index") is None:
        raise ValueError(f"{pointer_path} is missing period/window_index")
    if not pointer.get("chosen_hash"):
        raise ValueError(f"{pointer_path} is missing chosen_hash")
    train_window = pointer.get("train_window") or {}
    train_end = train_window.get("train_end")
    if not train_end:
        raise ValueError(f"{pointer_path} is missing train_window.train_end")
    if not isinstance(config.get("bot"), dict):
        raise ValueError(f"{config_path} is missing a bot section")

    hist = active_dir / "configs_history" / f"window_{int(pointer['window_index']):02d}_{train_end}.json"
    if not hist.exists():
        raise FileNotFoundError(f"missing local active history config: {hist}")
    return active_dir, pointer


def _select_run_window(windows: list[dict[str, Any]], window_index: Optional[int]) -> dict[str, Any]:
    if window_index is not None:
        for window in windows:
            if int(window.get("index", -1)) == int(window_index):
                return window
        raise FileNotFoundError(f"window index {window_index} not found in run")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    eligible = [
        window for window in windows
        if str(window.get("test_start", "")) <= today
    ]
    if not eligible:
        raise FileNotFoundError(f"no run window is eligible for today={today}")
    containing = [
        window for window in eligible
        if str(window.get("test_start", "")) <= today < str(window.get("test_end", "9999-99-99"))
    ]
    return (containing or eligible)[-1]


def live_period_end(test_start: str, wf: dict[str, Any]) -> str:
    """Return the intended live slot end for a published window.

    Historical WFO runs may clamp the final OOS ``test_end`` to the available data
    cutoff. Live deployment should still keep the chosen config active until the
    next rolling boundary, e.g. 2026-05-24..2026-06-24.
    """
    return _advance(
        _parse_date(test_start),
        int(wf["test_months"]),
        bool(wf["calendar_months"]),
    ).isoformat()


def publish_from_run_dir(
    profile: str,
    run_dir: str | Path,
    *,
    window_index: Optional[int] = None,
) -> tuple[Path, dict[str, Any]]:
    profile_path, wf, _ = load_profile(profile)
    run = Path(run_dir)
    if not run.is_absolute():
        run = (REPO_ROOT / run).resolve()
    windows_payload = read_json(run / "windows.json")
    windows = windows_payload.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError(f"{run / 'windows.json'} is missing windows")

    window = _select_run_window(windows, window_index)
    idx = int(window["index"])
    train_end = str(window["train_end"])
    wdir = run / f"window_{idx:02d}"
    config_path = wdir / "train_best.json"
    summary_path = wdir / "window_summary.json"
    history_path = run / "configs_history" / f"window_{idx:02d}_{train_end}.json"
    for required in (config_path, summary_path, history_path):
        if not required.exists():
            raise FileNotFoundError(f"run window is incomplete; missing {required}")
    config = read_json(config_path)
    summary = read_json(summary_path)
    chosen_hash = summary.get("chosen_hash")
    if not chosen_hash:
        raise ValueError(f"{summary_path} is missing chosen_hash")

    active_dir = active_dir_for(profile)
    period_end = live_period_end(str(window["test_start"]), wf)
    pointer = {
        "period": f"{window['test_start']}..{period_end}",
        "window_index": idx,
        "train_window": window,
        "config_path": "active_config.json",
        "cache_key": summary.get("cache_key"),
        "chosen_hash": chosen_hash,
        "anchor_start": windows_payload.get("span", {}).get("start_date") or windows[0]["train_start"],
        "generated_ts": int(time.time() * 1000),
        "params": {
            "profile": str(profile_path.relative_to(REPO_ROOT)) if profile_path.is_relative_to(REPO_ROOT) else str(profile_path),
            "train_months": int(wf["train_months"]),
            "test_months": int(wf["test_months"]),
            "step_months": int(wf["step_months"]),
            "base_seed": int(wf["base_seed"]),
            "calendar_months": bool(wf["calendar_months"]),
            "proximity_weight": float(wf["proximity_weight"]),
            "max_loss_flatten_frac": float(wf["max_loss_flatten_frac"]),
        },
    }
    cfg_text = json.dumps(config, indent=2, sort_keys=True)
    atomic_write(active_dir / "active_config.json", cfg_text)
    atomic_write(active_dir / "configs_history" / history_path.name, cfg_text)
    atomic_write(active_dir / "active.json", json.dumps(pointer, indent=2, sort_keys=True))
    atomic_write(
        active_dir / "scheduler_state.json",
        json.dumps({
            "last_published_period": pointer["period"],
            "last_window_index": idx,
            "last_cache_key": summary.get("cache_key"),
            "last_tick_ts": int(time.time() * 1000),
            "source_run_dir": str(run),
        }, indent=2, sort_keys=True),
    )
    return active_dir, pointer


def is_forbidden_remote_command(command: str) -> bool:
    return any(pattern.search(command) for pattern in FORBIDDEN_REMOTE_PATTERNS)


def run_local(argv: list[str], *, dry_run: bool = False, capture: bool = False) -> CmdResult:
    if dry_run:
        print("[dry-run]", " ".join(argv))
        return CmdResult(0)
    proc = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=capture)
    return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")


_SECURE_KEY_CACHE: dict[str, str] = {}


def secure_ssh_key(key: str | Path) -> str:
    """Return a path to the private key with permissions OpenSSH will accept.

    The repo is bind-mounted into the local WFO manager container from a Windows
    host, so ``lighter.pem`` surfaces inside the Linux container as ``0777`` and
    OpenSSH refuses it ("UNPROTECTED PRIVATE KEY FILE ... key will be ignored").
    ``chmod`` on a bind-mounted file is ignored, so when we can't tighten the key
    in place we copy it once to a private ``0600`` file and reuse that for the
    process lifetime. On Windows (host conda runs) ssh.exe doesn't enforce POSIX
    perms, so the key is returned unchanged.
    """
    key = str(key)
    cached = _SECURE_KEY_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    if os.name == "nt":
        return key
    try:
        mode = os.stat(key).st_mode & 0o777
    except OSError:
        return key  # let ssh surface the real "no such file" error
    if mode & 0o077 == 0:
        return key  # already private enough (<= 0700)
    # Too open (bind-mounted Windows key surfaces as 0777). NEVER chmod the
    # original: on a Docker Desktop bind mount a chmod can propagate a mangled
    # ACL back to the host file and break host-side ssh.exe. Instead copy it once
    # to a private 0600 file in the container's own filesystem and reuse that.
    try:
        fd, tmp = tempfile.mkstemp(prefix="wfo_key_", suffix=".pem")
        with os.fdopen(fd, "wb") as out, open(key, "rb") as src:
            out.write(src.read())
        os.chmod(tmp, 0o600)
    except OSError:
        return key
    _SECURE_KEY_CACHE[key] = tmp
    return tmp


def run_ssh(remote: Remote, command: str, *, dry_run: bool = False, capture: bool = False) -> CmdResult:
    if is_forbidden_remote_command(command):
        raise ValueError(f"refusing unsafe remote optimizer/scheduler command: {command}")
    argv = [
        "ssh",
        "-i",
        secure_ssh_key(remote.ssh_key),
        "-o",
        "StrictHostKeyChecking=no",
        remote.target,
        command,
    ]
    return run_local(argv, dry_run=dry_run, capture=capture)


def print_remote_output(text: str, *, file=None) -> None:
    """Print remote UTF-8 output on Windows consoles with narrow encodings."""
    file = file or sys.stdout
    try:
        print(text.rstrip(), file=file)
    except UnicodeEncodeError:
        safe = text.rstrip().encode(getattr(file, "encoding", None) or "utf-8", errors="replace").decode(
            getattr(file, "encoding", None) or "utf-8",
            errors="replace",
        )
        print(safe, file=file)


def run_scp(
    remote: Remote,
    source: str | Path,
    dest: str,
    *,
    dry_run: bool = False,
    download: bool = False,
) -> CmdResult:
    key = secure_ssh_key(remote.ssh_key)
    if download:
        argv = ["scp", "-i", key, "-o", "StrictHostKeyChecking=no", f"{remote.target}:{source}", dest]
    else:
        argv = ["scp", "-i", key, "-o", "StrictHostKeyChecking=no", str(source), f"{remote.target}:{dest}"]
    return run_local(argv, dry_run=dry_run)


def remote_active(remote: Remote, *, dry_run: bool = False) -> Optional[dict[str, Any]]:
    cmd = f"cd {q(remote.path)} && test -f runs/walkforward/live/active.json && cat runs/walkforward/live/active.json"
    res = run_ssh(remote, cmd, dry_run=dry_run, capture=True)
    if dry_run or res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        data = json.loads(res.stdout)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def backup_remote(remote: Remote, *, dry_run: bool = False) -> Optional[Path]:
    stamp = utc_stamp()
    remote_backup = f"backups/pre_wfo_update_{stamp}.tar.gz"
    script = f"""
set -eu
cd {q(remote.path)}
mkdir -p backups
items=""
for p in caches/lighter/*_pnls.json caches/fill_events logs bot.log monitor.log *.log runs/walkforward/live configs docker-compose.yml; do
  if [ -e "$p" ]; then items="$items $p"; fi
done
if [ -n "$items" ]; then
  tar -czf {q(remote_backup)} $items
else
  tar -czf {q(remote_backup)} --files-from /dev/null
fi
printf '%s\\n' {q(remote_backup)}
""".strip()
    res = run_ssh(remote, script, dry_run=dry_run, capture=True)
    if dry_run:
        return None
    if res.returncode != 0:
        raise RuntimeError(f"remote backup failed: {res.stderr.strip()}")

    local_dir = REPO_ROOT / "backups" / "remote"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / Path(remote_backup).name
    scp_res = run_scp(remote, f"{remote.path}/{remote_backup}", str(local_path), download=True)
    if scp_res.returncode != 0:
        raise RuntimeError("failed to download remote backup")
    print(f"Remote runtime backup saved: {remote.path}/{remote_backup}")
    print(f"Local backup copy saved: {local_path}")
    return local_path


def upload_active(profile: str, remote: Remote, *, dry_run: bool = False, force: bool = False) -> int:
    active_dir, pointer = validate_local_active(profile)
    current = remote_active(remote, dry_run=dry_run)
    if current and not force:
        same_period = current.get("period") == pointer.get("period")
        same_hash = current.get("chosen_hash") == pointer.get("chosen_hash")
        if same_period and same_hash:
            print(f"Remote already has {pointer.get('period')} / {pointer.get('chosen_hash')}; skipping upload.")
            return 0

    backup_remote(remote, dry_run=dry_run)
    remote_tmp = f"/tmp/wfo_active_{utc_stamp()}"
    files = {
        active_dir / "active_config.json": f"{remote_tmp}/active_config.json",
        active_dir / "active.json": f"{remote_tmp}/active.json",
    }
    train_end = pointer["train_window"]["train_end"]
    hist_name = f"window_{int(pointer['window_index']):02d}_{train_end}.json"
    files[active_dir / "configs_history" / hist_name] = f"{remote_tmp}/configs_history/{hist_name}"

    mkdir_res = run_ssh(remote, f"mkdir -p {q(remote_tmp)}/configs_history", dry_run=dry_run)
    if mkdir_res.returncode != 0:
        return mkdir_res.returncode
    for src, dst in files.items():
        scp_res = run_scp(remote, src, dst, dry_run=dry_run)
        if scp_res.returncode != 0:
            return scp_res.returncode

    publish = f"""
set -eu
cd {q(remote.path)}
mkdir -p runs/walkforward/live/configs_history
mv {q(remote_tmp)}/active_config.json runs/walkforward/live/active_config.json
mv {q(remote_tmp)}/configs_history/{q(hist_name)} runs/walkforward/live/configs_history/{q(hist_name)}
mv {q(remote_tmp)}/active.json runs/walkforward/live/active.json
rmdir {q(remote_tmp)}/configs_history
rmdir {q(remote_tmp)}
""".strip()
    res = run_ssh(remote, publish, dry_run=dry_run)
    if res.returncode != 0:
        return res.returncode
    print(f"Uploaded active WFO artifact: {pointer.get('period')} / {pointer.get('chosen_hash')}")
    return 0


def create_delta_archive(files: Iterable[str]) -> Path:
    tmp = Path(tempfile.gettempdir()) / f"passivbot_lighter_delta_{utc_stamp()}.tar.gz"
    with tarfile.open(tmp, "w:gz") as tar:
        for rel in files:
            path = REPO_ROOT / rel
            if not path.exists():
                raise FileNotFoundError(f"deploy file missing: {rel}")
            tar.add(path, arcname=rel)
    return tmp


def deploy_code(remote: Remote, *, dry_run: bool = False) -> int:
    backup_remote(remote, dry_run=dry_run)
    archive = create_delta_archive(MINIMAL_DEPLOY_FILES)
    remote_archive = f"/tmp/{archive.name}"
    try:
        scp_res = run_scp(remote, archive, remote_archive, dry_run=dry_run)
        if scp_res.returncode != 0:
            return scp_res.returncode
        cmd = f"cd {q(remote.path)} && tar -xzf {q(remote_archive)} && rm -f {q(remote_archive)}"
        res = run_ssh(remote, cmd, dry_run=dry_run)
        return res.returncode
    finally:
        if archive.exists():
            archive.unlink()


def optimize_once(profile: str, *, dry_run: bool = False) -> int:
    profile_path = resolve_profile(profile)
    argv = [sys.executable, str(SRC_ROOT / "wfo_scheduler.py"), "--meta", str(profile_path), "--once"]
    res = run_local(argv, dry_run=dry_run)
    if res.returncode == 0 and not dry_run:
        validate_local_active(profile)
    return res.returncode


def watch(profile: str, remote: Remote, *, interval_minutes: float, dry_run: bool, no_upload: bool) -> int:
    interval_s = max(60.0, interval_minutes * 60.0)
    print(f"Local WFO watch started for {profile}; interval={interval_s:.0f}s; upload={not no_upload}")
    while True:
        rc = optimize_once(profile, dry_run=dry_run)
        if rc == 0 and not no_upload:
            try:
                upload_active(profile, remote, dry_run=dry_run)
            except Exception as exc:
                print(f"upload-active failed: {exc}", file=sys.stderr)
        elif rc != 0:
            print(f"optimize-once returned rc={rc}; retrying next interval", file=sys.stderr)
        if dry_run:
            return rc
        time.sleep(interval_s)


def preflight_remote(remote: Remote, *, dry_run: bool = False) -> int:
    checks = [
        "docker ps -a --format '{{.Names}} {{.Status}}'",
        f"cd {q(remote.path)} && docker compose config --services 2>/dev/null || true",
        "crontab -l 2>/dev/null || true",
        "systemctl --user list-timers --all 2>/dev/null || true",
        "systemctl --user list-units --all 2>/dev/null || true",
        (
            f"cd {q(remote.path)} && "
            "grep -RInE 'optimize\\.py|walkforward\\.py|wfo_scheduler\\.py|passivbot-wfo' "
            "docker-compose*.yml . 2>/dev/null || true"
        ),
    ]
    rc = 0
    for cmd in checks:
        res = run_ssh(remote, cmd, dry_run=dry_run, capture=True)
        rc = max(rc, res.returncode)
        if res.stdout:
            print_remote_output(res.stdout)
        if res.stderr:
            print_remote_output(res.stderr, file=sys.stderr)
    return rc


def status(profile: str, remote: Remote, *, dry_run: bool = False) -> int:
    print(f"Profile: {profile}")
    try:
        profile_path, wf, base = load_profile(profile)
        print(f"Profile path: {profile_path}")
        print(f"Base config: {base}")
        print(f"Train months: {wf['train_months']}")
        active_dir, pointer = validate_local_active(profile)
        print(f"Local active: {pointer.get('period')} / {pointer.get('chosen_hash')} ({active_dir})")
    except Exception as exc:
        print(f"Local active: not deployable ({exc})")

    current = remote_active(remote, dry_run=dry_run)
    if current:
        print(f"Remote active: {current.get('period')} / {current.get('chosen_hash')}")
    else:
        print("Remote active: unavailable or missing")
    return 0


def stop_live(remote: Remote, *, dry_run: bool = False) -> int:
    cmd = f"cd {q(remote.path)} && docker compose stop passivbot-lighter-live"
    return run_ssh(remote, cmd, dry_run=dry_run).returncode


def start_live(remote: Remote, *, dry_run: bool = False) -> int:
    cmd = f"cd {q(remote.path)} && docker compose up -d passivbot-lighter-live"
    return run_ssh(remote, cmd, dry_run=dry_run).returncode


def build_remote(args: argparse.Namespace) -> Remote:
    return Remote(
        user=args.remote_user,
        host=args.remote_host,
        path=args.remote_path,
        ssh_key=Path(args.ssh_key).resolve(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local WFO manager for the Lighter VPS")
    parser.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    parser.add_argument("--remote-user", default=DEFAULT_REMOTE_USER)
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST)
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH)
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("status", "optimize-once", "publish-local", "upload-active", "watch"):
        p = sub.add_parser(name)
        p.add_argument("--profile", default=DEFAULT_PROFILE)
        p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    for name in ("preflight-remote", "deploy-code", "stop-live", "start-live"):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)

    upload = sub.choices["upload-active"]
    upload.add_argument("--force", action="store_true")

    publish = sub.choices["publish-local"]
    publish.add_argument("--from-run-dir", default=None)
    publish.add_argument("--window-index", type=int, default=None)

    watch_p = sub.choices["watch"]
    watch_p.add_argument("--check-interval-minutes", type=float, default=15.0)
    watch_p.add_argument("--no-upload", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    remote = build_remote(args)
    try:
        if args.cmd == "status":
            return status(args.profile, remote, dry_run=args.dry_run)
        if args.cmd == "preflight-remote":
            return preflight_remote(remote, dry_run=args.dry_run)
        if args.cmd == "optimize-once":
            return optimize_once(args.profile, dry_run=args.dry_run)
        if args.cmd == "publish-local":
            if args.from_run_dir:
                active_dir, pointer = publish_from_run_dir(
                    args.profile,
                    args.from_run_dir,
                    window_index=args.window_index,
                )
            else:
                active_dir, pointer = validate_local_active(args.profile)
            print(
                "Local active is deployable: "
                f"{pointer.get('period')} / {pointer.get('chosen_hash')} ({active_dir})"
            )
            return 0
        if args.cmd == "upload-active":
            return upload_active(args.profile, remote, dry_run=args.dry_run, force=args.force)
        if args.cmd == "watch":
            return watch(
                args.profile,
                remote,
                interval_minutes=args.check_interval_minutes,
                dry_run=args.dry_run,
                no_upload=args.no_upload,
            )
        if args.cmd == "deploy-code":
            return deploy_code(remote, dry_run=args.dry_run)
        if args.cmd == "stop-live":
            return stop_live(remote, dry_run=args.dry_run)
        if args.cmd == "start-live":
            return start_live(remote, dry_run=args.dry_run)
        raise AssertionError(args.cmd)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"{args.cmd}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
