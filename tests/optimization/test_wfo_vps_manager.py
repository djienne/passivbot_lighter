import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import wfo_vps_manager as mgr


def _write_active(active: Path, *, history: bool = True) -> None:
    active.mkdir(parents=True, exist_ok=True)
    pointer = {
        "period": "2026-05-24..2026-06-24",
        "window_index": 7,
        "chosen_hash": "abc123",
        "train_window": {"train_end": "2026-05-24"},
    }
    (active / "active.json").write_text(json.dumps(pointer), encoding="utf-8")
    (active / "active_config.json").write_text(
        json.dumps({"bot": {"long": {}, "short": {}}}),
        encoding="utf-8",
    )
    if history:
        hist = active / "configs_history"
        hist.mkdir()
        (hist / "window_07_2026-05-24.json").write_text(
            json.dumps({"bot": {"long": {}, "short": {}}}),
            encoding="utf-8",
        )


def _profile(tmp_path: Path, active: Path) -> Path:
    p = tmp_path / "profile.json"
    p.write_text(
        json.dumps({
            "base_config": "configs/wfo_hype.json",
            "initial_config": "configs/config_hype.json",
            "train_months": 8,
            "live_rolling": {"active_dir": str(active)},
        }),
        encoding="utf-8",
    )
    return p


def test_shipped_profiles_resolve_expected_train_months():
    _, wf8, _ = mgr.load_profile("hype_t8")
    _, wf12, _ = mgr.load_profile("hype_t12")
    assert wf8["train_months"] == 8
    assert wf12["train_months"] == 12


def test_validate_local_active_requires_history_file(tmp_path):
    active = tmp_path / "live"
    profile = _profile(tmp_path, active)
    _write_active(active, history=False)

    with pytest.raises(FileNotFoundError, match="history config"):
        mgr.validate_local_active(str(profile))


def test_validate_local_active_accepts_complete_artifact(tmp_path):
    active = tmp_path / "live"
    profile = _profile(tmp_path, active)
    _write_active(active)

    active_dir, pointer = mgr.validate_local_active(str(profile))
    assert active_dir == active.resolve()
    assert pointer["period"] == "2026-05-24..2026-06-24"


def test_remote_command_guard_blocks_optimizer_entrypoints():
    assert mgr.is_forbidden_remote_command("python src/optimize.py configs/x.json")
    assert mgr.is_forbidden_remote_command("python src/wfo_scheduler.py --meta configs/wfo_meta.json")
    assert mgr.is_forbidden_remote_command("docker compose --profile wfo up -d")
    assert not mgr.is_forbidden_remote_command("grep -RInE 'optimize\\.py|wfo_scheduler\\.py' .")
    assert not mgr.is_forbidden_remote_command("docker compose config --services")


def test_upload_active_backs_up_then_publishes_pointer_last(tmp_path, monkeypatch):
    active = tmp_path / "live"
    profile = _profile(tmp_path, active)
    _write_active(active)
    remote = mgr.Remote("ubuntu", "example.test", "/srv/passivbot", tmp_path / "key.pem")
    calls = []

    monkeypatch.setattr(mgr, "remote_active", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "backup_remote", lambda *a, **k: calls.append(("backup",)))

    def fake_ssh(_remote, command, **_kwargs):
        calls.append(("ssh", command))
        return mgr.CmdResult(0)

    def fake_scp(_remote, source, dest, **_kwargs):
        calls.append(("scp", str(source), dest))
        return mgr.CmdResult(0)

    monkeypatch.setattr(mgr, "run_ssh", fake_ssh)
    monkeypatch.setattr(mgr, "run_scp", fake_scp)

    assert mgr.upload_active(str(profile), remote) == 0
    assert calls[0] == ("backup",)
    publish_cmd = [c[1] for c in calls if c[0] == "ssh"][-1]
    assert publish_cmd.index("active_config.json") < publish_cmd.index("active.json")


def _write_run_dir(run: Path, *, complete: bool = True, test_end: str = "2026-06-24") -> None:
    run.mkdir(parents=True)
    windows = {
        "span": {"start_date": "2025-02-24"},
        "windows": [
            {
                "index": 7,
                "train_start": "2025-09-24",
                "train_end": "2026-05-24",
                "test_start": "2026-05-24",
                "test_end": test_end,
            }
        ],
    }
    (run / "windows.json").write_text(json.dumps(windows), encoding="utf-8")
    if not complete:
        return
    wdir = run / "window_07"
    wdir.mkdir()
    (wdir / "train_best.json").write_text(
        json.dumps({"bot": {"long": {"x": 1}, "short": {}}}),
        encoding="utf-8",
    )
    (wdir / "window_summary.json").write_text(
        json.dumps({"chosen_hash": "hash7", "cache_key": "cache7"}),
        encoding="utf-8",
    )
    hist = run / "configs_history"
    hist.mkdir()
    (hist / "window_07_2026-05-24.json").write_text(
        json.dumps({"bot": {"long": {"x": 1}, "short": {}}}),
        encoding="utf-8",
    )


def test_publish_from_run_dir_refuses_incomplete_window(tmp_path):
    active = tmp_path / "live"
    profile = _profile(tmp_path, active)
    run = tmp_path / "run"
    _write_run_dir(run, complete=False)

    with pytest.raises(FileNotFoundError, match="incomplete"):
        mgr.publish_from_run_dir(str(profile), run, window_index=7)


def test_publish_from_run_dir_writes_active_artifacts(tmp_path):
    active = tmp_path / "live"
    profile = _profile(tmp_path, active)
    run = tmp_path / "run"
    _write_run_dir(run)

    active_dir, pointer = mgr.publish_from_run_dir(str(profile), run, window_index=7)

    assert active_dir == active.resolve()
    assert pointer["period"] == "2026-05-24..2026-06-24"
    assert pointer["chosen_hash"] == "hash7"
    assert (active / "active.json").exists()
    assert (active / "active_config.json").exists()
    assert (active / "configs_history" / "window_07_2026-05-24.json").exists()


def test_publish_from_truncated_run_uses_full_live_slot(tmp_path):
    active = tmp_path / "live"
    profile = _profile(tmp_path, active)
    run = tmp_path / "run"
    _write_run_dir(run, test_end="2026-06-04")

    _, pointer = mgr.publish_from_run_dir(str(profile), run, window_index=7)

    assert pointer["train_window"]["test_end"] == "2026-06-04"
    assert pointer["period"] == "2026-05-24..2026-06-24"


@pytest.mark.parametrize("entrypoint", ["src/optimize.py", "src/walkforward.py", "src/wfo_scheduler.py"])
def test_remote_live_env_guard_blocks_optimizer_family(entrypoint):
    env = os.environ.copy()
    env["PASSIVBOT_REMOTE_LIVE"] = "1"
    proc = subprocess.run(
        [sys.executable, entrypoint, "--help"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert proc.returncode == 2
    assert "PASSIVBOT_REMOTE_LIVE=1" in proc.stderr
