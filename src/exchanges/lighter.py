from passivbot import Passivbot, logging
import asyncio
import math
import random
import traceback
import time
import os
from collections import deque

import aiohttp
import lighter
from lighter import CandlestickApi

import passivbot_rust as pbr
from utils import ts_to_date, symbol_to_coin, coin_to_symbol, utc_ms
from config_utils import require_live_value
from pure_funcs import shorten_custom_id
from procedures import print_async_exception

try:
    import orjson as _orjson

    def _json_dumps(obj):
        return _orjson.dumps(obj).decode()

    _json_loads = _orjson.loads
except ImportError:
    import json as _json_mod

    _json_dumps = _json_mod.dumps
    _json_loads = _json_mod.loads

round_ = pbr.round_
round_up = pbr.round_up
round_dn = pbr.round_dn

_PONG_MSG = '{"type":"pong"}'


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

def _is_quota_error(exc_or_msg) -> bool:
    """Return True if the error is specifically a volume-quota exhaustion."""
    msg = str(exc_or_msg).lower()
    if any(
        phrase in msg
        for phrase in (
            "didn't use volume quota",
            "did not use volume quota",
            "didnt use volume quota",
        )
    ):
        return False
    return (
        "volume quota" in msg
        or ("quota" in msg and "not enough" in msg)
        or ("quota" in msg and "exhausted" in msg)
    )


def _is_transient_error(exc) -> bool:
    """Return True for rate-limit (429), server errors (5xx), nonce errors,
    and network/DNS errors — temporary, not fatal.

    Note: quota errors are intentionally excluded; use ``_is_quota_error`` for those.
    """
    # Catch asyncio/builtin TimeoutError by type (str() is often empty)
    if isinstance(exc, (TimeoutError, OSError)):
        return True
    msg = str(exc).lower()
    if "429" in msg or "too many" in msg or "invalid nonce" in msg:
        return True
    # Detect 5xx server errors (502 Bad Gateway, 503 Service Unavailable, etc.)
    if "bad gateway" in msg or "service unavailable" in msg or "gateway timeout" in msg:
        return True
    # Detect transient network/DNS errors
    if "dns" in msg or "timeout" in msg or "connect" in msg or "reset by peer" in msg:
        return True
    s = str(exc)
    if any(f"({code})" in s for code in (500, 502, 503, 504)):
        return True
    return False


def _detect_429(exc) -> bool:
    """Return True if the exception indicates a 429 rate-limit."""
    msg = str(exc).lower()
    return "429" in msg or "too many" in msg


def _get_lighter_response_code(resp) -> int:
    """Normalize transaction response codes across SDK REST and WS wrappers."""
    if resp is None:
        return -1

    if isinstance(resp, dict):
        raw_code = resp.get("code", resp.get("status_code"))
        err = resp.get("error")
        if isinstance(err, dict):
            raw_code = err.get("code", raw_code)
    else:
        raw_code = getattr(resp, "code", getattr(resp, "status_code", None))

    try:
        code = int(raw_code) if raw_code is not None else 0
    except (TypeError, ValueError):
        code = 0
    return 0 if code == 200 else code


def _get_lighter_response_message(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, dict):
        err = resp.get("error")
        if isinstance(err, dict):
            return str(err.get("message", ""))
        return str(resp.get("message", ""))
    return str(getattr(resp, "message", str(resp)))


class _WsBatchResponse:
    """Adapt a WS dict response to match SDK response attribute access for Phase 3."""

    def __init__(self, data: dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = int(err.get("code", -1))
            self.message = str(err.get("message", ""))
        else:
            raw_code = data.get("code", data.get("status_code", 0))
            code = int(raw_code) if raw_code is not None else 0
            self.message = str(data.get("message", ""))
        # WS uses HTTP-style codes: 200 = success
        self.code = 0 if code == 200 else code
        self.volume_quota_remaining = data.get("volume_quota_remaining")
        self.tx_hash = data.get("tx_hash", [])


class _TxWebSocket:
    """Persistent WebSocket for sending transactions, bypassing the 200 msg/min REST limit.

    Uses ``jsonapi/sendtxbatch`` messages.  Runs a background recv loop to
    handle the Lighter server's app-level ``{"type":"ping"}`` JSON messages
    even when idle, preventing the server from dropping the connection.
    """

    def __init__(self, url: str):
        self._url = url
        self._ws = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._recv_task = None
        self._response_queue = asyncio.Queue()
        self._close_requested = False

    async def connect(self) -> None:
        """Establish (or re-establish) the WS connection and start the recv loop."""
        import websockets

        await self._close_internal()
        try:
            self._ws = await websockets.connect(
                self._url,
                ping_interval=20,
                ping_timeout=30,
            )
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
                init_msg = _json_loads(raw)
                logging.info("TxWebSocket connected (%s)", init_msg.get("type", "?"))
            except asyncio.TimeoutError:
                logging.info("TxWebSocket connected (no init message)")
            self._connected = True
            self._recv_task = asyncio.create_task(self._recv_loop())
        except Exception as e:
            self._connected = False
            logging.warning("TxWebSocket connect failed: %s", e)

    async def _recv_loop(self) -> None:
        """Continuously read from WS, handle app-level pings, route responses to queue."""
        try:
            while self._connected and self._ws is not None:
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=120)
                    data = _json_loads(raw)
                    if not isinstance(data, dict):
                        continue
                    msg_type = data.get("type", "")

                    if msg_type == "ping":
                        await self._ws.send(_PONG_MSG)
                        continue

                    if msg_type in ("connected", "subscribed"):
                        continue

                    # Transaction response — route to send_batch caller
                    await self._response_queue.put(data)

                except asyncio.TimeoutError:
                    # 120s silence is unusual; probe with app-level ping
                    try:
                        await self._ws.send(_json_dumps({"type": "ping"}))
                    except Exception:
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.warning("TxWebSocket recv loop error: %s", e)
                    logging.debug(traceback.format_exc())
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._connected = False
            logging.info("TxWebSocket recv loop exited")

    @property
    def is_connected(self) -> bool:
        if not self._connected or self._ws is None:
            return False
        try:
            return self._ws.state.name == "OPEN"
        except Exception:
            return False

    async def send_batch(self, tx_types: list, tx_infos: list) -> dict | None:
        """Send a transaction batch via WS and return the parsed response.

        Returns None if WS is unavailable (caller should fall back to REST).
        """
        async with self._lock:
            if not self.is_connected:
                if self._close_requested:
                    return None
                try:
                    await self.connect()
                except Exception:
                    return None
                if not self.is_connected:
                    return None

            # Drain any stale messages from the queue
            while not self._response_queue.empty():
                try:
                    self._response_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            msg = _json_dumps({
                "type": "jsonapi/sendtxbatch",
                "data": {
                    "tx_types": _json_dumps(tx_types),
                    "tx_infos": _json_dumps(tx_infos),
                },
            })

            try:
                await self._ws.send(msg)
                response = await asyncio.wait_for(self._response_queue.get(), timeout=10.0)
                return response
            except asyncio.TimeoutError:
                logging.warning("TxWebSocket: timeout waiting for batch response")
                self._connected = False
                return None
            except Exception as e:
                logging.warning("TxWebSocket send failed (%s); marking disconnected", e)
                self._connected = False
                return None

    async def _close_internal(self) -> None:
        """Close WS and cancel recv task without setting _close_requested."""
        self._connected = False
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
            self._recv_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def close(self) -> None:
        """Close the WS connection permanently."""
        self._close_requested = True
        await self._close_internal()


