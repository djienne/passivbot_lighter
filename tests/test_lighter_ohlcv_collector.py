"""Tests for the Lighter OHLCV collector's automatic partial-day healing.

A fetch of the in-progress UTC day forward-fills the not-yet-existing tail of the
day with the last real close and zero base volume (padding). ``heal_partial_days``
must detect such a trailing padded run, drop the day file, and rewind the cursor so
the next backfill re-downloads it cleanly. Complete days must be left untouched.
"""
import os
import sys

import numpy as np
import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from tools import lighter_ohlcv_collector as col  # noqa: E402


def _make_day(date_str: str, real_minutes: int) -> np.ndarray:
    """Build a 1440-row daily array with ``real_minutes`` real bars then padding.

    Real bars carry varying OHLC and positive base volume; padded bars repeat the
    last real close with ``bv == 0`` and ``o == h == l == c`` (the forward-fill
    signature produced by ``build_daily_array``).
    """
    start = col.date_str_to_start_ms(date_str)
    arr = np.empty((col.CANDLES_PER_DAY,), dtype=col.CANDLE_DTYPE)
    arr["ts"] = start + np.arange(col.CANDLES_PER_DAY, dtype=np.int64) * col.MS_PER_MIN
    last_close = 100.0
    for i in range(col.CANDLES_PER_DAY):
        if i < real_minutes:
            price = 100.0 + i * 0.01
            arr["o"][i] = price
            arr["h"][i] = price + 0.5
            arr["l"][i] = price - 0.5
            arr["c"][i] = price + 0.1
            arr["bv"][i] = 10.0 + i
            last_close = price + 0.1
        else:
            arr["o"][i] = last_close
            arr["h"][i] = last_close
            arr["l"][i] = last_close
            arr["c"][i] = last_close
            arr["bv"][i] = 0.0
    return arr


@pytest.fixture
def hype_dir(tmp_path, monkeypatch):
    """Point the collector at a temp data dir and return the HYPE coin dir."""
    monkeypatch.setattr(col, "LIGHTER_DATA_DIR", tmp_path)
    return col.coin_dir("HYPE")


def _write_day(coin_dir, date_str, real_minutes):
    np.save(str(coin_dir / f"{date_str}.npy"), _make_day(date_str, real_minutes))


def test_trailing_padded_run_counts_padding():
    complete = _make_day("2025-03-01", col.CANDLES_PER_DAY)
    partial = _make_day("2025-03-02", 766)
    assert col._trailing_padded_run(complete) == 0
    assert col._trailing_padded_run(partial) == col.CANDLES_PER_DAY - 766


def test_heal_drops_partial_latest_day_and_rewinds_cursor(hype_dir):
    _write_day(hype_dir, "2025-03-01", col.CANDLES_PER_DAY)  # complete
    _write_day(hype_dir, "2025-03-02", 766)  # partial (latest)
    col.set_fetched_until("HYPE", col.date_str_to_start_ms("2025-03-02") + 766 * col.MS_PER_MIN)

    dropped = col.heal_partial_days("HYPE")

    assert dropped == 1
    assert not (hype_dir / "2025-03-02.npy").exists()  # partial dropped
    assert (hype_dir / "2025-03-01.npy").exists()  # complete kept
    # cursor rewound to the start of the dropped day so backfill re-fetches it whole
    assert col.get_fetched_until("HYPE") == col.date_str_to_start_ms("2025-03-02")


def test_heal_leaves_complete_latest_day_untouched(hype_dir):
    _write_day(hype_dir, "2025-03-01", col.CANDLES_PER_DAY)
    _write_day(hype_dir, "2025-03-02", col.CANDLES_PER_DAY)
    cursor = col.date_str_to_start_ms("2025-03-03")
    col.set_fetched_until("HYPE", cursor)

    dropped = col.heal_partial_days("HYPE")

    assert dropped == 0
    assert (hype_dir / "2025-03-01.npy").exists()
    assert (hype_dir / "2025-03-02.npy").exists()
    assert col.get_fetched_until("HYPE") == cursor  # cursor unchanged


def test_heal_drops_multiple_trailing_partial_days(hype_dir):
    _write_day(hype_dir, "2025-03-01", col.CANDLES_PER_DAY)  # complete
    _write_day(hype_dir, "2025-03-02", 500)  # partial
    _write_day(hype_dir, "2025-03-03", 200)  # partial (latest)

    dropped = col.heal_partial_days("HYPE")

    assert dropped == 2
    assert (hype_dir / "2025-03-01.npy").exists()
    assert not (hype_dir / "2025-03-02.npy").exists()
    assert not (hype_dir / "2025-03-03.npy").exists()
    # rewound to the earliest dropped day
    assert col.get_fetched_until("HYPE") == col.date_str_to_start_ms("2025-03-02")


def test_heal_tolerates_short_zero_volume_tail(hype_dir):
    # A few genuine zero-volume minutes at day end must NOT trigger a heal.
    real = col.CANDLES_PER_DAY - col.PARTIAL_DAY_TAIL_TOLERANCE_MIN
    _write_day(hype_dir, "2025-03-02", real)

    dropped = col.heal_partial_days("HYPE")

    assert dropped == 0
    assert (hype_dir / "2025-03-02.npy").exists()
