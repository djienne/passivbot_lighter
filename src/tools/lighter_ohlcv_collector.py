"""
Lighter DEX 1-minute OHLCV collector.

Fetches all available 1m candle history from Lighter and stores as daily .npy files
compatible with passivbot backtesting format:
  data/ohlcvs_lighter/{COIN}/YYYY-MM-DD.npy
  shape (1440, 6) = [timestamp_ms, open, high, low, close, volume]
"""

import asyncio
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("lighter-collector")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL", "https://mainnet.zklighter.elliot.ai")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
SYMBOLS = os.environ.get("SYMBOLS", "BTC,ETH,HYPE")  # comma-separated, empty = all
EARLIEST_DATE = os.environ.get("EARLIEST_DATE", "2025-01-15")  # Lighter mainnet data starts ~Jan 17, 2025
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
REQUEST_INTERVAL = float(os.environ.get("REQUEST_INTERVAL", "0.5"))  # seconds between requests

MS_PER_MIN = 60_000
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000
WINDOW_MS = 6 * MS_PER_HOUR  # 6 h = 360 min < 500 API limit
SKIP_WINDOW_MS = 3 * MS_PER_DAY  # jump 3 days when no data found (short enough to not overshoot new listings)
CANDLES_PER_DAY = 1440
MAX_RETRIES = 8
BACKOFF_BASE = 2.0
BACKOFF_CAP = 120.0

shutdown_event = asyncio.Event()
_save_locks: dict[str, threading.Lock] = {}


def _get_save_lock(coin: str) -> threading.Lock:
    if coin not in _save_locks:
        _save_locks[coin] = threading.Lock()
    return _save_locks[coin]


def _handle_signal(*_):
    log.info("Shutdown signal received")
    shutdown_event.set()


# ---------------------------------------------------------------------------
# Global rate limiter: semaphore + minimum interval between requests
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, max_concurrent: int, min_interval: float):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def acquire(self):
        await self._sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._last_request + self._interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def release(self):
        self._sem.release()


_rate_limiter: RateLimiter = None  # initialized in run()


# ---------------------------------------------------------------------------
# HTTP with rate limiting, exponential backoff, jitter
# ---------------------------------------------------------------------------
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict | None:
    backoff = BACKOFF_BASE
    for attempt in range(1, MAX_RETRIES + 1):
        await _rate_limiter.acquire()
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 429 or resp.status >= 500:
                    jitter = backoff * random.uniform(0, 0.25)
                    wait = min(backoff + jitter, BACKOFF_CAP)
                    log.warning(
                        f"HTTP {resp.status} (attempt {attempt}/{MAX_RETRIES}), "
                        f"retrying in {wait:.1f}s"
                    )
                    _rate_limiter.release()
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, BACKOFF_CAP)
                    continue

                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"HTTP {resp.status}: {body[:200]}")
                    _rate_limiter.release()
                    return None

                data = await resp.json()
                _rate_limiter.release()

                if isinstance(data, dict) and data.get("code") not in (200, None):
                    jitter = backoff * random.uniform(0, 0.25)
                    wait = min(backoff + jitter, BACKOFF_CAP)
                    log.warning(
                        f"API error {data.get('code')} (attempt {attempt}/{MAX_RETRIES}), "
                        f"retrying in {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    backoff = min(backoff * 2, BACKOFF_CAP)
                    continue
                return data

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            _rate_limiter.release()
            jitter = backoff * random.uniform(0, 0.25)
            wait = min(backoff + jitter, BACKOFF_CAP)
            log.warning(
                f"Request error (attempt {attempt}/{MAX_RETRIES}): {e}, "
                f"retrying in {wait:.1f}s"
            )
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, BACKOFF_CAP)

    log.error(f"All {MAX_RETRIES} retries exhausted for {url}")
    return None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
async def discover_markets(session: aiohttp.ClientSession) -> dict[int, str]:
    """Return {market_id: COIN} deduplicated by coin name (lowest market_id wins)."""
    data = await fetch_json(session, f"{BASE_URL}/api/v1/orderBooks", {})
    if not data:
        return {}
    order_books = data.get("order_books") or data.get("orderBooks") or []
    symbol_filter = (
        {s.strip().upper() for s in SYMBOLS.split(",") if s.strip()} if SYMBOLS else None
    )

    # Collect all (market_id, coin) pairs
    all_markets: list[tuple[int, str]] = []
    for ob in order_books:
        raw_id = ob.get("market_id")
        if raw_id is None:
            raw_id = ob.get("marketId")
        if raw_id is None:
            continue
        mid = int(raw_id)
        sym = (ob.get("symbol") or "").upper()
        if not sym:
            continue
        coin = sym.split("/")[0]
        if symbol_filter and coin not in symbol_filter:
            continue
        all_markets.append((mid, coin))

    # Deduplicate: keep lowest market_id per coin
    seen: dict[str, int] = {}
    for mid, coin in all_markets:
        if coin not in seen or mid < seen[coin]:
            seen[coin] = mid
    markets = {mid: coin for coin, mid in seen.items()}

    log.info(f"Discovered {len(markets)} markets: {', '.join(sorted(markets.values()))}")
    return markets