class LighterBot(Passivbot):
    def __init__(self, config: dict):
        # These must be set BEFORE super().__init__() because it calls
        # create_ccxt_sessions() which needs them.
        from procedures import load_user_info

        _user_info = load_user_info(require_live_value(config, "user"))
        self.account_index = int(_user_info.get("account_index", 0))
        self.api_key_index = int(_user_info.get("api_key_index", 0))
        self.private_key = _user_info.get("private_key", "")
        self.base_url = _user_info.get(
            "base_url", "https://mainnet.zklighter.elliot.ai"
        )
        self.ws_url = _user_info.get(
            "ws_url", "wss://mainnet.zklighter.elliot.ai/stream"
        )
        self.lighter_client = None
        self.api_client = None
        self.order_api = None
        self.account_api = None
        self.candlestick_api = None

        super().__init__(config)

        # Patch CandlestickManager's exchange reference.
        # CM only needs fetch_ohlcv(symbol, timeframe, since, limit) and
        # fetch_ticker(symbol).  LighterBot implements fetch_ohlcv already;
        # we add a thin fetch_ticker adapter and an `id` attribute so the CM
        # recognises us as a valid exchange object.
        if hasattr(self, "cm") and self.cm is not None:
            self.cm.exchange = self

        self.id = "lighter"  # CM reads exchange.id for cache paths
        self.quote = "USDC"
        self.hedge_mode = False
        self.custom_id_max_length = 36

        # Lighter-specific state
        self.market_id_map = {}  # symbol -> market_index (int)
        self.market_index_to_symbol = {}  # market_index -> symbol
        self.price_tick_sizes = {}  # symbol -> float tick size
        self.amount_tick_sizes = {}  # symbol -> float tick size
        self.price_decimals = {}  # symbol -> int
        self.amount_decimals = {}  # symbol -> int
        self._client_to_exchange_order_id = {}  # client_order_index -> order_index
        self._sdk_write_lock = asyncio.Lock()
        self._ws_task = None
        self._tx_ws = None  # _TxWebSocket instance, initialized in start_data_maintainers
        self._auth_token = None
        self._auth_token_ts = 0
        self._aiohttp_session = None  # persistent session for REST calls

        # API outage tracking
        self._api_consecutive_failures = 0
        self._api_down_since = 0.0        # time.monotonic() when outage started
        self._api_last_status_log = 0.0   # throttle "still down" messages

        # Rate limiting state (40 ops / 60s sliding window)
        self._rl_ops_per_window = 40
        self._rl_window_seconds = 60.0
        self._rl_min_send_interval = 0.1
        self._rl_cancel_min_interval = 0.5
        self._rl_backoff_base = 15.0
        self._rl_backoff_max = 120.0
        self._rl_backoff_reset_after = 2
        self._op_timestamps = deque()
        self._last_send_time = 0.0
        self._consecutive_successes = 0
        self._global_backoff_until = 0.0
        self._global_backoff_consecutive = 0
        self._rl_min_read_interval = 0.5  # 500ms between reads → max ~2 req/s
        self._last_read_time = 0.0
        self._read_gate_lock = asyncio.Lock()

        # Volume quota tracking
        self._rl_quota_high = 500
        self._rl_quota_medium = 50
        self._rl_quota_low = 10
        self._rl_free_slot_interval = 15.0
        self._volume_quota_remaining = None  # None = unknown until first response
        self._quota_warning_level = "ok"

        # Atomic counter for client order IDs to avoid collision
        self._order_id_counter = 0
        self._last_client_order_id = 0

        # TTL cache for fetch_tickers (avoid refetching all order books per symbol)
        self._tickers_cache = None
        self._tickers_cache_ts = 0.0
        self._tickers_cache_ttl = 2.0  # seconds

        # Fix 1: Known exchange order IDs for collision guard
        self._known_exchange_order_ids = set()

        # Fix 4: Rejection tracking circuit breaker
        self._consecutive_rejections = 0
        self._rejection_pause_until = 0.0

        # Fix 5: WS cancel confirmation events
        self._order_cancel_events = {}  # exchange_order_id -> asyncio.Event

        # WS snapshot tracking: first message per channel is a full snapshot
        self._ws_orders_snapshot_received = False

        # WS cache: serve positions/balance/orders from WS data, fall back to REST
        self._ws_positions_cache = None       # list of position dicts, or None
        self._ws_positions_cache_ts = 0.0     # monotonic timestamp of last update
        self._ws_balance_cache = None         # float balance from WS, or None
        self._ws_balance_cache_ts = 0.0
        self._last_fetched_balance = None    # consume-once cache for fetch_balance()
        self._ws_open_orders_cache = None     # list of order dicts, or None
        self._ws_open_orders_cache_ts = 0.0
        self._ws_cache_max_age = 30.0         # seconds before cache is considered stale

        # WS ticker cache: BBO from ticker/{MARKET_INDEX} channel
        self._ws_tickers_cache = {}           # symbol -> {"bid", "ask", "last"}
        self._ws_tickers_cache_ts = 0.0

        # Disk cache for market metadata (avoids order_books REST on restart)
        self._market_metadata_cache_path = os.path.join("caches", "lighter", "market_metadata.json")
        self._MARKET_METADATA_CACHE_MAX_AGE_S = 3600  # 1 hour

        # Max leverage overrides — SDK doesn't expose this field
        self._max_leverage_overrides = {
            "HYPE": 20,
        }
        self._max_leverage_default = 20

        # Fix 7: Quota recovery state
        self._quota_recovery_in_progress = False
        self._quota_recovery_last_attempt = 0.0
        self._qr_post_warmup_grace = 120.0  # seconds after init before allowing recovery
        self._bot_start_time = time.monotonic()

        self.max_n_concurrent_ohlcvs_1m_updates = 2

    def create_ccxt_sessions(self):
        """Initialize Lighter SDK clients instead of CCXT."""
        self.cca = None
        self.ccp = None

        self.api_client = lighter.ApiClient(
            configuration=lighter.Configuration(host=self.base_url)
        )
        self.order_api = lighter.OrderApi(self.api_client)
        self.account_api = lighter.AccountApi(self.api_client)
        self.candlestick_api = CandlestickApi(self.api_client)

        try:
            # Newer SDK signature (try first to avoid TypeError on modern SDK)
            self.lighter_client = lighter.SignerClient(
                url=self.base_url,
                account_index=self.account_index,
                api_private_keys={self.api_key_index: self.private_key},
            )
        except TypeError:
            # Older SDK signature
            self.lighter_client = lighter.SignerClient(
                url=self.base_url,
                private_key=self.private_key,
                account_index=self.account_index,
                api_key_index=self.api_key_index,
            )

    # --- Raw price/amount conversion helpers ---

    def _to_raw_price(self, price, symbol):
        tick = self.price_tick_sizes[symbol]
        return int(round(price / tick))

    def _to_raw_amount(self, amount, symbol):
        tick = self.amount_tick_sizes[symbol]
        return int(round(abs(amount) / tick))

    def _from_raw_price(self, raw, symbol):
        return raw * self.price_tick_sizes[symbol]

    def _from_raw_amount(self, raw, symbol):
        return raw * self.amount_tick_sizes[symbol]

    @staticmethod
    def _coerce_is_ask(raw) -> bool:
        """Coerce is_ask field to bool. API may return bool, int, float, or string."""
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.lower() in ("true", "1", "yes", "sell", "ask")
        return False

    def _trim_order_id_mapping(self, max_size=200, trim_to=100):
        """Cap _client_to_exchange_order_id to prevent unbounded growth."""
        if len(self._client_to_exchange_order_id) > max_size:
            # Keep the most recent entries (highest client IDs = most recent timestamps)
            sorted_keys = sorted(self._client_to_exchange_order_id.keys())
            for k in sorted_keys[:len(sorted_keys) - trim_to]:
                del self._client_to_exchange_order_id[k]

    def _generate_client_order_id(self):
        self._order_id_counter += 1
        max_id = 2**48 - 1
        new_id = (time.time_ns() + self._order_id_counter) % max_id
        if new_id <= self._last_client_order_id:
            new_id = (self._last_client_order_id + 1) % max_id
        self._last_client_order_id = new_id
        return new_id

    def _symbol_to_market_index(self, symbol):
        return self.market_id_map[symbol]

    @staticmethod
    def _extract_lighter_balance(data, fields):
        for field in fields:
            val = getattr(data, field, None) if not isinstance(data, dict) else data.get(field)
            if val is not None:
                try:
                    parsed = float(val)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed):
                    return parsed
        return None

    def _get_balance_value_from_account_data(self, account_data):
        balance = self._extract_lighter_balance(
            account_data, ["collateral", "available_balance"]
        )
        if balance is None:
            logging.warning(
                "no balance field found in account data (collateral, available_balance all None)"
            )
            return 0.0
        return balance

    def _get_balance_value_from_user_stats(self, stats):
        return self._extract_lighter_balance(
            stats, ["collateral", "available_balance"]
        )

    def _normalize_market_order_for_lighter(self, order: dict):
        order_type = order.get("type", "limit")
        tif_config = require_live_value(self.config, "time_in_force")
        if order_type == "market":
            if tif_config == "post_only":
                logging.warning("lighter: rejecting market order while time_in_force=post_only")
                return None
            # Use an aggressive GTC limit to preserve one-send execution semantics
            # without additional REST reads or quota consumption.
            return (
                lighter.SignerClient.ORDER_TYPE_LIMIT,
                lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            )
        if tif_config == "post_only":
            return (
                lighter.SignerClient.ORDER_TYPE_LIMIT,
                lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
            )
        return (
            lighter.SignerClient.ORDER_TYPE_LIMIT,
            lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
        )

    def _get_cached_market_ticker(self, symbol: str) -> dict | None:
        for source in (
            self._ws_tickers_cache,
            getattr(self, "tickers", None),
            self._tickers_cache,
        ):
            if source and symbol in source and isinstance(source[symbol], dict):
                return source[symbol]
        return None

    def _get_market_execution_price(self, order: dict) -> float:
        price = float(order["price"])
        if order.get("type") != "market":
            return price

        symbol = order["symbol"]
        tick = float(self.price_steps.get(symbol, 0.0) or 0.0)
        ticker = self._get_cached_market_ticker(symbol) or {}
        last = float(ticker.get("last", 0.0) or 0.0)

        if order["side"] == "buy":
            best = float(ticker.get("ask", 0.0) or last or price)
            aggressive = max(price, best)
            if tick > 0.0:
                aggressive = max(aggressive * 1.001, aggressive + tick)
            return aggressive

        best = float(ticker.get("bid", 0.0) or last or price)
        aggressive = min(price, best)
        if tick > 0.0:
            aggressive = min(aggressive * 0.999, aggressive - tick)
            aggressive = max(tick, aggressive)
        return aggressive

    @staticmethod
    def _parse_candles_payload(payload):
        if payload is None:
            return []

        if isinstance(payload, dict):
            candles_raw = payload.get("c") or payload.get("candlesticks") or []
        else:
            candles_raw = getattr(payload, "c", None)
            if candles_raw is None:
                candles_raw = getattr(payload, "candlesticks", [])

        candles = []
        for candle in candles_raw:
            if isinstance(candle, dict):
                if "t" in candle:
                    ts = candle.get("t")
                    open_ = candle.get("o")
                    high = candle.get("h")
                    low = candle.get("l")
                    close = candle.get("c")
                    volume = candle.get("v", 0)
                else:
                    ts = candle.get("timestamp")
                    open_ = candle.get("open")
                    high = candle.get("high")
                    low = candle.get("low")
                    close = candle.get("close")
                    volume = candle.get("volume", 0)
            else:
                ts = getattr(candle, "t", None)
                open_ = getattr(candle, "o", None)
                high = getattr(candle, "h", None)
                low = getattr(candle, "l", None)
                close = getattr(candle, "c", None)
                volume = getattr(candle, "v", 0)
                if ts is None:
                    ts = getattr(candle, "timestamp", None)
                    open_ = getattr(candle, "open", None)
                    high = getattr(candle, "high", None)
                    low = getattr(candle, "low", None)
                    close = getattr(candle, "close", None)
                    volume = getattr(candle, "volume", 0)

            if None in (ts, open_, high, low, close):
                continue
            candles.append(
                [
                    int(ts),
                    float(open_),
                    float(high),
                    float(low),
                    float(close),
                    float(volume or 0),
                ]
            )
        return candles

    async def _fetch_candles_via_sdk(self, symbol, timeframe, n_candles, since=None):
        if self.candlestick_api is None or not hasattr(self.candlestick_api, "candlesticks"):
            return []

        params = {
            "market_id": self.market_id_map[symbol],
            "resolution": timeframe,
            "count_back": n_candles,
        }
        if since is not None:
            params["start_timestamp"] = int(since)
        payload = await self.candlestick_api.candlesticks(**params)
        return self._parse_candles_payload(payload)

    # --- Persistent aiohttp session ---

    async def _get_aiohttp_session(self):
        """Return a persistent aiohttp session, creating one if needed."""
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
            self._aiohttp_session = aiohttp.ClientSession(timeout=timeout)
        return self._aiohttp_session

    # --- Auth token management ---

    async def _get_auth_token(self):
        now = time.time()
        if self._auth_token and now - self._auth_token_ts < 3300:
            return self._auth_token
        try:
            auth, err = self.lighter_client.create_auth_token_with_expiry(deadline=3600)
            if err:
                logging.error(f"error creating auth token: {err}")
                return self._auth_token
            self._auth_token = auth
            self._auth_token_ts = now
            return auth
        except Exception as e:
            logging.error(f"error creating auth token: {e}")
            logging.debug(traceback.format_exc())
            return self._auth_token

    # --- Rate limiting ---

    def _prune_op_window(self):
        """Evict ops older than the rolling window; return current op count."""
        cutoff = time.monotonic() - self._rl_window_seconds
        while self._op_timestamps and self._op_timestamps[0] < cutoff:
            self._op_timestamps.popleft()
        return len(self._op_timestamps)

    def _ops_available(self):
        """How many ops can we send right now within the sliding window."""
        return max(0, self._rl_ops_per_window - self._prune_op_window())

    def _time_until_ops_free(self, n):
        """Seconds until n ops become available (0.0 if already available)."""
        self._prune_op_window()
        if len(self._op_timestamps) + n <= self._rl_ops_per_window:
            return 0.0
        idx = len(self._op_timestamps) - (self._rl_ops_per_window - n)
        if idx < 0:
            return 0.0
        if idx >= len(self._op_timestamps):
            idx = len(self._op_timestamps) - 1
        expires_at = self._op_timestamps[idx] + self._rl_window_seconds
        return max(0.0, expires_at - time.monotonic())

    def _record_ops_sent(self, count):
        """Record count operations sent at the current time."""
        now = time.monotonic()
        self._op_timestamps.extend([now] * count)
        self._last_send_time = now

    def _trigger_global_backoff(self):
        """Set a global cooldown after hitting 429. Escalates with consecutive failures.

        Only escalates the counter if the previous backoff window has expired,
        so parallel 429s within the same window don't stack up.
        """
        now = time.monotonic()
        if now < self._global_backoff_until:
            # Already in a backoff window — don't escalate
            return
        self._global_backoff_consecutive += 1
        self._consecutive_successes = 0
        duration = min(
            self._rl_backoff_base * (2 ** (self._global_backoff_consecutive - 1)),
            self._rl_backoff_max,
        )
        self._global_backoff_until = now + duration
        logging.warning(f"429 rate limit — global backoff for {duration:.0f}s "
                        f"(attempt #{self._global_backoff_consecutive})")

    def _reset_global_backoff(self):
        """After a successful write, require N consecutive successes before resetting."""
        self._consecutive_successes += 1
        if self._global_backoff_consecutive > 0:
            if self._consecutive_successes >= self._rl_backoff_reset_after:
                self._global_backoff_consecutive = 0
                self._consecutive_successes = 0

    # --- API outage tracking ---

    def _record_api_failure(self, exc, context=""):
        """Track consecutive API failures with throttled logging."""
        logging.debug("API failure traceback (%s):\n%s", context, traceback.format_exc())
        now = time.monotonic()
        self._api_consecutive_failures += 1
        if self._api_consecutive_failures == 1:
            self._api_down_since = now
            self._api_last_status_log = now
            logging.warning("API error (%s): %s: %s", context, type(exc).__name__, exc)
        elif self._api_consecutive_failures == 3:
            logging.warning(
                "Lighter API appears down (%d consecutive failures) "
                "-- retrying with exponential backoff",
                self._api_consecutive_failures,
            )
            self._api_last_status_log = now
        elif now - self._api_last_status_log >= 300:
            down_s = now - self._api_down_since
            logging.info(
                "API still unreachable (%.0fm%02.0fs, %d failures) -- continuing to retry",
                down_s // 60, down_s % 60, self._api_consecutive_failures,
            )
            self._api_last_status_log = now

    def _record_api_success(self):
        """Reset outage tracking; log recovery if was in outage."""
        if self._api_consecutive_failures >= 3:
            down_s = time.monotonic() - self._api_down_since
            logging.info(
                "API recovered after %.0fm%02.0fs (%d failures) -- resuming normal operation",
                down_s // 60, down_s % 60, self._api_consecutive_failures,
            )
        self._api_consecutive_failures = 0
        self._api_down_since = 0.0
        self._api_last_status_log = 0.0

    @property
    def api_is_down(self):
        return self._api_consecutive_failures >= 3

    # --- Volume quota ---

    def _quota_pace_multiplier(self):
        """Return a pacing multiplier based on volume_quota_remaining.
        1.0 = full speed, higher = slower, inf = wait for free slot."""
        if self._volume_quota_remaining is None or self._volume_quota_remaining >= self._rl_quota_high:
            return 1.0
        if self._volume_quota_remaining >= self._rl_quota_medium:
            return 1.5
        if self._volume_quota_remaining >= self._rl_quota_low:
            return 3.0
        return float("inf")

    def _update_volume_quota(self, raw):
        """Parse volume_quota_remaining from an exchange response."""
        if raw is None or raw == "?":
            return
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return
        self._volume_quota_remaining = val

        if val <= 0:
            if self._quota_warning_level != "critical":
                logging.warning("QUOTA EXHAUSTED (0 remaining) — free-slot pacing only (1 tx / 16s)")
                self._quota_warning_level = "critical"
        elif val < self._rl_quota_low:
            if self._quota_warning_level != "low":
                logging.warning(f"QUOTA LOW: {val} remaining (< {self._rl_quota_low}) — 3x pacing")
                self._quota_warning_level = "low"
        elif val < self._rl_quota_medium:
            if self._quota_warning_level != "medium":
                logging.warning(f"QUOTA MEDIUM: {val} remaining (< {self._rl_quota_medium}) — 1.5x pacing")
                self._quota_warning_level = "medium"
        elif self._quota_warning_level != "ok":
            logging.info(f"QUOTA RECOVERED: {val} remaining — full speed")
            self._quota_warning_level = "ok"

    async def _wait_for_write_slot(self, op_count=1, cancel_only=False):
        """Adaptive rate-limit gate. Returns True if OK to proceed, False to skip."""
        deadline = time.monotonic() + 15.0  # max total wait to prevent stale data

        # Fix 4: Rejection circuit breaker pause
        if time.monotonic() < self._rejection_pause_until:
            return False

        now = time.monotonic()

        # Phase 1: Global 429 backoff
        if now < self._global_backoff_until:
            remaining = self._global_backoff_until - now
            if remaining <= 2.0 and remaining <= (deadline - time.monotonic()):
                await asyncio.sleep(remaining)
            else:
                logging.warning(f"RATE LIMIT: global backoff active ({remaining:.0f}s remaining) — skipping")
                return False

        # Phase 2: Sliding window capacity
        avail = self._ops_available()
        if avail < op_count:
            wait_time = self._time_until_ops_free(op_count)
            if wait_time > 30.0 or wait_time > (deadline - time.monotonic()):
                return False
            if wait_time > 0:
                await asyncio.sleep(wait_time)

        if time.monotonic() >= deadline:
            return False

        # Phase 3: Minimum send-interval floor
        floor = self._rl_cancel_min_interval if cancel_only else self._rl_min_send_interval
        elapsed = time.monotonic() - self._last_send_time
        if elapsed < floor:
            sleep_time = floor - elapsed
            if sleep_time > (deadline - time.monotonic()):
                return False
            await asyncio.sleep(sleep_time)

        if time.monotonic() >= deadline:
            return False

        # Phase 4: Volume-quota pacing (skip for cancel-only)
        if not cancel_only:
            mult = self._quota_pace_multiplier()
            if mult == float("inf"):
                since_last = time.monotonic() - self._last_send_time
                if since_last < self._rl_free_slot_interval:
                    wait_free = self._rl_free_slot_interval - since_last
                    if wait_free > (deadline - time.monotonic()):
                        return False
                    logging.warning(f"QUOTA: low ({self._volume_quota_remaining} remaining), "
                                    f"waiting {wait_free:.1f}s for free slot")
                    await asyncio.sleep(wait_free)
            elif mult > 1.0:
                extra = floor * (mult - 1.0)
                elapsed2 = time.monotonic() - self._last_send_time
                if elapsed2 < floor + extra:
                    sleep_time = floor + extra - elapsed2
                    if sleep_time > (deadline - time.monotonic()):
                        return False
                    await asyncio.sleep(sleep_time)

        return True

    async def _wait_for_read_slot(self, timeout=30.0):
        """Wait until safe to make a read call (429 backoff + pacing).
        Returns True if ok to proceed, False if timeout expired.
        Uses an async lock so concurrent callers are serialized."""
        async with self._read_gate_lock:
            # Gate 1: global 429 backoff
            remaining = self._global_backoff_until - time.monotonic()
            if remaining > 0:
                if remaining > timeout:
                    logging.debug(f"Read gate: backoff {remaining:.0f}s exceeds timeout {timeout:.0f}s, skipping")
                    return False
                logging.debug(f"Read gate: waiting {remaining:.1f}s for backoff to expire")
                await asyncio.sleep(remaining)
            # Gate 2: minimum interval between reads to avoid bursting
            now = time.monotonic()
            elapsed = now - self._last_read_time
            if elapsed < self._rl_min_read_interval:
                await asyncio.sleep(self._rl_min_read_interval - elapsed)
            self._last_read_time = time.monotonic()
            return True

    # --- Nonce error handling ---

    def _handle_nonce_error(self, error_msg, api_key_idx=None):
        """Handle nonce/quota/transient errors with appropriate recovery."""
        if api_key_idx is None:
            api_key_idx = self.api_key_index
        err_str = str(error_msg)

        if _is_quota_error(err_str):
            self._update_volume_quota(0)
            if hasattr(self.lighter_client, "nonce_manager"):
                self.lighter_client.nonce_manager.hard_refresh_nonce(api_key_idx)
            logging.warning("Quota error — nonce refreshed, switching to free-slot pacing")
        elif _is_transient_error(err_str):
            self._trigger_global_backoff()
            if hasattr(self.lighter_client, "nonce_manager"):
                self.lighter_client.nonce_manager.hard_refresh_nonce(api_key_idx)
            logging.warning("Transient error — nonce refreshed, backoff triggered")
        elif "nonce" in err_str.lower():
            if hasattr(self.lighter_client, "nonce_manager"):
                self.lighter_client.nonce_manager.hard_refresh_nonce(api_key_idx)
            logging.warning("Nonce error — nonce refreshed")

    def _acknowledge_nonce_failure(self, api_key_idx=None):
        """Roll back nonce counter after a failed send."""
        if api_key_idx is None:
            api_key_idx = self.api_key_index
        if hasattr(self.lighter_client, "nonce_manager") and hasattr(
            self.lighter_client.nonce_manager, "acknowledge_failure"
        ):
            self.lighter_client.nonce_manager.acknowledge_failure(api_key_idx)

    # --- Quota recovery ---

    async def _attempt_quota_recovery(self):
        """Send small IOC market orders to generate volume and recover quota.

        Safeguards: max 3 attempts, max $2 cumulative loss, verify quota increases,
        2-minute cooldown between attempts.
        """
        if self._quota_recovery_in_progress:
            return False
        if self._volume_quota_remaining is None or self._volume_quota_remaining >= 5:
            return False
        # Post-warmup grace: don't attempt recovery until bot has been running for a while
        if time.monotonic() - self._bot_start_time < self._qr_post_warmup_grace:
            return False
        if time.monotonic() - self._quota_recovery_last_attempt < 120:
            return False

        self._quota_recovery_in_progress = True
        self._quota_recovery_last_attempt = time.monotonic()
        max_loss = 2.0
        max_attempts = 3
        target_quota = 50

        try:
            logging.warning(f"QUOTA RECOVERY: starting (quota={self._volume_quota_remaining}, target={target_quota})")

            if hasattr(self.lighter_client, "nonce_manager"):
                self.lighter_client.nonce_manager.hard_refresh_nonce(self.api_key_index)

            # Snapshot balance before recovery for real PnL safety check
            initial_balance = None
            try:
                result = await self._fetch_positions_and_balance()
                if result and isinstance(result, tuple) and len(result) == 2:
                    initial_balance = result[1]
            except Exception:
                pass

            for attempt in range(1, max_attempts + 1):
                # Wait for free slot
                since_last = time.monotonic() - self._last_send_time
                free_wait = self._rl_free_slot_interval + 1.0
                if since_last < free_wait:
                    await asyncio.sleep(free_wait - since_last)

                # Find a symbol to trade on
                symbol = None
                for sym in self.active_symbols:
                    if sym in self.market_id_map:
                        symbol = sym
                        break
                if not symbol:
                    symbol = next(iter(self.market_id_map), None)
                if not symbol:
                    logging.warning("QUOTA RECOVERY: no markets available, aborting")
                    return False

                market_index = self.market_id_map[symbol]

                # Use minimum order size
                min_qty = self.min_qtys.get(symbol, self.qty_steps.get(symbol, 0.01))
                qty_step = self.qty_steps.get(symbol, 0.01)
                size = max(min_qty, qty_step)

                # Get current prices from tickers
                tickers = await self.fetch_tickers()
                if not tickers or symbol not in tickers:
                    logging.warning("QUOTA RECOVERY: no ticker data, aborting")
                    return False

                ticker = tickers[symbol]
                best_bid = ticker.get("bid", 0)
                best_ask = ticker.get("ask", 0)
                if not best_bid or not best_ask:
                    logging.warning("QUOTA RECOVERY: no bid/ask data, aborting")
                    return False

                # Check position to determine direction (reduce if possible)
                is_ask = True  # default sell
                if symbol in self.positions:
                    pos_size = self.positions[symbol].get("long", {}).get("size", 0)
                    if pos_size > 0:
                        is_ask = True  # sell to reduce long
                    else:
                        is_ask = False  # buy to reduce short

                price = best_bid if is_ask else best_ask
                raw_price = self._to_raw_price(price, symbol)
                raw_size = self._to_raw_amount(size, symbol)
                client_order_id = self._generate_client_order_id()

                logging.info(f"QUOTA RECOVERY: attempt {attempt}/{max_attempts} — "
                             f"{'SELL' if is_ask else 'BUY'} {size} @ {price}")

                old_quota = self._volume_quota_remaining
                try:
                    async with self._sdk_write_lock:
                        if hasattr(self.lighter_client, "create_market_order"):
                            tx, response, err = await self.lighter_client.create_market_order(
                                market_index=market_index,
                                client_order_index=client_order_id,
                                base_amount=raw_size,
                                avg_execution_price=raw_price,
                                is_ask=is_ask,
                            )
                        else:
                            # Fallback: use IOC limit order at crossing price
                            order_type = lighter.SignerClient.ORDER_TYPE_LIMIT
                            tif = lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
                            tx, tx_hash, err = await self.lighter_client.create_order(
                                market_index=market_index,
                                client_order_index=client_order_id,
                                base_amount=raw_size,
                                price=raw_price,
                                is_ask=is_ask,
                                order_type=order_type,
                                time_in_force=tif,
                                reduce_only=False,
                            )
                            response = tx

                    if err:
                        logging.warning(f"QUOTA RECOVERY: order error: {err}")
                        return False

                    self._record_ops_sent(1)

                    if response is not None:
                        self._update_volume_quota(getattr(response, 'volume_quota_remaining', None))

                    logging.info(f"QUOTA RECOVERY: attempt {attempt} — quota {old_quota} -> {self._volume_quota_remaining}")

                    if self._volume_quota_remaining is not None and self._volume_quota_remaining <= old_quota:
                        logging.warning(f"QUOTA RECOVERY: quota did not increase ({old_quota} -> {self._volume_quota_remaining}), stopping")
                        return False

                    # Real PnL safety check: verify actual balance change
                    if initial_balance is not None:
                        try:
                            result = await self._fetch_positions_and_balance()
                            if result and isinstance(result, tuple) and len(result) == 2:
                                current_balance = result[1]
                                actual_loss = initial_balance - current_balance
                                if actual_loss >= max_loss:
                                    logging.warning(
                                        f"QUOTA RECOVERY: actual balance loss ${actual_loss:.2f} >= "
                                        f"${max_loss:.2f} limit, aborting"
                                    )
                                    return self._volume_quota_remaining is not None and self._volume_quota_remaining >= 5
                        except Exception as bal_err:
                            logging.warning(f"QUOTA RECOVERY: balance check failed: {bal_err}")

                    if self._volume_quota_remaining is not None and self._volume_quota_remaining >= target_quota:
                        logging.info(f"QUOTA RECOVERY: target reached (quota={self._volume_quota_remaining}), resuming")
                        return True

                except Exception as e:
                    logging.warning(f"QUOTA RECOVERY: exception: {e}")
                    return False

            logging.warning(f"QUOTA RECOVERY: max attempts reached (quota={self._volume_quota_remaining})")
            return self._volume_quota_remaining is not None and self._volume_quota_remaining >= 5
        finally:
            self._quota_recovery_in_progress = False

    # --- Market initialization ---

    async def init_markets(self, verbose=True):
        """Override base to load markets from Lighter API instead of CCXT."""
        self.init_markets_last_update_ms = utc_ms()
        await self.update_exchange_config()

        # Try loading market metadata from disk cache first
        if not self._load_market_metadata_cache():
            # Cache miss or stale — fetch from REST with retry on 429
            for attempt in range(3):
                try:
                    order_books_resp = await self.order_api.order_books()
                    break
                except Exception as e:
                    if attempt == 2 or not _detect_429(e):
                        logging.error(f"error fetching order_books from Lighter: {e}")
                        logging.debug(traceback.format_exc())
                        raise
                    delay = 15 * (2 ** attempt)  # 15s, 30s
                    logging.warning(f"429 on order_books, retry in {delay}s (attempt {attempt+1}/3)")
                    await asyncio.sleep(delay)

            markets_dict = {}
            for ob in order_books_resp.order_books:
                symbol_base = ob.symbol.upper()
                symbol = f"{symbol_base}/{self.quote}:{self.quote}"
                market_id = int(ob.market_id)
                price_decimals = ob.supported_price_decimals
                size_decimals = ob.supported_size_decimals
                price_tick = 10 ** (-price_decimals)
                amount_tick = 10 ** (-size_decimals)
                min_base = float(ob.min_base_amount) if hasattr(ob, "min_base_amount") and ob.min_base_amount else amount_tick
                min_quote = float(ob.min_quote_amount) if hasattr(ob, "min_quote_amount") and ob.min_quote_amount else 1.0

                self.market_id_map[symbol] = market_id
                self.market_index_to_symbol[market_id] = symbol
                self.price_tick_sizes[symbol] = price_tick
                self.amount_tick_sizes[symbol] = amount_tick
                self.price_decimals[symbol] = price_decimals
                self.amount_decimals[symbol] = size_decimals

                markets_dict[symbol] = {
                    "symbol": symbol,
                    "id": str(market_id),
                    "base": symbol_base,
                    "quote": self.quote,
                    "active": True,
                    "swap": True,
                    "linear": True,
                    "type": "swap",
                    "precision": {
                        "amount": amount_tick,
                        "price": price_tick,
                    },
                    "limits": {
                        "amount": {"min": min_base},
                        "cost": {"min": min_quote},
                    },
                    "maker": 0.0,  # Lighter has zero fees
                    "taker": 0.0,
                    "contractSize": 1.0,
                    "info": {
                        "market_id": market_id,
                        "maxLeverage": self._max_leverage_overrides.get(
                            symbol_base, self._max_leverage_default
                        ),
                        "price_decimals": price_decimals,
                        "size_decimals": size_decimals,
                    },
                }

            self.markets_dict = markets_dict
            self._save_market_metadata_cache()

        # Build coin<->symbol caches for Lighter
        self._build_coin_symbol_caches()

        await self.determine_utc_offset(verbose)

        from utils import filter_markets
        eligible, _, reasons = filter_markets(self.markets_dict, self.exchange, verbose=verbose)
        self.eligible_symbols = set(eligible)
        self.ineligible_symbols = reasons

        self.set_market_specific_settings()

        self.max_len_symbol = max([len(s) for s in self.markets_dict]) if self.markets_dict else 17
        self.sym_padding = max(self.sym_padding, self.max_len_symbol + 1)
        self.init_coin_overrides()
        self.refresh_approved_ignored_coins_lists()
        self.set_wallet_exposure_limits()
        await self._start_ws_early()
        await self.update_positions()
        await self.update_open_orders()
        await self.update_effective_min_cost()
        if self.is_forager_mode():
            await self.update_first_timestamps()

    def _build_coin_symbol_caches(self):
        """Build local coin-to-symbol and symbol-to-coin maps for Lighter."""
        self.coin_to_symbol_map = {}
        self.symbol_to_coin_map = {}
        for symbol in self.markets_dict:
            base = self.markets_dict[symbol]["base"]
            self.coin_to_symbol_map[base] = symbol
            self.symbol_to_coin_map[symbol] = base
        # Also populate the global symbol_to_coin cache so that
        # utility functions (format_approved_ignored_coins, etc.) can resolve coins
        from utils import create_coin_symbol_map_cache
        create_coin_symbol_map_cache("lighter", self.markets_dict, verbose=False)

    def _load_market_metadata_cache(self):
        """Load market metadata from disk cache. Returns True if successful."""
        try:
            if not os.path.exists(self._market_metadata_cache_path):
                return False
            import json
            with open(self._market_metadata_cache_path, "r") as f:
                cache = json.load(f)
            age = time.time() - cache.get("timestamp", 0)
            if age > self._MARKET_METADATA_CACHE_MAX_AGE_S:
                logging.info(f"Market metadata cache is stale ({age:.0f}s old), will refresh from REST")
                return False
            markets_data = cache.get("markets", {})
            if not markets_data:
                return False

            markets_dict = {}
            for symbol, m in markets_data.items():
                market_id = int(m["market_id"])
                price_decimals = int(m["price_decimals"])
                size_decimals = int(m["size_decimals"])
                price_tick = 10 ** (-price_decimals)
                amount_tick = 10 ** (-size_decimals)

                self.market_id_map[symbol] = market_id
                self.market_index_to_symbol[market_id] = symbol
                self.price_tick_sizes[symbol] = price_tick
                self.amount_tick_sizes[symbol] = amount_tick
                self.price_decimals[symbol] = price_decimals
                self.amount_decimals[symbol] = size_decimals

                markets_dict[symbol] = {
                    "symbol": symbol,
                    "id": str(market_id),
                    "base": m["base"],
                    "quote": m["quote"],
                    "active": True,
                    "swap": True,
                    "linear": True,
                    "type": "swap",
                    "precision": {"amount": amount_tick, "price": price_tick},
                    "limits": {
                        "amount": {"min": float(m["min_base"])},
                        "cost": {"min": float(m["min_quote"])},
                    },
                    "maker": 0.0,  # Lighter has zero fees
                    "taker": 0.0,
                    "contractSize": 1.0,
                    "info": {
                        "market_id": market_id,
                        "maxLeverage": self._max_leverage_overrides.get(
                            m["base"], int(m.get("maxLeverage", self._max_leverage_default))
                        ),
                        "price_decimals": price_decimals,
                        "size_decimals": size_decimals,
                    },
                }
            self.markets_dict = markets_dict
            logging.info(f"Loaded market metadata from cache ({len(markets_dict)} markets, {age:.0f}s old)")
            return True
        except Exception as e:
            logging.warning(f"Failed to load market metadata cache: {e}")
            return False

    def _save_market_metadata_cache(self):
        """Persist market metadata to disk to avoid REST call on next startup."""
        try:
            cache = {"timestamp": time.time(), "markets": {}}
            for symbol, mkt in self.markets_dict.items():
                info = mkt["info"]
                cache["markets"][symbol] = {
                    "symbol": symbol,
                    "base": mkt["base"],
                    "quote": mkt["quote"],
                    "market_id": info["market_id"],
                    "price_decimals": info["price_decimals"],
                    "size_decimals": info["size_decimals"],
                    "maxLeverage": info["maxLeverage"],
                    "min_base": mkt["limits"]["amount"]["min"],
                    "min_quote": mkt["limits"]["cost"]["min"],
                }
            import json
            os.makedirs(os.path.dirname(self._market_metadata_cache_path), exist_ok=True)
            with open(self._market_metadata_cache_path, "w") as f:
                json.dump(cache, f)
            logging.info(f"Saved market metadata cache ({len(cache['markets'])} markets)")
        except Exception as e:
            logging.warning(f"Failed to save market metadata cache: {e}")

    def symbol_to_coin(self, symbol):
        """Override: use local map instead of CCXT cache files."""
        if hasattr(self, "symbol_to_coin_map") and symbol in self.symbol_to_coin_map:
            return self.symbol_to_coin_map[symbol]
        # Fallback: extract base from standard symbol format
        if "/" in symbol:
            return symbol[: symbol.find("/")]
        return symbol

    def coin_to_symbol(self, coin, verbose=True):
        """Override: map coin name to Lighter symbol."""
        if coin == "":
            return ""
        if hasattr(self, "coin_to_symbol_map") and coin in self.coin_to_symbol_map:
            return self.coin_to_symbol_map[coin]
        # Try uppercased
        coin_upper = coin.upper()
        if hasattr(self, "coin_to_symbol_map") and coin_upper in self.coin_to_symbol_map:
            return self.coin_to_symbol_map[coin_upper]
        # Fallback: construct it
        symbol = f"{coin_upper}/{self.quote}:{self.quote}"
        if symbol in self.markets_dict:
            return symbol
        if verbose:
            logging.warning(f"coin_to_symbol: could not find symbol for coin {coin}")
        return ""

    def set_market_specific_settings(self):
        """Populate per-symbol market metadata from Lighter markets dict."""
        super().set_market_specific_settings()
        for symbol in self.markets_dict:
            elm = self.markets_dict[symbol]
            self.symbol_ids[symbol] = elm["id"]
            self.min_costs[symbol] = (
                10.0
                if elm["limits"]["cost"]["min"] is None
                else max(10.0, float(elm["limits"]["cost"]["min"]))
            )
            self.qty_steps[symbol] = elm["precision"]["amount"]
            self.min_qtys[symbol] = (
                self.qty_steps[symbol]
                if elm["limits"]["amount"]["min"] is None
                else float(elm["limits"]["amount"]["min"])
            )
            self.price_steps[symbol] = elm["precision"]["price"]
            self.c_mults[symbol] = elm["contractSize"]
            self.max_leverage[symbol] = int(elm["info"].get("maxLeverage", 50))

    def symbol_is_eligible(self, symbol):
        """All active Lighter markets are eligible."""
        return symbol in self.markets_dict and self.markets_dict[symbol].get("active", True)

    # --- Data fetching ---

    async def determine_utc_offset(self, verbose=True):
        """Determine server time offset using Lighter API."""
        try:
            # Use a lightweight REST call to measure offset
            root_api = lighter.RootApi(self.api_client)
            status = await root_api.status()
            server_time = float(status.timestamp) * 1000 if hasattr(status, "timestamp") else utc_ms()
            self.utc_offset = round((server_time - utc_ms()) / (1000 * 60 * 60)) * (
                1000 * 60 * 60
            )
        except Exception as e:
            logging.warning(f"Could not determine UTC offset from Lighter: {e}. Using 0.")
            self.utc_offset = 0
        if verbose:
            logging.info(f"Exchange time offset is {self.utc_offset}ms compared to UTC")

    async def _fetch_positions_and_balance(self):
        """Fetch positions and balance from Lighter AccountApi (internal).

        Returns (positions_list, balance_float) on success, or False on failure.
        """
        # Serve from WS cache if fresh
        now = time.monotonic()
        pos_fresh = (self._ws_positions_cache is not None
                     and (now - self._ws_positions_cache_ts) < self._ws_cache_max_age)
        bal_fresh = (self._ws_balance_cache is not None
                     and (now - self._ws_balance_cache_ts) < self._ws_cache_max_age)
        if pos_fresh and bal_fresh:
            return self._ws_positions_cache, self._ws_balance_cache

        if not await self._wait_for_read_slot():
            return False

        info = None
        try:
            info = await asyncio.wait_for(
                self.account_api.account(
                    by="index", value=str(self.account_index),
                ),
                timeout=30.0,
            )
            if not info or not hasattr(info, "accounts") or not info.accounts:
                logging.error("empty account response from Lighter")
                return False

            account_data = info.accounts[0]

            # Extract balance
            balance = self._get_balance_value_from_account_data(account_data)

            # Extract positions
            positions = []
            raw_positions = getattr(account_data, "positions", None)
            if raw_positions:
                if isinstance(raw_positions, dict):
                    items = raw_positions.items()
                elif isinstance(raw_positions, list):
                    items = enumerate(raw_positions)
                else:
                    items = []
                for key, pos_data in items:
                    try:
                        if isinstance(pos_data, dict):
                            pos_size = float(pos_data.get("position", 0))
                            pos_sign = int(pos_data.get("sign", 1))
                            entry_price = float(pos_data.get("avg_entry_price", pos_data.get("entry_price", 0)))
                            # Prefer market_id from position data over list index
                            raw_mid = pos_data.get("market_id")
                            market_id_key = int(raw_mid) if raw_mid is not None else (int(key) if isinstance(key, (int, str)) else key)
                        else:
                            pos_size = float(getattr(pos_data, "position", 0) or 0)
                            pos_sign = int(getattr(pos_data, "sign", 1) or 1)
                            entry_price = float(getattr(pos_data, "avg_entry_price", None) or getattr(pos_data, "entry_price", 0) or 0)
                            # Prefer market_id from position data over list index
                            raw_mid = getattr(pos_data, "market_id", None)
                            market_id_key = int(raw_mid) if raw_mid is not None else (int(key) if isinstance(key, (int, str)) else key)

                        if pos_size == 0.0:
                            continue

                        if pos_sign == 0:
                            logging.warning(f"position sign=0 with non-zero size={pos_size} for market {key}, skipping")
                            continue

                        if pos_sign == -1:
                            signed_size = -pos_size
                        elif pos_sign == 1:
                            signed_size = pos_size
                        else:
                            logging.warning(f"unexpected position sign={pos_sign} for market {key}, treating as positive")
                            signed_size = pos_size
                        symbol = self.market_index_to_symbol.get(market_id_key)
                        if not symbol:
                            continue

                        positions.append({
                            "symbol": symbol,
                            "position_side": "long" if signed_size > 0 else "short",
                            "size": signed_size,
                            "price": entry_price,
                        })
                    except Exception as e:
                        logging.error(f"error parsing position {key}: {e}")
                        logging.debug(traceback.format_exc())
                        continue

            self._reset_global_backoff()
            self._record_api_success()
            return positions, balance
        except Exception as e:
            if _is_transient_error(e):
                logging.warning(f"transient error fetching positions and balance ({type(e).__name__}); will retry automatically")
                logging.debug(traceback.format_exc())
                self._trigger_global_backoff()
            else:
                logging.error(f"error fetching positions and balance: {e}")
                if _detect_429(e):
                    self._trigger_global_backoff()
                if info:
                    print_async_exception(info)
                logging.debug(traceback.format_exc())
            return False

    async def fetch_positions(self):
        """Fetch positions from Lighter (v7.8.4 contract: returns list of position dicts)."""
        res = await self._fetch_positions_and_balance()
        if res is False:
            return None
        positions, balance = res
        # Cache balance so fetch_balance() can return it without a second API call
        self._last_fetched_balance = balance
        return positions

    async def fetch_balance(self):
        """Fetch balance from Lighter (v7.8.4 contract: returns float)."""
        # If we have a recently cached balance from fetch_positions, use it
        if self._last_fetched_balance is not None:
            bal = self._last_fetched_balance
            self._last_fetched_balance = None  # consume once
            return bal
        # Otherwise do a full fetch
        res = await self._fetch_positions_and_balance()
        if res is False:
            return None
        _positions, balance = res
        return balance

    async def fetch_open_orders(self, symbol=None):
        """Fetch open orders from Lighter API."""
        # Serve from WS cache if fresh
        now = time.monotonic()
        if (self._ws_open_orders_cache is not None
                and (now - self._ws_open_orders_cache_ts) < self._ws_cache_max_age):
            cached = self._ws_open_orders_cache
            if symbol:
                cached = [o for o in cached if o.get("symbol") == symbol]
            return sorted(cached, key=lambda x: x.get("timestamp", 0))

        try:
            auth = await self._get_auth_token()
            if symbol:
                markets_to_check = [symbol]
            else:
                # Only check relevant markets (approved + those with positions/open orders)
                active = set()
                if hasattr(self, "approved_coins_minus_ignored_coins"):
                    active.update(*self.approved_coins_minus_ignored_coins.values())
                if hasattr(self, "positions") and isinstance(self.positions, dict):
                    for s, sides in self.positions.items():
                        if any(float(sides.get(ps, {}).get("size", 0)) != 0 for ps in ("long", "short")):
                            active.add(s)
                if hasattr(self, "open_orders") and isinstance(self.open_orders, dict):
                    active.update([s for s, orders in self.open_orders.items() if orders])
                markets_to_check = [s for s in active if s in self.market_id_map]
                if not markets_to_check:
                    markets_to_check = list(self.market_id_map.keys())

            sem = asyncio.Semaphore(3)

            async def _fetch_for_market(sym):
                market_id = self.market_id_map[sym]
                orders_out = []
                if not await self._wait_for_read_slot():
                    return []
                try:
                    url = f"{self.base_url}/api/v1/accountActiveOrders"
                    params = {
                        "account_index": self.account_index,
                        "market_id": market_id,
                        "auth": auth,
                    }
                    session = await self._get_aiohttp_session()
                    async with sem:
                        async with session.get(url, params=params) as resp:
                            if resp.status == 429:
                                self._trigger_global_backoff()
                                return []
                            if resp.status != 200:
                                return []
                            data = await resp.json()
                    orders = data.get("orders", [])
                    for o in orders:
                        status = o.get("status", "")
                        if status not in ("open", "partial_filled", "pending", "in-progress"):
                            continue
                        is_ask = self._coerce_is_ask(o.get("is_ask", False))
                        side = "sell" if is_ask else "buy"
                        qty = float(o.get("remaining_base_amount", o.get("size", 0)))
                        if qty <= 0:
                            continue
                        order_dict = {
                            "id": str(o.get("order_index", o.get("client_order_index", ""))),
                            "symbol": sym,
                            "side": side,
                            "qty": qty,
                            "amount": qty,
                            "price": float(o.get("price", 0)),
                            "timestamp": int(o.get("timestamp", 0)),
                            "position_side": self.determine_pos_side(
                                {"symbol": sym, "side": side, "reduceOnly": bool(o.get("reduce_only", False))}
                            ),
                            "reduce_only": bool(o.get("reduce_only", False)),
                            "info": o,
                        }
                        # Track client->exchange order ID mapping
                        client_idx = o.get("client_order_index")
                        exchange_idx = o.get("order_index")
                        if client_idx is not None and exchange_idx is not None:
                            self._client_to_exchange_order_id[int(client_idx)] = int(exchange_idx)
                        orders_out.append(order_dict)
                except Exception as e:
                    if _detect_429(e):
                        self._trigger_global_backoff()
                    if _is_transient_error(e):
                        self._record_api_failure(e, f"fetch_open_orders({sym})")
                    else:
                        logging.error(f"error fetching open orders for {sym}: {e}")
                        logging.debug(traceback.format_exc())
                return orders_out

            results = await asyncio.wait_for(
                asyncio.gather(
                    *[_fetch_for_market(s) for s in markets_to_check],
                    return_exceptions=True,
                ),
                timeout=30.0,
            )
            all_orders = []
            for r in results:
                if isinstance(r, Exception):
                    logging.error(f"error in parallel fetch_open_orders: {r}")
                    continue
                all_orders.extend(r)
            # Fix 8: Populate known exchange order IDs for collision guard
            # Replace (not merge) to prune IDs of orders that closed while WS was down
            new_ids = {
                int(o['id']) for o in all_orders
                if o.get('id', '').isdigit()
            }
            self._known_exchange_order_ids = new_ids
            self._trim_order_id_mapping()

            # Orphan detection: find exchange orders not tracked locally
            tracked_exchange_ids = set(self._client_to_exchange_order_id.values())
            orphan_ids = new_ids - tracked_exchange_ids
            if orphan_ids:
                logging.warning(
                    f"fetch_open_orders: {len(orphan_ids)} orphan order(s) "
                    f"on exchange not tracked locally: {orphan_ids}"
                )
                self.execution_scheduled = True  # force immediate reconciliation

            return sorted(all_orders, key=lambda x: x["timestamp"])
        except Exception as e:
            if _is_transient_error(e):
                logging.warning(f"transient error fetching open orders ({type(e).__name__}); will retry automatically")
                logging.debug(traceback.format_exc())
            else:
                logging.error(f"error fetching open orders: {e}")
                logging.debug(traceback.format_exc())
            return False

    async def fetch_tickers(self):
        """Fetch tickers from Lighter order book data.

        Prefers WS ticker cache (from ticker/{MARKET_INDEX} channel).
        Falls back to REST order_books() with short TTL cache.
        """
        now = time.monotonic()

        # Prefer WS ticker cache if fresh
        if (self._ws_tickers_cache
                and (now - self._ws_tickers_cache_ts) < self._ws_cache_max_age):
            return self._ws_tickers_cache

        # REST fallback with TTL cache
        if self._tickers_cache is not None and (now - self._tickers_cache_ts) < self._tickers_cache_ttl:
            return self._tickers_cache

        if not await self._wait_for_read_slot():
            return False

        fetched = None
        try:
            fetched = await asyncio.wait_for(self.order_api.order_books(), timeout=30.0)
            tickers = {}
            for ob in fetched.order_books:
                symbol_base = ob.symbol.upper()
                symbol = f"{symbol_base}/{self.quote}:{self.quote}"
                if symbol not in self.markets_dict:
                    continue

                best_bid = float(ob.best_bid) if hasattr(ob, "best_bid") and ob.best_bid else 0.0
                best_ask = float(ob.best_ask) if hasattr(ob, "best_ask") and ob.best_ask else 0.0
                last = (best_bid + best_ask) / 2 if best_bid and best_ask else best_bid or best_ask

                tickers[symbol] = {
                    "bid": best_bid,
                    "ask": best_ask,
                    "last": last,
                }
            self._tickers_cache = tickers
            self._tickers_cache_ts = now
            self._reset_global_backoff()
            self._record_api_success()
            return tickers
        except Exception as e:
            if _is_transient_error(e):
                logging.warning(f"transient error fetching tickers ({type(e).__name__}); will retry automatically")
                logging.debug(traceback.format_exc())
                self._trigger_global_backoff()
            else:
                logging.error(f"error fetching tickers: {e}")
                if _detect_429(e):
                    self._trigger_global_backoff()
                if fetched:
                    print_async_exception(fetched)
                logging.debug(traceback.format_exc())
            return False

    async def update_tickers(self):
        """Override base: fetch tickers from Lighter (no CCXT)."""
        if not hasattr(self, "tickers"):
            self.tickers = {}
        tickers = await self.fetch_tickers()
        if tickers:
            for symbol in tickers:
                if tickers[symbol]["last"] is None:
                    if tickers[symbol]["bid"] is not None and tickers[symbol]["ask"] is not None:
                        tickers[symbol]["last"] = (
                            tickers[symbol]["bid"] + tickers[symbol]["ask"]
                        ) / 2
                else:
                    for oside in ["bid", "ask"]:
                        if tickers[symbol][oside] is None and tickers[symbol]["last"] is not None:
                            tickers[symbol][oside] = tickers[symbol]["last"]
            self.tickers = tickers

    async def fetch_ticker(self, symbol):
        """Fetch a single ticker — used by CandlestickManager."""
        tickers = await self.fetch_tickers()
        if tickers and symbol in tickers:
            return tickers[symbol]
        return {}

    _RESOLUTION_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

    async def _fetch_candles(self, symbol, timeframe, n_candles, since=None):
        """Shared helper for fetching candles from Lighter API.

        Uses direct HTTP instead of the SDK's CandlestickApi to work around
        a deserialization bug where duplicate field aliases cause OHLC values
        to be returned as None.
        """

        market_id = self.market_id_map[symbol]
        now_ms = int(utc_ms())
        resolution_s = self._RESOLUTION_SECONDS.get(timeframe, 60)
        start_ts = since if since else now_ms - resolution_s * n_candles * 1000
        # Lighter API returns the last count_back candles within the window,
        # so cap the window to 500 candles (API max) to ensure forward pagination works.
        api_max = 500
        capped = min(n_candles, api_max)
        end_ts = min(now_ms, int(start_ts) + resolution_s * capped * 1000)

        if not await self._wait_for_read_slot():
            return []

        params = {
            "market_id": market_id,
            "resolution": timeframe,
            "start_timestamp": int(start_ts),
            "end_timestamp": int(end_ts),
            "count_back": capped,
        }
        try:
            session = await self._get_aiohttp_session()
            async with session.get(
                f"{self.base_url}/api/v1/candles", params=params
            ) as resp:
                if resp.status == 429:
                    self._trigger_global_backoff()
                    return []
                data = await resp.json()
        except Exception as e:
            if _detect_429(e):
                self._trigger_global_backoff()
            raise
        return self._parse_candles_payload(data)

    async def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, **kwargs):
        """Fetch OHLCV candles from Lighter CandlestickApi."""
        try:
            if symbol not in self.market_id_map:
                logging.error(f"fetch_ohlcv: unknown symbol {symbol}")
                return []

            n_candles = limit if limit else 480
            try:
                candles = await self._fetch_candles(symbol, timeframe, n_candles, since)
                if candles:
                    return candles
            except Exception as direct_error:
                logging.warning(f"direct candle fetch failed for {symbol}: {direct_error}")
                logging.debug(traceback.format_exc())

            return await self._fetch_candles_via_sdk(symbol, timeframe, n_candles, since)
        except Exception as e:
            if _is_transient_error(e):
                self._record_api_failure(e, f"fetch_ohlcv({symbol})")
            else:
                logging.error(f"error fetching ohlcv for {symbol}: {e}")
                logging.debug(traceback.format_exc())
            return []

    async def fetch_ohlcvs_1m(self, symbol, since=None, limit=None, **kwargs):
        """Fetch 1m candles for EMA warmup."""
        try:
            if symbol not in self.market_id_map:
                return []
            n_candles = 5000 if limit is None else limit
            try:
                candles = await self._fetch_candles(symbol, "1m", n_candles, since)
                if candles:
                    return candles
            except Exception as direct_error:
                logging.warning(f"direct 1m candle fetch failed for {symbol}: {direct_error}")
                logging.debug(traceback.format_exc())

            return await self._fetch_candles_via_sdk(symbol, "1m", n_candles, since)
        except Exception as e:
            if _is_transient_error(e):
                self._record_api_failure(e, f"fetch_ohlcvs_1m({symbol})")
            else:
                logging.error(f"error fetching ohlcvs_1m for {symbol}: {e}")
                logging.debug(traceback.format_exc())
            return []

    async def fetch_pnls(self, start_time=None, end_time=None, limit=None):
        """Fetch PnL / trade history from Lighter using /api/v1/trades.

        Computes per-trade realized PnL from the exchange's position tracking
        data (position_size_before, entry_quote_before). Falls back to local
        reconstruction if exchange position data is unavailable.
        """
        # When limit is None, paginate until start_time is reached (up to 50 pages)
        uncapped = limit is None
        if limit is None:
            limit = 5000
        if not await self._wait_for_read_slot():
            return []
        try:
            auth = await self._get_auth_token()
            url = f"{self.base_url}/api/v1/trades"

            # Collect trades, paginating as needed
            all_trades = []
            cursor = None
            max_pages = 50 if uncapped else max(1, (limit + 99) // 100)

            for _ in range(max_pages):
                params = {
                    "account_index": self.account_index,
                    "auth": auth,
                    "sort_by": "timestamp",
                    "sort_dir": "desc",
                    "limit": min(100, limit - len(all_trades)),
                }
                if cursor:
                    params["cursor"] = cursor

                session = await self._get_aiohttp_session()
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        self._trigger_global_backoff()
                        break
                    if resp.status != 200:
                        logging.error(f"error fetching pnls: status {resp.status}")
                        break
                    data = await resp.json()

                trades = data.get("trades", [])
                if not trades:
                    break

                found_start = False
                for t in trades:
                    ts = int(t.get("timestamp", 0))
                    if end_time and ts > int(end_time):
                        continue
                    if start_time and ts < int(start_time):
                        found_start = True
                        break
                    all_trades.append(t)

                if found_start or not data.get("next_cursor"):
                    break
                cursor = data.get("next_cursor")
                if len(all_trades) >= limit:
                    break

            # Process trades into PnL entries
            pnls = []
            has_position_data = False
            for t in all_trades:
                trade_id = str(t.get("trade_id", f"unk_{int(time.time_ns())}"))
                market_id = int(t.get("market_id", -1))
                symbol = self.market_index_to_symbol.get(market_id, "")
                ts = int(t.get("timestamp", 0))
                trade_size = float(t.get("size", 0))
                trade_price = float(t.get("price", 0))

                # Determine our side (ask = sell, bid = buy)
                ask_acct = int(t.get("ask_account_id", -1))
                we_are_ask = (ask_acct == self.account_index)
                side = "sell" if we_are_ask else "buy"

                # Determine our role (maker or taker)
                is_maker_ask = t.get("is_maker_ask", False)
                if we_are_ask:
                    we_are_maker = bool(is_maker_ask)
                else:
                    we_are_maker = not bool(is_maker_ask)

                role = "maker" if we_are_maker else "taker"
                pos_before = float(t.get(f"{role}_position_size_before", 0))
                entry_quote = float(t.get(f"{role}_entry_quote_before", 0))

                # Compute PnL from exchange position data
                computed_pnl = 0.0
                trade_delta = -trade_size if we_are_ask else trade_size

                if pos_before != 0.0 and entry_quote != 0.0:
                    has_position_data = True
                    avg_entry = entry_quote / pos_before
                    # Reducing position -> realized PnL
                    if (pos_before > 0 and trade_delta < 0) or (pos_before < 0 and trade_delta > 0):
                        close_qty = min(abs(trade_delta), abs(pos_before))
                        if pos_before > 0:
                            computed_pnl = close_qty * (trade_price - avg_entry)
                        else:
                            computed_pnl = close_qty * (avg_entry - trade_price)

                # Derive position_side from position state.
                # When a fill fully closes a position (pos_after == 0), the fill
                # belongs to the side that was closed, so use pos_before's sign.
                pos_after = pos_before + trade_delta
                if pos_after > 0:
                    position_side = "long"
                elif pos_after < 0:
                    position_side = "short"
                else:
                    position_side = "long" if pos_before > 0 else "short"

                pnls.append({
                    "id": trade_id,
                    "symbol": symbol,
                    "timestamp": ts,
                    "pnl": computed_pnl,
                    "position_side": position_side,
                    "side": side,
                    "qty": trade_size,
                    "price": trade_price,
                })

            pnls = sorted(pnls, key=lambda x: x["timestamp"])

            # Fallback: reconstruct PnL if exchange position data was absent
            if not has_position_data and pnls:
                net_pos = 0.0
                avg_entry = 0.0
                for p in pnls:
                    qty = p["qty"]
                    price = p["price"]
                    trade_qty = qty if p["side"] == "buy" else -qty
                    new_pos = net_pos + trade_qty
                    computed_pnl = 0.0

                    if net_pos == 0.0:
                        avg_entry = price
                    elif (net_pos > 0 and trade_qty < 0) or (net_pos < 0 and trade_qty > 0):
                        close_qty = min(abs(trade_qty), abs(net_pos))
                        if net_pos > 0:
                            computed_pnl = close_qty * (price - avg_entry)
                        else:
                            computed_pnl = close_qty * (avg_entry - price)
                        if new_pos != 0.0 and ((new_pos > 0) != (net_pos > 0)):
                            avg_entry = price
                    else:
                        total_cost = abs(net_pos) * avg_entry + abs(trade_qty) * price
                        if new_pos != 0.0:
                            avg_entry = total_cost / abs(new_pos)

                    prev_net_pos = net_pos
                    net_pos = new_pos
                    p["pnl"] = computed_pnl

                    if net_pos > 0:
                        p["position_side"] = "long"
                    elif net_pos < 0:
                        p["position_side"] = "short"
                    else:
                        p["position_side"] = "long" if prev_net_pos > 0 else "short"

            return pnls
        except Exception as e:
            if _detect_429(e):
                self._trigger_global_backoff()
            if _is_transient_error(e):
                self._record_api_failure(e, "fetch_pnls")
            else:
                logging.error(f"error fetching pnls: {e}")
                logging.debug(traceback.format_exc())
            return []

    # --- Order execution ---

    async def execute_order(self, order: dict) -> dict:
        """Place an order on Lighter via SignerClient.

        Uses high-level create_order() which manages nonces internally.
        Contrast with execute_cancellation() which uses low-level sign + send_tx
        for explicit nonce control — this is intentional, as create_order() handles
        nonce lifecycle automatically while cancel requires manual nonce management.
        """
        try:
            symbol = order["symbol"]
            market_index = self._symbol_to_market_index(symbol)
            is_ask = order["side"] == "sell"
            price = self._get_market_execution_price(order)
            qty = abs(order["qty"])
            raw_price = self._to_raw_price(price, symbol)
            raw_amount = self._to_raw_amount(qty, symbol)
            client_order_id = self._generate_client_order_id()

            # Determine order type / time in force
            normalized = self._normalize_market_order_for_lighter(order)
            if normalized is None:
                return {}
            order_type, tif = normalized

            # Rate limiting gate
            if not await self._wait_for_write_slot(op_count=1, cancel_only=False):
                logging.warning(f"execute_order: rate limit gate blocked — skipping {symbol}")
                return {}

            reduce_only = bool(order.get("reduce_only", False))

            async with self._sdk_write_lock:
                tx, tx_hash, err = await asyncio.wait_for(
                    self.lighter_client.create_order(
                        market_index=market_index,
                        client_order_index=client_order_id,
                        base_amount=raw_amount,
                        price=raw_price,
                        is_ask=is_ask,
                        order_type=order_type,
                        time_in_force=tif,
                        reduce_only=reduce_only,
                    ),
                    timeout=30.0,
                )

            if err:
                logging.error(f"error creating order on Lighter: {err}")
                self._handle_nonce_error(err)
                # Only count truly unexpected errors toward circuit breaker.
                # Quota, 429/rate-limit, and nonce errors are transient and
                # should not trigger the rejection pause.
                if not _is_transient_error(err) and not _is_quota_error(err):
                    self._consecutive_rejections += 1
                    if self._consecutive_rejections >= 5:
                        self._rejection_pause_until = time.monotonic() + 60.0
                        logging.error(f"circuit breaker: {self._consecutive_rejections} consecutive rejections, pausing 60s")
                return {}

            # Record successful send and extract quota
            self._record_ops_sent(1)
            self._reset_global_backoff()
            self._consecutive_rejections = 0  # Fix 4: Reset on success
            if hasattr(tx, "volume_quota_remaining"):
                self._update_volume_quota(getattr(tx, "volume_quota_remaining", None))

            executed = {
                "id": str(client_order_id),
                "symbol": symbol,
                "side": order["side"],
                "amount": qty,
                "price": price,
                "status": "open",
                "info": {
                    "tx_hash": tx_hash,
                    "client_order_index": client_order_id,
                    "filled": None,
                    "resting": order.get("type") != "market",
                },
            }
            return executed
        except Exception as e:
            logging.error(f"error executing order {order}: {e}")
            self._handle_nonce_error(e)
            logging.debug(traceback.format_exc())
            return {}

    async def execute_cancellation(self, order: dict) -> dict:
        """Cancel an order on Lighter.

        Uses low-level sign_cancel_order() + send_tx() with explicit nonce management.
        This differs from execute_order() which uses high-level create_order() because
        the cancel path requires manual nonce acquisition and acknowledgment for proper
        error handling and nonce recovery.
        """
        api_key_idx = self.api_key_index
        nonce_acquired = False
        try:
            symbol = order["symbol"]
            market_index = self._symbol_to_market_index(symbol)

            # We need the exchange order_index, not the client_order_index
            order_id = order["id"]
            exchange_order_id = None

            # Try to find from our mapping
            try:
                client_id = int(order_id)
                exchange_order_id = self._client_to_exchange_order_id.get(client_id)
            except (ValueError, TypeError):
                pass

            if exchange_order_id is None:
                # Fix 1: Only treat as exchange ID if it's in the known set
                try:
                    candidate = int(order_id)
                    if candidate in self._known_exchange_order_ids:
                        exchange_order_id = candidate
                    else:
                        logging.warning(f"order_id {order_id} not found in client mapping or known exchange IDs")
                        return {}
                except (ValueError, TypeError):
                    logging.error(f"cannot cancel order with non-numeric id: {order_id}")
                    logging.debug(traceback.format_exc())
                    return {}

            # Rate limiting gate (cancel-only uses shorter interval)
            if not await self._wait_for_write_slot(op_count=1, cancel_only=True):
                logging.warning(f"execute_cancellation: rate limit gate blocked — skipping {order_id}")
                return {}

            async with self._sdk_write_lock:
                nonce_info = self.lighter_client.nonce_manager.next_nonce()
                if isinstance(nonce_info, tuple):
                    api_key_idx, nonce = nonce_info
                else:
                    api_key_idx = self.api_key_index
                    nonce = nonce_info
                nonce_acquired = True

                sign_result = self.lighter_client.sign_cancel_order(
                    market_index=market_index,
                    order_index=exchange_order_id,
                    nonce=nonce,
                    api_key_index=api_key_idx,
                )
                if isinstance(sign_result, tuple) and len(sign_result) == 2:
                    tx_info, err = sign_result
                    tx_type = 15  # TX_TYPE_CANCEL_ORDER
                else:
                    tx_type, tx_info, _tx_hash, err = sign_result
                if err:
                    logging.error(f"error signing cancel: {err}")
                    self._acknowledge_nonce_failure(api_key_idx)
                    self._handle_nonce_error(err, api_key_idx)
                    return {}

                # Fix 5: Register cancel confirmation event BEFORE send_tx
                # so WS handler can find it even if response arrives immediately
                evt = asyncio.Event()
                self._order_cancel_events[exchange_order_id] = evt

                try:
                    resp = await asyncio.wait_for(
                        self.lighter_client.send_tx(
                            tx_type=tx_type, tx_info=tx_info
                        ),
                        timeout=30.0,
                    )
                    # Nonce consumed by successful send — don't roll back on later errors
                    nonce_acquired = False
                except Exception as send_err:
                    logging.error(f"error sending cancel tx: {send_err}")
                    logging.debug(traceback.format_exc())
                    self._acknowledge_nonce_failure(api_key_idx)
                    self._handle_nonce_error(send_err, api_key_idx)
                    self._order_cancel_events.pop(exchange_order_id, None)
                    return {}

            resp_code = _get_lighter_response_code(resp)
            if resp_code != 0:
                err_msg = _get_lighter_response_message(resp)
                logging.error(f"cancel order failed: {err_msg}")
                self._handle_nonce_error(err_msg, api_key_idx)
                self._order_cancel_events.pop(exchange_order_id, None)
                return {}

            # Record successful send and extract quota
            self._record_ops_sent(1)
            self._reset_global_backoff()
            if hasattr(resp, "volume_quota_remaining"):
                self._update_volume_quota(getattr(resp, "volume_quota_remaining", None))

            # Fix 5: Wait for WS cancel confirmation, with REST fallback
            cancel_confirmed = True
            try:
                await asyncio.wait_for(evt.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logging.debug(f"cancel confirmation timeout for {exchange_order_id}, checking REST")
                try:
                    open_orders = await self.fetch_open_orders(symbol)
                    if open_orders and isinstance(open_orders, list):
                        still_open = any(
                            str(o.get("id")) == str(exchange_order_id)
                            for o in open_orders
                        )
                        if still_open:
                            logging.warning(
                                f"cancel REST fallback: order {exchange_order_id} still open"
                            )
                            cancel_confirmed = False
                except Exception as rest_err:
                    logging.debug(f"cancel REST fallback check failed: {rest_err}")
            finally:
                self._order_cancel_events.pop(exchange_order_id, None)

            if not cancel_confirmed:
                return {}

            # Clean up order ID mapping only after cancellation is confirmed.
            try:
                client_id = int(order_id)
                self._client_to_exchange_order_id.pop(client_id, None)
            except (ValueError, TypeError):
                pass

            executed = {"id": order_id, "symbol": symbol, "status": "success"}
            return executed
        except Exception as e:
            logging.error(f"error cancelling order {order}: {e}")
            if nonce_acquired:
                self._acknowledge_nonce_failure(api_key_idx)
            self._handle_nonce_error(e, api_key_idx)
            logging.debug(traceback.format_exc())
            return {}

    def did_create_order(self, executed) -> bool:
        """Check if order creation was successful."""
        try:
            if not executed:
                return False
            if "error" in executed:
                return False
            return "id" in executed and executed["id"] is not None
        except Exception:
            return False

    def did_cancel_order(self, executed, order=None) -> bool:
        """Check if order cancellation was successful."""
        try:
            if isinstance(executed, list) and len(executed) == 1:
                return self.did_cancel_order(executed[0])
            if not executed:
                return False
            return executed.get("status") == "success"
        except Exception:
            return False

    async def _sign_and_send_batch(self, ops, cancel_only=False):
        """Sign and send multiple create/cancel ops in one API call.

        Each op dict must include ``"action"`` (``"create"`` or ``"cancel"``)
        plus the fields needed by execute_order / execute_cancellation.
        Returns a list of result dicts (one per input op, empty dict on failure).
        """
        if not ops:
            return []

        original_count = len(ops)
        results = [{} for _ in range(original_count)]

        # Rate limiting gate (counts all ops)
        if not await self._wait_for_write_slot(op_count=len(ops), cancel_only=cancel_only):
            logging.warning(f"batch: rate limit gate blocked — skipping {len(ops)} ops")
            return results

        # Free-slot mode: quota exhausted, only send 1 op via single REST
        free_slot_mode = (
            self._volume_quota_remaining is not None
            and self._volume_quota_remaining <= 0
        )
        if free_slot_mode:
            ops = ops[:1]
            logging.info("batch: free-slot mode (quota=0), sending 1 op via REST")

        tx_types = []
        tx_infos = []
        signed_ops = []      # (original_index, op, metadata_dict)
        signed_nonces = []   # api_key_index per signed op — for rollback

        resp = None
        async with self._sdk_write_lock:
            # -- Phase 1: acquire nonces and sign each op --
            for i, op in enumerate(ops):
                nonce_info = self.lighter_client.nonce_manager.next_nonce()
                if isinstance(nonce_info, tuple):
                    api_key_idx, nonce = nonce_info
                else:
                    api_key_idx = self.api_key_index
                    nonce = nonce_info

                action = op.get("action", "create")
                err = None
                tx_type = None
                tx_info = None
                meta = {}

                try:
                    if action == "create":
                        symbol = op["symbol"]
                        qty = abs(op["qty"])
                        min_qty = self.min_qtys.get(symbol, 0)
                        if qty <= 0 or qty < min_qty:
                            logging.info(f"Skipping order {symbol} qty={qty} (min={min_qty})")
                            results[i] = {}
                            continue
                        market_index = self._symbol_to_market_index(symbol)
                        is_ask = op["side"] == "sell"
                        price = self._get_market_execution_price(op)
                        raw_price = self._to_raw_price(price, symbol)
                        raw_amount = self._to_raw_amount(qty, symbol)
                        client_order_id = self._generate_client_order_id()

                        normalized = self._normalize_market_order_for_lighter(op)
                        if normalized is None:
                            results[i] = {}
                            self._acknowledge_nonce_failure(api_key_idx)
                            continue
                        order_type, tif = normalized

                        reduce_only = bool(op.get("reduce_only", False))

                        sign_result = self.lighter_client.sign_create_order(
                            market_index=market_index,
                            client_order_index=client_order_id,
                            base_amount=raw_amount,
                            price=raw_price,
                            is_ask=is_ask,
                            order_type=order_type,
                            time_in_force=tif,
                            nonce=nonce,
                            api_key_index=api_key_idx,
                            reduce_only=reduce_only,
                        )

                        if isinstance(sign_result, tuple) and len(sign_result) == 2:
                            tx_info, err = sign_result
                            tx_type = 14  # TX_TYPE_CREATE_ORDER
                        else:
                            tx_type, tx_info, _tx_hash, err = sign_result

                        meta = {
                            "client_order_id": client_order_id,
                            "symbol": symbol,
                            "side": op["side"],
                            "qty": qty,
                            "price": price,
                        }

                    elif action == "cancel":
                        symbol = op["symbol"]
                        market_index = self._symbol_to_market_index(symbol)
                        order_id = op["id"]

                        exchange_order_id = None
                        try:
                            client_id = int(order_id)
                            exchange_order_id = self._client_to_exchange_order_id.get(client_id)
                        except (ValueError, TypeError):
                            pass
                        if exchange_order_id is None:
                            try:
                                candidate = int(order_id)
                                if candidate in self._known_exchange_order_ids:
                                    exchange_order_id = candidate
                                else:
                                    logging.warning(
                                        f"batch: order_id {order_id} not found in client mapping or known exchange IDs"
                                    )
                                    self._acknowledge_nonce_failure(api_key_idx)
                                    continue
                            except (ValueError, TypeError):
                                logging.error(
                                    f"batch: cannot cancel non-numeric order id: {order_id}"
                                )
                                logging.debug(traceback.format_exc())
                                self._acknowledge_nonce_failure(api_key_idx)
                                continue

                        sign_result = self.lighter_client.sign_cancel_order(
                            market_index=market_index,
                            order_index=exchange_order_id,
                            nonce=nonce,
                            api_key_index=api_key_idx,
                        )

                        if isinstance(sign_result, tuple) and len(sign_result) == 2:
                            tx_info, err = sign_result
                            tx_type = 15  # TX_TYPE_CANCEL_ORDER
                        else:
                            tx_type, tx_info, _tx_hash, err = sign_result

                        meta = {"order_id": order_id, "symbol": symbol}

                    else:
                        logging.error(f"batch: unknown action '{action}'")
                        self._acknowledge_nonce_failure(api_key_idx)
                        continue

                except Exception as sign_ex:
                    logging.warning(f"batch sign exception for {action}: {sign_ex}")
                    self._acknowledge_nonce_failure(api_key_idx)
                    continue

                if err:
                    logging.warning(f"batch sign error for {action}: {err}")
                    logging.debug(traceback.format_exc())
                    self._acknowledge_nonce_failure(api_key_idx)
                    continue

                tx_types.append(int(tx_type))
                tx_infos.append(tx_info)
                signed_ops.append((i, op, meta))
                signed_nonces.append(api_key_idx)

            if not tx_types:
                logging.warning("batch: all ops failed signing; nothing to send")
                return results

            # -- Phase 2: send --
            try:
                if free_slot_mode and len(tx_types) == 1:
                    # Free-slot mode: single REST send_tx (0 quota cost)
                    resp = await asyncio.wait_for(
                        self.lighter_client.send_tx(
                            tx_type=tx_types[0], tx_info=tx_infos[0]
                        ),
                        timeout=30.0,
                    )
                else:
                    # Try WS first (bypasses 200 msg/min REST rate limit)
                    ws_resp = None
                    if self._tx_ws is not None and self._tx_ws.is_connected:
                        ws_resp = await asyncio.wait_for(
                            self._tx_ws.send_batch(tx_types, tx_infos),
                            timeout=30.0,
                        )

                    if ws_resp is not None:
                        resp = _WsBatchResponse(ws_resp)
                    else:
                        resp = await asyncio.wait_for(
                            self.lighter_client.send_tx_batch(tx_types, tx_infos),
                            timeout=30.0,
                        )
            except Exception as send_err:
                logging.error(f"batch send error: {send_err}")
                logging.debug(traceback.format_exc())
                # Acknowledge all signed nonces before hard-refresh to keep
                # nonce manager's internal counter consistent.
                if hasattr(self.lighter_client, "nonce_manager"):
                    for aki in signed_nonces:
                        self.lighter_client.nonce_manager.acknowledge_failure(aki)
                    self.lighter_client.nonce_manager.hard_refresh_nonce(self.api_key_index)
                self.execution_scheduled = True  # force reconciliation on next cycle
                self._handle_nonce_error(send_err)
                return results

        # -- Phase 3: process response (outside lock) --
        resp_code = _get_lighter_response_code(resp)
        if resp_code != 0:
            err_msg = _get_lighter_response_message(resp)
            err_lower = err_msg.lower() if isinstance(err_msg, str) else ""
            logging.error(f"batch send failed (code={resp_code}): {err_msg}")
            # Distinguish error types for proper recovery (matching reference impl)
            if _is_quota_error(err_msg):
                self._update_volume_quota(0)
                if hasattr(self.lighter_client, "nonce_manager"):
                    for aki in signed_nonces:
                        self.lighter_client.nonce_manager.acknowledge_failure(aki)
                    self.lighter_client.nonce_manager.hard_refresh_nonce(self.api_key_index)
            elif "429" in err_lower or "too many" in err_lower:
                self._trigger_global_backoff()
                if hasattr(self.lighter_client, "nonce_manager"):
                    for aki in signed_nonces:
                        self.lighter_client.nonce_manager.acknowledge_failure(aki)
            elif "nonce" in err_lower:
                if hasattr(self.lighter_client, "nonce_manager"):
                    seen_keys = set()
                    for aki in signed_nonces:
                        if aki not in seen_keys:
                            self.lighter_client.nonce_manager.hard_refresh_nonce(aki)
                            seen_keys.add(aki)
            else:
                self._handle_nonce_error(err_msg)
                # Only truly unexpected errors count toward circuit breaker
                self._consecutive_rejections += 1
                if self._consecutive_rejections >= 5:
                    self._rejection_pause_until = time.monotonic() + 60.0
                    logging.error(f"circuit breaker: {self._consecutive_rejections} consecutive batch rejections, pausing 60s")
            return results

        # Success
        self._record_ops_sent(len(signed_ops))
        self._reset_global_backoff()
        if hasattr(resp, "volume_quota_remaining"):
            self._update_volume_quota(getattr(resp, "volume_quota_remaining", None))

        tx_hashes = getattr(resp, "tx_hash", []) or []
        for idx, (orig_i, op, meta) in enumerate(signed_ops):
            tx_hash = tx_hashes[idx] if idx < len(tx_hashes) else None
            action = op.get("action", "create")

            if action == "create":
                results[orig_i] = {
                    "id": str(meta["client_order_id"]),
                    "symbol": meta["symbol"],
                    "side": meta["side"],
                    "amount": meta["qty"],
                    "price": meta["price"],
                    "status": "open",
                    "info": {
                        "tx_hash": tx_hash,
                        "client_order_index": meta["client_order_id"],
                        "filled": None,
                        "resting": op.get("type") != "market",
                    },
                }
            elif action == "cancel":
                try:
                    client_id = int(meta["order_id"])
                    self._client_to_exchange_order_id.pop(client_id, None)
                except (ValueError, TypeError):
                    pass
                results[orig_i] = {
                    "id": meta["order_id"],
                    "symbol": meta["symbol"],
                    "status": "success",
                }

        return results

    async def execute_orders(self, orders):
        """Execute order creates individually (not batched) for independent failure handling."""
        if not orders:
            return []
        if len(orders) == 1:
            return [await self.execute_order(orders[0])]
        sem = asyncio.Semaphore(3)

        async def _create_with_sem(order):
            async with sem:
                return await self.execute_order(order)

        return list(await asyncio.gather(*[_create_with_sem(o) for o in orders]))

    async def execute_cancellations(self, orders):
        """Override base: cancel individually via send_tx (free, 0 quota cost).

        Batching cancels via send_tx_batch costs N quota per N ops, but individual
        L2CancelOrder via send_tx() costs 0 quota. Uses concurrent execution with
        a semaphore to overlap network I/O while respecting rate limits.
        """
        if not orders:
            return []
        if len(orders) == 1:
            return [await self.execute_cancellation(orders[0])]
        # Fix 3: Run up to 3 concurrent cancels
        sem = asyncio.Semaphore(3)

        async def _cancel_with_sem(order):
            async with sem:
                return await self.execute_cancellation(order)

        return list(await asyncio.gather(*[_cancel_with_sem(o) for o in orders]))

    def get_order_execution_params(self, order: dict) -> dict:
        """Return Lighter-specific order params."""
        params = {
            "reduceOnly": order.get("reduce_only", False),
        }
        tif = require_live_value(self.config, "time_in_force")
        if tif == "post_only":
            params["timeInForce"] = "post_only"
        else:
            params["timeInForce"] = "good_till_cancelled"
        return params

    # --- Exchange config ---

    async def update_exchange_config(self):
        """No-op for Lighter (no hedge mode to set)."""
        pass

    async def update_exchange_config_by_symbols(self, symbols):
        """Set leverage for each symbol on Lighter."""
        for symbol in symbols:
            try:
                market_id = self.market_id_map.get(symbol)
                if market_id is None:
                    continue
                if not await self._wait_for_write_slot(op_count=1, cancel_only=False):
                    continue
                leverage = int(
                    min(
                        self.max_leverage.get(symbol, 50),
                        self.config_get(["live", "leverage"], symbol=symbol),
                    )
                )
                async with self._sdk_write_lock:
                    tx, response, err = await self.lighter_client.update_leverage(
                        market_id,
                        self.lighter_client.CROSS_MARGIN_MODE,
                        leverage,
                    )
                if err:
                    logging.error(f"{symbol}: error setting leverage: {err}")
                else:
                    self._record_ops_sent(1)
                    self._reset_global_backoff()
                    logging.info(f"{symbol}: set leverage to {leverage}x cross")
            except Exception as e:
                logging.error(f"{symbol}: error setting leverage: {e}")
                logging.debug(traceback.format_exc())
                if _detect_429(e):
                    self._trigger_global_backoff()

    # --- WebSocket ---

    async def _subscribe_ws_channels(self, ws, auth):
        """Subscribe to active market channels with the given auth token."""
        active_market_ids = set()
        for sym in self.active_symbols:
            if sym in self.market_id_map:
                active_market_ids.add(self.market_id_map[sym])
        # Fallback: use approved coins from config, not all 168 markets
        if not active_market_ids and hasattr(self, "approved_coins_minus_ignored_coins"):
            for pside_coins in self.approved_coins_minus_ignored_coins.values():
                for sym in pside_coins:
                    if sym in self.market_id_map:
                        active_market_ids.add(self.market_id_map[sym])
        if not active_market_ids:
            logging.warning("WS subscribe: no active symbols or approved coins — skipping market channels")
        success_count = 0

        for mid in active_market_ids:
            try:
                sub_msg = _json_dumps({
                    "type": "subscribe",
                    "channel": f"account_orders/{mid}/{self.account_index}",
                    "auth": auth,
                })
                await ws.send(sub_msg)
                success_count += 1
            except Exception as e:
                logging.error(f"WS subscription failed for account_orders/{mid}: {e}")
                logging.debug(traceback.format_exc())

        try:
            await ws.send(_json_dumps({
                "type": "subscribe",
                "channel": f"account_all/{self.account_index}",
                "auth": auth,
            }))
            success_count += 1
        except Exception as e:
            logging.error(f"WS subscription failed for account_all: {e}")
            logging.debug(traceback.format_exc())

        try:
            await ws.send(_json_dumps({
                "type": "subscribe",
                "channel": f"user_stats/{self.account_index}",
                "auth": auth,
            }))
            success_count += 1
        except Exception as e:
            logging.error(f"WS subscription failed for user_stats: {e}")
            logging.debug(traceback.format_exc())

        # Subscribe to ticker channels for real-time BBO (public, no auth needed)
        for mid in active_market_ids:
            try:
                await ws.send(_json_dumps({
                    "type": "subscribe",
                    "channel": f"ticker/{mid}",
                }))
                success_count += 1
            except Exception as e:
                logging.error(f"WS subscription failed for ticker/{mid}: {e}")
                logging.debug(traceback.format_exc())

        if success_count == 0:
            raise ConnectionError("All WS subscription sends failed — triggering reconnect")
        logging.info("WS subscriptions sent: %d succeeded", success_count)

    async def _unsubscribe_ws_channels(self, ws):
        """Unsubscribe from active market channels before re-subscribing."""
        active_market_ids = set()
        for sym in self.active_symbols:
            if sym in self.market_id_map:
                active_market_ids.add(self.market_id_map[sym])
        if not active_market_ids and hasattr(self, "approved_coins_minus_ignored_coins"):
            for pside_coins in self.approved_coins_minus_ignored_coins.values():
                for sym in pside_coins:
                    if sym in self.market_id_map:
                        active_market_ids.add(self.market_id_map[sym])

        for mid in active_market_ids:
            try:
                await ws.send(_json_dumps({
                    "type": "unsubscribe",
                    "channel": f"account_orders/{mid}/{self.account_index}",
                }))
            except Exception as e:
                logging.error(f"WS unsubscribe failed for account_orders/{mid}: {e}")
                logging.debug(traceback.format_exc())

        for channel in [
            f"account_all/{self.account_index}",
            f"user_stats/{self.account_index}",
        ]:
            try:
                await ws.send(_json_dumps({"type": "unsubscribe", "channel": channel}))
            except Exception as e:
                logging.error(f"WS unsubscribe failed for {channel}: {e}")
                logging.debug(traceback.format_exc())

        for mid in active_market_ids:
            try:
                await ws.send(_json_dumps({
                    "type": "unsubscribe",
                    "channel": f"ticker/{mid}",
                }))
            except Exception as e:
                logging.error(f"WS unsubscribe failed for ticker/{mid}: {e}")
                logging.debug(traceback.format_exc())

    async def _start_ws_early(self):
        """Start WS listener early during init_markets so caches populate before REST calls."""
        if not self.ws_enabled:
            return
        if hasattr(self, "maintainers") and "watch_orders" in getattr(self, "maintainers", {}):
            return  # already running
        logging.info("Starting WS early for cache warmup...")
        if not hasattr(self, "maintainers"):
            self.maintainers = {}
        self.maintainers["watch_orders"] = asyncio.create_task(self.watch_orders())
        # Wait for WS snapshots (positions + orders + balance) up to 5s
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            pos_ok = self._ws_positions_cache is not None
            bal_ok = self._ws_balance_cache is not None
            ord_ok = self._ws_open_orders_cache is not None
            ticker_ok = bool(self._ws_tickers_cache)
            if pos_ok and bal_ok and ord_ok and ticker_ok:
                logging.info("WS caches populated (incl. tickers) — warmup REST calls will be skipped")
                return
            await asyncio.sleep(0.2)
        logging.info("WS cache warmup timeout — will fall back to REST for missing data")

    async def watch_orders(self):
        """Monitor order updates via Lighter WebSocket.

        Subscribes to account_orders and account_all channels only (not order book).
        Order book data, stale order reconciliation, circuit breakers, and emergency
        close are handled by the Passivbot base class via REST polling.
        """
        import websockets

        ws_backoff = 5
        while True:
            try:
                if self.stop_websocket:
                    break

                auth = await self._get_auth_token()
                last_auth_refresh = time.time()
                self._ws_orders_snapshot_received = False  # reset for snapshot reconciliation
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=30,
                ) as ws:
                    await self._subscribe_ws_channels(ws, auth)

                    ws_backoff = 5  # reset backoff on successful connect
                    while not self.stop_websocket:
                        try:
                            # Refresh auth token every ~55 minutes (tokens expire at 60 min, 5-min margin)
                            if time.time() - last_auth_refresh > 3300:
                                try:
                                    auth = await self._get_auth_token()
                                    await self._unsubscribe_ws_channels(ws)
                                    await self._subscribe_ws_channels(ws, auth)
                                    last_auth_refresh = time.time()
                                    logging.info("WS auth token refreshed — re-subscribed on same connection")
                                except Exception as refresh_err:
                                    logging.error(f"WS auth re-subscribe failed, triggering reconnect: {refresh_err}")
                                    logging.debug(traceback.format_exc())
                                    break

                            # Account channels (account_orders, account_all) are event-driven;
                            # minutes of silence is normal on quiet markets. Use 300s timeout
                            # to avoid spurious reconnections that waste auth tokens.
                            raw = await asyncio.wait_for(ws.recv(), timeout=300)
                            data = _json_loads(raw)
                            if not isinstance(data, dict):
                                continue
                            self._ws_last_message_ms = utc_ms()
                            msg_type = data.get("type", "")

                            if msg_type == "ping":
                                await ws.send(_PONG_MSG)
                                continue

                            if msg_type == "subscribed":
                                channel = data.get("channel", "unknown")
                                logging.debug(f"WS subscription confirmed: {channel}")
                                continue

                            if msg_type.startswith("update/account_orders"):
                                self._handle_ws_order_update(data)
                            elif msg_type.startswith("update/account_all"):
                                self._handle_ws_account_update(data)
                            elif msg_type.startswith("update/user_stats"):
                                self._handle_ws_user_stats(data)
                            elif msg_type.startswith("update/ticker") or msg_type.startswith("subscribed/ticker"):
                                self._handle_ws_ticker_update(data)

                        except asyncio.TimeoutError:
                            try:
                                await ws.send(_json_dumps({"type": "ping"}))
                            except Exception:
                                break
                        except Exception as e:
                            logging.error(f"error in ws recv loop: {e}")
                            logging.debug(traceback.format_exc())
                            break

            except Exception as e:
                self._health_ws_reconnects += 1
                logging.error(f"websocket connection error: {e}")
                logging.debug(traceback.format_exc())
                if self.stop_websocket:
                    break
                jitter = ws_backoff * random.uniform(-0.2, 0.2)
                await asyncio.sleep(ws_backoff + jitter)
                ws_backoff = min(ws_backoff * 2, 60)  # exponential backoff with 60s cap

    def _handle_ws_order_update(self, data):
        """Parse WS order update and call handle_order_update.

        The first message after (re)connect is a snapshot: it contains all
        currently open orders.  We use it to clear stale local mappings for
        orders that no longer exist on the exchange.  Subsequent messages are
        incremental deltas.
        """
        try:
            orders = data.get("orders", [])
            if not orders:
                return
            # WS may send orders as a dict keyed by index — normalize to list
            if isinstance(orders, dict):
                orders = list(orders.values())
            # WS may send orders as list of strings or other non-dict types
            if orders and not isinstance(orders[0], dict):
                logging.debug(f"WS order update: unexpected format, skipping: {type(orders[0])}")
                return

            is_snapshot = not self._ws_orders_snapshot_received
            if is_snapshot:
                self._ws_orders_snapshot_received = True
                # Snapshot reconciliation: build set of exchange IDs present in
                # the snapshot and remove any local mappings not in it.
                snapshot_exchange_ids = set()
                for o in orders:
                    eidx = o.get("order_index")
                    if eidx is not None:
                        snapshot_exchange_ids.add(int(eidx))
                # Remove stale mappings not present in the snapshot
                stale_clients = [
                    cid for cid, eid in self._client_to_exchange_order_id.items()
                    if eid not in snapshot_exchange_ids
                ]
                for cid in stale_clients:
                    eid = self._client_to_exchange_order_id.pop(cid)
                    self._known_exchange_order_ids.discard(eid)
                if stale_clients:
                    logging.info(f"WS snapshot: cleared {len(stale_clients)} stale order mappings")

            parsed = []
            for o in orders:
                market_id = int(o.get("market_id", -1))
                symbol = self.market_index_to_symbol.get(market_id, "")
                is_ask = self._coerce_is_ask(o.get("is_ask", False))
                side = "sell" if is_ask else "buy"
                status = o.get("status", "").lower()

                # Track order ID mapping
                client_idx = o.get("client_order_index")
                exchange_idx = o.get("order_index")
                if client_idx is not None and exchange_idx is not None:
                    client_int = int(client_idx)
                    exchange_int = int(exchange_idx)
                    if status in ("filled", "cancelled", "canceled", "expired", "rejected"):
                        # Terminal status — remove mapping to prevent memory leak
                        self._client_to_exchange_order_id.pop(client_int, None)
                        # Fix 1: Remove from known exchange IDs
                        self._known_exchange_order_ids.discard(exchange_int)
                        # Fix 5: Fire cancel confirmation event
                        evt = self._order_cancel_events.pop(exchange_int, None)
                        if evt:
                            evt.set()
                    else:
                        self._client_to_exchange_order_id[client_int] = exchange_int
                        # Fix 1: Track known exchange order IDs
                        self._known_exchange_order_ids.add(exchange_int)
                        self._trim_order_id_mapping()

                order_dict = {
                    "id": str(exchange_idx or client_idx or ""),
                    "symbol": symbol,
                    "side": side,
                    "amount": float(o.get("remaining_base_amount", o.get("size", 0))),
                    "qty": float(o.get("remaining_base_amount", o.get("size", 0))),
                    "price": float(o.get("price", 0)),
                    "status": status,
                    "position_side": self.determine_pos_side(
                        {"symbol": symbol, "side": side, "reduceOnly": bool(o.get("reduce_only", False))}
                    ),
                    "timestamp": int(o.get("timestamp", 0)),
                }
                parsed.append(order_dict)

            # Cache open orders from WS data
            live_statuses = {"open", "partial_filled", "pending", "in-progress"}
            live_orders = [o for o in parsed if o.get("status", "") in live_statuses]
            if is_snapshot:
                self._ws_open_orders_cache = live_orders
                self._ws_open_orders_cache_ts = time.monotonic()
            elif self._ws_open_orders_cache is not None:
                update_ids = {o["id"] for o in parsed}
                self._ws_open_orders_cache = [
                    o for o in self._ws_open_orders_cache if o["id"] not in update_ids
                ] + live_orders
                self._ws_open_orders_cache_ts = time.monotonic()

            if parsed:
                self.handle_order_update(parsed)
        except Exception as e:
            logging.error(f"error handling ws order update: {e}")
            logging.debug(traceback.format_exc())

    def _handle_ws_account_update(self, data):
        """Parse WS account_all update for positions and cache them."""
        try:
            raw_positions = data.get("positions")
            if not raw_positions or not isinstance(raw_positions, dict):
                return
            positions = []
            for key, pos_data in raw_positions.items():
                try:
                    if not isinstance(pos_data, dict):
                        continue
                    pos_size = float(pos_data.get("position", 0))
                    pos_sign = int(pos_data.get("sign", 1))
                    entry_price = float(pos_data.get("avg_entry_price", pos_data.get("entry_price", 0)))
                    raw_mid = pos_data.get("market_id")
                    market_id_key = int(raw_mid) if raw_mid is not None else int(key)
                    if pos_size == 0.0 or pos_sign == 0:
                        continue
                    signed_size = -pos_size if pos_sign == -1 else pos_size
                    symbol = self.market_index_to_symbol.get(market_id_key)
                    if not symbol:
                        continue
                    positions.append({
                        "symbol": symbol,
                        "position_side": "long" if signed_size > 0 else "short",
                        "size": signed_size,
                        "price": entry_price,
                    })
                except Exception as e:
                    logging.error(f"error parsing WS position {key}: {e}")
                    logging.debug(traceback.format_exc())
            self._ws_positions_cache = positions
            self._ws_positions_cache_ts = time.monotonic()
            self.execution_scheduled = True
        except Exception as e:
            logging.error(f"error handling ws account update: {e}")
            logging.debug(traceback.format_exc())

    def _handle_ws_user_stats(self, data):
        """Parse WS user_stats update for real-time balance."""
        stats = data.get("stats", data)
        if not stats:
            return
        # Validate collateral if present
        pv = stats.get("collateral")
        if pv is not None:
            try:
                pv_float = float(pv)
                if pv_float < 0:
                    logging.warning(f"WS user_stats: rejecting negative collateral {pv_float}")
                    return
            except (ValueError, TypeError):
                logging.warning(f"WS user_stats: invalid collateral {pv!r}")
                return
        bal = self._get_balance_value_from_user_stats(stats)
        if bal is not None:
            try:
                bal_float = float(bal)
                if bal_float < 0:
                    logging.warning(f"WS user_stats: rejecting negative balance {bal_float}")
                    return
                if hasattr(self, "balance"):
                    self.balance = bal_float
                self._ws_balance_cache = bal_float
                self._ws_balance_cache_ts = time.monotonic()
            except (ValueError, TypeError):
                pass

    def _handle_ws_ticker_update(self, data):
        """Parse WS ticker update for best bid/ask."""
        try:
            # Extract market_id from channel or data
            channel = data.get("channel", "")
            market_id = None
            if "/" in channel:
                parts = channel.split("/")
                if len(parts) >= 2:
                    try:
                        market_id = int(parts[-1])
                    except ValueError:
                        pass
            if market_id is None:
                market_id = data.get("market_id")
            if market_id is None:
                return
            market_id = int(market_id)
            symbol = self.market_index_to_symbol.get(market_id)
            if not symbol:
                return

            # Log first ticker message for field verification
            if not hasattr(self, "_ws_ticker_sample_logged"):
                logging.info(f"WS ticker sample message: {data}")
                self._ws_ticker_sample_logged = True

            best_bid = float(data.get("best_bid", 0) or 0)
            best_ask = float(data.get("best_ask", 0) or 0)
            last = (best_bid + best_ask) / 2 if best_bid and best_ask else best_bid or best_ask

            self._ws_tickers_cache[symbol] = {
                "bid": best_bid,
                "ask": best_ask,
                "last": last,
            }
            self._ws_tickers_cache_ts = time.monotonic()
        except Exception as e:
            logging.error(f"error handling ws ticker update: {e}")
            logging.debug(traceback.format_exc())

    def determine_pos_side(self, order):
        """Determine position side for net-position mode (same as Hyperliquid)."""
        if order["symbol"] in self.positions:
            if self.positions[order["symbol"]]["long"]["size"] != 0.0:
                return "long"
            elif self.positions[order["symbol"]]["short"]["size"] != 0.0:
                return "short"
            else:
                return "long" if order["side"] == "buy" else "short"
        else:
            if "reduceOnly" in order:
                if order["side"] == "buy":
                    return "short" if order["reduceOnly"] else "long"
                if order["side"] == "sell":
                    return "long" if order["reduceOnly"] else "short"
            return "long" if order["side"] == "buy" else "short"

    # --- Lifecycle ---

    async def restart_bot(self):
        """Override: cca is None for Lighter, so close api_client instead."""
        logging.info("Initiating bot restart...")
        self.stop_signal_received = True
        self.stop_data_maintainers()
        if self._tx_ws is not None:
            try:
                await self._tx_ws.close()
            except Exception:
                pass
        if self.api_client:
            try:
                await self.api_client.close()
            except Exception:
                pass
        raise Exception("Bot will restart.")

    async def close(self):
        """Close Lighter clients and WS connections."""
        logging.info(f"Stopped data maintainers: {self.stop_data_maintainers()}")
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        if self._tx_ws is not None:
            try:
                await self._tx_ws.close()
            except Exception:
                pass
        if self._aiohttp_session and not self._aiohttp_session.closed:
            try:
                await self._aiohttp_session.close()
            except Exception:
                pass
        if self.api_client:
            try:
                await self.api_client.close()
            except Exception:
                pass

    async def start_data_maintainers(self):
        """Override: also initialize TxWebSocket for rate-limit-free transaction sending."""
        await super().start_data_maintainers()
        self._tx_ws = _TxWebSocket(self.ws_url)
        try:
            await self._tx_ws.connect()
            logging.info("TxWebSocket initialized successfully")
        except Exception as e:
            logging.warning("TxWebSocket init failed (will retry on first use): %s", e)

    def format_custom_id_single(self, order_type_id: int) -> str:
        formatted = super().format_custom_id_single(order_type_id)
        return formatted[: self.custom_id_max_length]
