#!/usr/bin/env python3
"""Shared walk-forward live-rolling policy helpers.

These are pure, side-effect-free functions used by BOTH the live bot
(``src/passivbot.py``) and the backtest orchestrator (``src/walkforward.py``) so
the month-boundary behaviour is identical in simulation and in production:

- ``should_flatten``      the position-handoff rule at a month boundary
- ``current_live_window`` which rolling train/test window contains a given date

Keeping the logic here (one source of truth) guarantees backtest/live parity and
makes both rules trivially unit-testable without an exchange or the optimizer.
"""

from __future__ import annotations

from typing import Optional

# Reuse the exact window math the backtest orchestrator uses, so a live period
# and a backtested period with the same anchor agree to the day.
try:
    from tools.wfo_utils import Window, _advance, _parse_date
except Exception:  # pragma: no cover - fallback when imported as a package
    from src.tools.wfo_utils import Window, _advance, _parse_date  # type: ignore


# ---------------------------------------------------------------------------
# Boundary position-handoff rule (rule D)
# ---------------------------------------------------------------------------
def should_flatten(
    unrealized_pnl: float,
    total_wallet_equity: float,
    max_loss_frac: float = 0.05,
) -> bool:
    """Decide whether to close a position at a month boundary.

    Rule (confirmed with the user): close the position only if it is **in profit**,
    or if its **unrealized loss is small** relative to the account — i.e. the
    unrealized loss is less than ``max_loss_frac`` of total wallet equity. Larger
    losing positions are kept and inherited by the next period's config.

    - ``unrealized_pnl``       signed uPnL in quote currency (>0 = profit).
    - ``total_wallet_equity``  balance + summed uPnL, in quote currency.
    - ``max_loss_frac``        loss tolerance as a fraction of equity (default 5%).

    Returns ``True`` to flatten (close), ``False`` to keep.

    Edge cases: a non-positive equity (blown/empty account) yields no meaningful
    threshold, so a losing position is kept (conservative: defer to the new config).
    """
    if unrealized_pnl >= 0:
        return True
    if total_wallet_equity <= 0:
        return False
    return abs(unrealized_pnl) < max_loss_frac * float(total_wallet_equity)


# ---------------------------------------------------------------------------
# Backtest stateful carry between OOS segments
# ---------------------------------------------------------------------------
def advance_carry(end_state: dict, max_loss_frac: float = 0.05) -> dict:
    """Compute the next OOS segment's seed (balance + positions) from this segment's
    end-state, applying the same handoff rule (``should_flatten``) the live bot uses.

    ``end_state`` = ``{"equity": float, "positions": [{coin, side, size, entry, upnl}, ...]}``.
    Positions that should be flattened (in profit, or small unrealized loss) are realized
    into cash; the rest are carried forward as open positions for the next segment.

    Returns ``{"balance": float, "positions": {coin: {side: {"size", "price"}}}}`` shaped for
    ``backtest.initial_positions``. Equity-based accounting keeps the stitched curve continuous:
    next first-step equity = next_balance + Σ uPnL(kept) at ≈ the same price ≈ this segment's equity.
    """
    equity = float(end_state.get("equity", 0.0) or 0.0)
    positions = end_state.get("positions", []) or []
    kept = [
        p for p in positions
        if not should_flatten(float(p.get("upnl", 0.0) or 0.0), equity, max_loss_frac)
    ]
    next_balance = equity - sum(float(p.get("upnl", 0.0) or 0.0) for p in kept)
    next_positions: dict = {}
    for p in kept:
        size = float(p.get("size", 0.0) or 0.0)
        if size == 0.0:
            continue
        coin = p.get("coin")
        side = p.get("side")
        if coin is None or side not in ("long", "short"):
            continue
        next_positions.setdefault(coin, {})[side] = {
            "size": size,
            "price": float(p.get("entry", 0.0) or 0.0),
        }
    return {"balance": next_balance, "positions": next_positions}


# ---------------------------------------------------------------------------
# Live rolling state machine (pure decision)
# ---------------------------------------------------------------------------
def decide_rolling_state(
    loaded_period: Optional[str],
    published_period: Optional[str],
    today_test_start: Optional[str],
    published_test_start: Optional[str],
) -> str:
    """Decide the live bot's rolling state from on-disk facts (no side effects).

    - ``loaded_period``       the period whose strategy the bot is currently running.
    - ``published_period``    the latest period the scheduler has published (active.json).
    - ``today_test_start``    test_start of the window containing today (calendar).
    - ``published_test_start``test_start of the published period's window.

    Returns one of:
    - ``"ADOPT"``     a newer/different config has been published than the one running
                      → restart to adopt it.
    - ``"WIND_DOWN"`` the calendar has rolled into a newer period than the published one
                      → pause new entries / apply the handoff rule until the scheduler
                      publishes the new month.
    - ``"NORMAL"``    running the right config for the current period; trade normally.
    """
    if published_period and published_period != loaded_period:
        return "ADOPT"
    if today_test_start and published_test_start and today_test_start > published_test_start:
        return "WIND_DOWN"
    return "NORMAL"


# ---------------------------------------------------------------------------
# Current live window from a fixed anchor
# ---------------------------------------------------------------------------
def current_live_window(
    anchor_start: str,
    train_months: int,
    test_months: int,
    step_months: int,
    today: str,
    calendar_months: bool = True,
    max_iter: int = 100_000,
) -> Optional[Window]:
    """Return the rolling window whose OOS/live test period contains ``today``.

    The walk-forward periods tile forward from a **fixed anchor** (never "now"),
    so this is deterministic and matches ``generate_windows`` (same train/test/step
    math via :func:`tools.wfo_utils._advance`). The training window for the returned
    period is ``[train_start, train_end)`` and the live/test period the config is
    deployed for is ``[test_start = train_end, test_end)``.

    Returns ``None`` if ``today`` precedes the first test period (no live config
    exists yet — the caller should fall back to the initial config). If the periods
    have gaps (``step_months > test_months``) and ``today`` falls in a gap, the most
    recent past window is returned.
    """
    if train_months <= 0 or test_months <= 0 or step_months <= 0:
        raise ValueError("train_months, test_months and step_months must be positive")

    start = _parse_date(anchor_start)
    today_d = _parse_date(today)
    idx = 0
    train_start = start
    last: Optional[Window] = None
    for _ in range(max_iter):
        train_end = _advance(train_start, train_months, calendar_months)
        test_start = train_end
        test_end = _advance(test_start, test_months, calendar_months)
        if test_start > today_d:
            break  # all further periods start in the future
        window = Window(
            index=idx,
            train_start=train_start.isoformat(),
            train_end=train_end.isoformat(),
            test_start=test_start.isoformat(),
            test_end=test_end.isoformat(),
        )
        if test_start <= today_d < test_end:
            return window
        last = window  # today is past this period's end; remember as fallback
        idx += 1
        train_start = _advance(train_start, step_months, calendar_months)
    return last