async def fetch_candles(
    session: aiohttp.ClientSession, market_id: int, start_ms: int, end_ms: int
) -> list[list]:
    """Fetch 1m candles in [start_ms, end_ms]. Returns list of [t,o,h,l,c,v]."""
    params = {
        "market_id": market_id,
        "resolution": "1m",
        "start_timestamp": int(start_ms),
        "end_timestamp": int(end_ms),
        "count_back": 500,
    }
    data = await fetch_json(session, f"{BASE_URL}/api/v1/candles", params)
    if not data or not isinstance(data, dict):
        return []
    raw = data.get("c") or []
    return [
        [int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]),
         float(c["c"]), float(c.get("v", 0) or 0)]
        for c in raw
    ]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def coin_dir(coin: str) -> Path:
    d = DATA_DIR / "ohlcvs_lighter" / coin
    d.mkdir(parents=True, exist_ok=True)
    return d


def day_file(coin: str, date_str: str) -> Path:
    return coin_dir(coin) / f"{date_str}.npy"


def ts_to_date_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def date_str_to_start_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_fetched_until(coin: str) -> int | None:
    """Read the fetch-progress cursor for a coin."""
    cursor = coin_dir(coin) / ".fetched_until"
    if cursor.exists():
        try:
            return int(cursor.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def set_fetched_until(coin: str, ts_ms: int):
    """Write the fetch-progress cursor for a coin."""
    cursor = coin_dir(coin) / ".fetched_until"
    cursor.write_text(str(ts_ms))


def find_resume_ts(coin: str) -> int | None:
    """Return timestamp to resume fetching from.

    Prefers the cursor file (exact progress). Falls back to inferring
    from .npy files for legacy data (re-fetches last day once, then
    cursor takes over).
    """
    cursor_ts = get_fetched_until(coin)
    if cursor_ts is not None:
        return cursor_ts
    d = coin_dir(coin)
    files = sorted(f for f in d.glob("*.npy") if ".tmp" not in f.name)
    if not files:
        return None
    return date_str_to_start_ms(files[-1].stem)


def cleanup_tmp_files(coin: str):
    for tmp in coin_dir(coin).glob("*.tmp.npy"):
        try:
            tmp.unlink()
            log.info(f"[{coin}] Cleaned stale temp: {tmp.name}")
        except OSError:
            pass


def build_daily_array(
    candles: list[list], date_str: str, existing: np.ndarray | None = None
) -> np.ndarray:
    """Build a complete 1440-row daily array, forward/backward filling gaps."""
    day_start_ms = date_str_to_start_ms(date_str)
    arr = np.full((CANDLES_PER_DAY, 6), np.nan, dtype=np.float64)

    # Timestamps
    arr[:, 0] = np.arange(CANDLES_PER_DAY, dtype=np.float64) * MS_PER_MIN + day_start_ms

    # Existing data
    if existing is not None and existing.shape == (CANDLES_PER_DAY, 6):
        mask = ~np.isnan(existing[:, 1])
        arr[mask, 1:] = existing[mask, 1:]

    # Overlay new candles (dedup: new data wins over existing)
    for c in candles:
        idx = (int(c[0]) - day_start_ms) // MS_PER_MIN
        if 0 <= idx < CANDLES_PER_DAY:
            arr[idx, 1:] = c[1:]

    # Forward-fill gaps
    last_close = np.nan
    for i in range(CANDLES_PER_DAY):
        if np.isnan(arr[i, 1]):
            if not np.isnan(last_close):
                arr[i, 1:5] = last_close
                arr[i, 5] = 0.0
        else:
            last_close = arr[i, 4]

    # Backward-fill leading NaN
    for i in range(CANDLES_PER_DAY):
        if not np.isnan(arr[i, 1]):
            if i > 0:
                price = arr[i, 1]
                arr[:i, 1:5] = price
                arr[:i, 5] = 0.0
            break

    return arr


def save_day(coin: str, date_str: str, candles: list[list]):
    """Atomically save/update a daily .npy file."""
    fpath = day_file(coin, date_str)
    lock = _get_save_lock(coin)

    with lock:
        existing = None
        if fpath.exists():
            try:
                existing = np.load(str(fpath))
            except Exception:
                existing = None

        arr = build_daily_array(candles, date_str, existing)

        if np.isnan(arr[:, 1]).all():
            return

        tmp = fpath.parent / f"{fpath.stem}.tmp.npy"
        np.save(str(tmp), arr)
        if fpath.exists():
            fpath.unlink()
        tmp.rename(fpath)


def group_candles_by_day(candles: list[list]) -> dict[str, list[list]]:
    by_day: dict[str, list[list]] = {}
    for c in candles:
        by_day.setdefault(ts_to_date_str(int(c[0])), []).append(c)
    return by_day


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------
async def backfill_market(session: aiohttp.ClientSession, market_id: int, coin: str):
    """Fetch all available history for one market.

    Uses adaptive window stepping: 6h when data exists, jumps 7 days forward
    after consecutive empty windows to skip pre-listing periods fast.
    """
    cleanup_tmp_files(coin)

    resume_ts = find_resume_ts(coin)
    if resume_ts:
        start_ms = resume_ts
        log.info(f"[{coin}] Resuming from {ts_to_date_str(resume_ts)}")
    else:
        start_ms = date_str_to_start_ms(EARLIEST_DATE)
        log.info(f"[{coin}] Starting backfill from {EARLIEST_DATE}")

    now_ms = int(time.time() * 1000)
    total_candles = 0
    empty_streak = 0
    window_start = start_ms
    last_log = time.time()

    while window_start < now_ms and not shutdown_event.is_set():
        # Adaptive window: skip faster when no data
        if empty_streak >= 4:
            step = SKIP_WINDOW_MS  # 7 days
        else:
            step = WINDOW_MS  # 6 hours

        window_end = min(window_start + step, now_ms)

        try:
            candles = await fetch_candles(session, market_id, window_start, window_end)
        except Exception as e:
            log.error(f"[{coin}] Fetch error at {ts_to_date_str(window_start)}: {e}")
            window_start = window_end + 1
            continue

        if candles:
            empty_streak = 0
            total_candles += len(candles)
            for date_str, day_candles in group_candles_by_day(candles).items():
                save_day(coin, date_str, day_candles)

            # If we were in skip mode and got data, rewind to the start of
            # this skip window and rescan with fine-grained 6h windows
            if step == SKIP_WINDOW_MS:
                empty_streak = 0  # switch to fine-grained mode
                continue  # window_start unchanged, rescan this range in 6h steps
        else:
            empty_streak += 1

        if time.time() - last_log > 10:
            pct = (window_start - start_ms) / max(now_ms - start_ms, 1) * 100
            mode = "skip" if empty_streak >= 4 else "scan"
            log.info(
                f"[{coin}] Backfill {pct:.0f}% ({ts_to_date_str(window_start)}) "
                f"— {total_candles} candles [{mode}]"
            )
            last_log = time.time()

        window_start = window_end + 1
        set_fetched_until(coin, window_start)

    log.info(f"[{coin}] Backfill done: {total_candles} candles")


async def update_market(session: aiohttp.ClientSession, market_id: int, coin: str):
    """Fetch candles since last data using windowed approach (handles large gaps)."""
    resume_ts = find_resume_ts(coin)
    if not resume_ts:
        return await backfill_market(session, market_id, coin)

    now_ms = int(time.time() * 1000)
    total = 0
    window_start = resume_ts

    while window_start < now_ms and not shutdown_event.is_set():
        window_end = min(window_start + WINDOW_MS, now_ms)
        try:
            candles = await fetch_candles(session, market_id, window_start, window_end)
        except Exception as e:
            log.error(f"[{coin}] Update fetch error: {e}")
            break

        if candles:
            total += len(candles)
            for date_str, day_candles in group_candles_by_day(candles).items():
                save_day(coin, date_str, day_candles)

        window_start = window_end + 1
        set_fetched_until(coin, window_start)

    if total > 0:
        log.info(f"[{coin}] Updated: +{total} candles")


async def _safe_task(coro, coin: str):
    """Wrapper to isolate errors per market."""
    try:
        await coro
    except Exception as e:
        log.error(f"[{coin}] Unhandled error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run():
    global _rate_limiter
    _rate_limiter = RateLimiter(MAX_CONCURRENT, REQUEST_INTERVAL)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("Lighter OHLCV Collector starting")
    log.info(f"  Data dir:       {DATA_DIR.resolve()}")
    log.info(f"  Base URL:       {BASE_URL}")
    log.info(f"  Symbols:        {SYMBOLS or 'all'}")
    log.info(f"  Earliest date:  {EARLIEST_DATE}")
    log.info(f"  Poll interval:  {POLL_INTERVAL}s")
    log.info(f"  Max concurrent: {MAX_CONCURRENT}")
    log.info(f"  Request interval: {REQUEST_INTERVAL}s")

    async with aiohttp.ClientSession() as session:
        markets = await discover_markets(session)
        if not markets:
            log.error("No markets found, exiting")
            return

        # Phase 1: Backfill all markets concurrently
        log.info("=== Phase 1: Backfill ===")
        tasks = [
            _safe_task(backfill_market(session, mid, coin), coin)
            for mid, coin in sorted(markets.items())
        ]
        await asyncio.gather(*tasks)

        if shutdown_event.is_set():
            log.info("Collector stopped (shutdown during backfill)")
            return

        # Phase 2: Continuous updates
        log.info("=== Phase 2: Continuous collection ===")
        last_discovery = time.time()

        while not shutdown_event.is_set():
            if time.time() - last_discovery > 3600:
                markets = await discover_markets(session)
                last_discovery = time.time()

            update_tasks = [
                _safe_task(update_market(session, mid, coin), coin)
                for mid, coin in sorted(markets.items())
            ]
            await asyncio.gather(*update_tasks)

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass

    log.info("Collector stopped")


if __name__ == "__main__":
    asyncio.run(run())
