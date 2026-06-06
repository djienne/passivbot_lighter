"""Runtime safety guards shared by operational entrypoints."""

from __future__ import annotations

import os
import sys


REMOTE_LIVE_ENV = "PASSIVBOT_REMOTE_LIVE"


def abort_if_remote_live(entrypoint: str) -> None:
    """Refuse optimizer/scheduler entrypoints inside the remote live container."""
    value = os.environ.get(REMOTE_LIVE_ENV, "").strip().lower()
    if value not in {"1", "true", "yes", "on"}:
        return
    print(
        f"{entrypoint} is disabled when {REMOTE_LIVE_ENV}=1; "
        "run walk-forward optimization locally and upload completed artifacts instead.",
        file=sys.stderr,
    )
    raise SystemExit(2)
