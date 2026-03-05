"""
DryRunMixin: paper trading simulation overlay.

Mix this in before any real exchange bot class to intercept all private
API calls.  Public endpoints (OHLCV, tickers, market metadata) continue
to use the real exchange so the bot's signal logic is exercised against
live data.

Usage (handled automatically by setup_bot when live.dry_run is true):
    DryRunBot = type("DryRunBot", (DryRunMixin, ExchangeBot), {})
    bot = DryRunBot(config)
"""

import asyncio
import itertools
import logging

from utils import utc_ms

_dry_run_id_counter = itertools.count()


class DryRunMixin:
    """Intercept every private exchange call for in-memory paper trading."""

    # ------------------------------------------------------------------ #
    #  Paper-state helpers                                                 #
    # ------------------------------------------------------------------ #

    def _ensure_dry_run_state(self):
        if getattr(self, "_dry_run_initialized", False):
            return
        from config_utils import get_optional_config_value

        raw = get_optional_config_value(self.config, "live.dry_run_wallet", 10000.0)
        try:
            self._dry_run_balance = float(raw) if raw is not None else 10000.0
        except (TypeError, ValueError):
            self._dry_run_balance = 10000.0
        # {(symbol, pside): {"size": float, "price": float}}
        self._dry_run_positions = {}
        # {order_id: order_dict} — pending limit orders waiting to be matched
        self._dry_run_open_orders: dict = {}
        if not hasattr(self, "pnls"):
            self.pnls = []
        self._dry_run_initialized = True
        logging.info(
            f"[DRY RUN] paper wallet initialised at {self._dry_run_balance} USDC"
        )

    def _get_fee_rate(self, symbol: str, is_taker: bool = False) -> float:
        try:
            key = "taker" if is_taker else "maker"
            fee = self.markets_dict[symbol].get(key, 0.0)
            if fee:
                return fee
        except Exception:
            pass
        # Fallback: exchange-level defaults.
        default_key = "_default_taker_fee" if is_taker else "_default_maker_fee"
        return getattr(self, default_key, 0.0)

    def _get_dry_run_leverage(self) -> float:
        from config_utils import get_optional_config_value

        try:
            leverage = float(get_optional_config_value(self.config, "live.leverage", 10))
        except (TypeError, ValueError):
            leverage = 10.0
        return max(leverage, 1e-12)

    def _get_order_required_margin(self, order: dict, fill_price: float | None = None) -> float:
        qty = abs(order.get("qty", order.get("amount", 0.0)))
        if qty <= 0.0 or order.get("reduce_only", False):
            return 0.0
        symbol = order["symbol"]
        price = float(fill_price if fill_price is not None else order.get("price", 0.0))
        c_mult = self.c_mults.get(symbol, 1.0) if hasattr(self, "c_mults") else 1.0
        return (price * qty * c_mult) / self._get_dry_run_leverage()

    def _get_used_margin(self) -> float:
        used_margin = 0.0
        for (symbol, _pside), pos in self._dry_run_positions.items():
            size = abs(pos.get("size", 0.0))
            price = float(pos.get("price", 0.0))
            c_mult = self.c_mults.get(symbol, 1.0) if hasattr(self, "c_mults") else 1.0
            used_margin += (size * price * c_mult) / self._get_dry_run_leverage()
        return used_margin

    def _get_reserved_margin(self, exclude_order_id: str | None = None) -> float:
        reserved_margin = 0.0
        for order_id, order in self._dry_run_open_orders.items():
            if exclude_order_id is not None and order_id == exclude_order_id:
                continue
            reserved_margin += self._get_order_required_margin(order)
        return reserved_margin

    def _get_position_mark_price(self, symbol: str, pos: dict, mark_prices: dict | None = None) -> float:
        if mark_prices and mark_prices.get(symbol) is not None:
            return float(mark_prices[symbol])
        return float(pos.get("price", 0.0))

    def _get_dry_run_equity(self, mark_prices: dict | None = None) -> float:
        equity = self._dry_run_balance
        for (symbol, pside), pos in self._dry_run_positions.items():
            size = abs(pos.get("size", 0.0))
            if size == 0.0:
                continue
            entry_price = float(pos.get("price", 0.0))
            mark_price = self._get_position_mark_price(symbol, pos, mark_prices)
            c_mult = self.c_mults.get(symbol, 1.0) if hasattr(self, "c_mults") else 1.0
            if pside == "long":
                equity += (mark_price - entry_price) * size * c_mult
            else:
                equity += (entry_price - mark_price) * size * c_mult
        return equity

    def _get_available_margin(
        self, exclude_order_id: str | None = None, mark_prices: dict | None = None
    ) -> float:
        return (
            self._get_dry_run_equity(mark_prices=mark_prices)
            - self._get_used_margin()
            - self._get_reserved_margin(exclude_order_id=exclude_order_id)
        )

    async def _get_latest_dry_run_marks(self, extra_symbols=None) -> dict:
        symbols = {symbol for symbol, _pside in self._dry_run_positions}
        if extra_symbols:
            symbols.update(extra_symbols)
        if not symbols or not hasattr(self, "cm"):
            return {}
        try:
            prices = await self.cm.get_last_prices(symbols, max_age_ms=10_000)
            return {
                symbol: float(price)
                for symbol, price in prices.items()
                if price is not None
            }
        except Exception:
            return {}

    def _make_rejected_order_result(
        self, order: dict, reason: str, *, order_id=None, remaining=None
    ) -> dict:
        qty = abs(order.get("qty", order.get("amount", 0.0)))
        if remaining is None:
            remaining = qty
        return {
            "id": order_id,
            "symbol": order.get("symbol", ""),
            "side": order.get("side", ""),
            "position_side": order.get("position_side") or "long",
            "qty": qty,
            "price": float(order.get("price", 0.0) or 0.0),
            "amount": qty,
            "reduce_only": order.get("reduce_only", False),
            "timestamp": utc_ms(),
            "status": "rejected",
            "type": order.get("type", "limit"),
            "filled": 0.0,
            "remaining": remaining,
            "rejected": True,
            "reason": reason,
        }

    async def _check_margin_available(
        self,
        order: dict,
        fill_price: float,
        exclude_order_id: str | None = None,
        mark_prices: dict | None = None,
    ) -> tuple[bool, str, float, float]:
        required_margin = self._get_order_required_margin(order, fill_price=fill_price)
        fee_rate = self._get_fee_rate(symbol=order["symbol"], is_taker=order.get("type") == "market")
        qty = abs(order.get("qty", order.get("amount", 0.0)))
        c_mult = self.c_mults.get(order["symbol"], 1.0) if hasattr(self, "c_mults") else 1.0
        fee = fill_price * qty * c_mult * fee_rate
        if mark_prices is None:
            mark_prices = await self._get_latest_dry_run_marks({order["symbol"]})
        available_margin = self._get_available_margin(
            exclude_order_id=exclude_order_id, mark_prices=mark_prices
        )
        required_available = required_margin + fee
        if available_margin + 1e-12 >= required_available:
            return True, "", required_margin, fee
        reason = (
            f"insufficient margin: need {required_available:.6f}, "
            f"have {available_margin:.6f}"
        )
        return False, reason, required_margin, fee

    async def _dry_run_fill_order(
        self, order: dict, fill_price: float, mark_prices: dict | None = None
    ):
        """Apply a filled order to paper state and return a structured result."""
        symbol = order["symbol"]
        pside = order.get("position_side")
        if not pside:
            logging.warning(f"[DRY RUN] order missing position_side, defaulting to 'long': {order}")
            pside = "long"
        qty = abs(order.get("qty", order.get("amount", 0.0)))
        reduce_only = order.get("reduce_only", False)
        c_mult = self.c_mults.get(symbol, 1.0) if hasattr(self, "c_mults") else 1.0
        fee_rate = self._get_fee_rate(symbol, is_taker=order.get("type") == "market")

        key = (symbol, pside)

        if reduce_only:
            # Closing an existing position — clamp qty, realise PnL
            pos = self._dry_run_positions.get(key, {"size": 0.0, "price": 0.0})
            old_size = pos["size"]
            effective_qty = min(qty, old_size)
            if effective_qty <= 0.0:
                reason = f"no {pside} position available to reduce"
                logging.warning(f"[DRY RUN] rejecting reduce-only order: {reason} {order}")
                return self._make_rejected_order_result(order, reason, order_id=order.get("id"))
            if qty > old_size:
                logging.warning(
                    f"[DRY RUN] close {pside} {symbol}: close qty={qty} exceeds "
                    f"position size={old_size:.6f}; clamping to zero"
                )
            entry_price = pos["price"]
            fee = fill_price * effective_qty * c_mult * fee_rate
            self._dry_run_balance -= fee
            if pside == "long":
                pnl = (fill_price - entry_price) * effective_qty * c_mult
            else:
                pnl = (entry_price - fill_price) * effective_qty * c_mult
            self._dry_run_balance += pnl
            new_size = old_size - effective_qty  # always >= 0
            if new_size == 0.0:
                self._dry_run_positions.pop(key, None)
            else:
                self._dry_run_positions[key] = {"size": new_size, "price": entry_price}

            self.pnls.append(
                {
                    "id": f"dry_run_{next(_dry_run_id_counter)}",
                    "symbol": symbol,
                    "timestamp": float(utc_ms()),
                    "pnl": pnl - fee,
                    "position_side": pside,
                    "qty": effective_qty,
                    "price": fill_price,
                    "side": order["side"],
                }
            )
            logging.info(
                f"[DRY RUN] fill (close) {pside} {symbol} qty={effective_qty} @ {fill_price}"
                f"  pnl={pnl:.4f}  fee={fee:.4f}  balance={self._dry_run_balance:.2f}"
            )
            return {
                "id": order.get("id"),
                "symbol": symbol,
                "position_side": pside,
                "qty": qty,
                "filled_qty": effective_qty,
                "price": fill_price,
                "status": "filled",
                "fee": fee,
                "pnl": pnl,
                "balance_after": self._dry_run_balance,
                "rejected": False,
            }
        else:
            # Opening / adding to a position — update weighted average entry
            can_fill, reason, required_margin, fee = await self._check_margin_available(
                order,
                fill_price,
                exclude_order_id=order.get("id"),
                mark_prices=mark_prices,
            )
            if not can_fill:
                logging.warning(f"[DRY RUN] rejecting {pside} {symbol} entry: {reason}")
                return self._make_rejected_order_result(order, reason, order_id=order.get("id"))
            self._dry_run_balance -= fee
            pos = self._dry_run_positions.get(key, {"size": 0.0, "price": 0.0})
            old_size = pos["size"]
            new_size = old_size + qty
            if new_size > 0:
                new_price = (pos["price"] * old_size + fill_price * qty) / new_size
            else:
                new_price = fill_price
            self._dry_run_positions[key] = {"size": new_size, "price": new_price}
            logging.info(
                f"[DRY RUN] fill (entry) {pside} {symbol} qty={qty} @ {fill_price}"
                f"  fee={fee:.4f}  pos_size={new_size:.6f}  avg_price={new_price:.4f}"
                f"  balance={self._dry_run_balance:.2f}"
            )
            return {
                "id": order.get("id"),
                "symbol": symbol,
                "position_side": pside,
                "qty": qty,
                "filled_qty": qty,
                "price": fill_price,
                "status": "filled",
                "fee": fee,
                "required_margin": required_margin,
                "balance_after": self._dry_run_balance,
                "rejected": False,
            }

    async def _dry_run_match_orders(self):
        """Match pending limit orders against current market prices."""
        if not self._dry_run_open_orders:
            return
        if not hasattr(self, "cm"):
            return  # called before cm is ready; skip silently
        symbols = {o["symbol"] for o in self._dry_run_open_orders.values()}
        try:
            last_prices = await self.cm.get_last_prices(symbols, max_age_ms=10_000)
        except Exception:
            return
        filled_ids = []
        for order_id, order in list(self._dry_run_open_orders.items()):
            last_price = last_prices.get(order["symbol"])
            if not last_price:
                continue
            side = order["side"]        # "buy" or "sell"
            limit_price = order["price"]
            if (side == "buy" and last_price <= limit_price) or \
               (side == "sell" and last_price >= limit_price):
                fill_result = await self._dry_run_fill_order(
                    order, fill_price=limit_price, mark_prices=last_prices
                )
                if fill_result and not fill_result.get("rejected", False):
                    filled_ids.append(order_id)
        for oid in filled_ids:
            self._dry_run_open_orders.pop(oid, None)
        if filled_ids and hasattr(self, "execution_scheduled"):
            self.execution_scheduled = True

    # ------------------------------------------------------------------ #
    #  Overridden private endpoints                                        #
    # ------------------------------------------------------------------ #

    async def fetch_positions(self):
        """Return simulated positions and balance instead of querying exchange."""
        self._ensure_dry_run_state()
        positions = []
        now = utc_ms()
        for (symbol, pside), pos in self._dry_run_positions.items():
            if pos["size"] != 0.0:
                positions.append(
                    {
                        "symbol": symbol,
                        "position_side": pside,
                        "size": abs(pos["size"]),
                        "price": pos["price"],
                        "timestamp": now,
                    }
                )
        mark_prices = await self._get_latest_dry_run_marks()
        return positions, self._get_dry_run_equity(mark_prices=mark_prices)

    async def fetch_open_orders(self, symbol=None):
        """Run the matching engine then return remaining pending orders."""
        self._ensure_dry_run_state()
        await self._dry_run_match_orders()
        orders = list(self._dry_run_open_orders.values())
        if symbol is not None:
            orders = [o for o in orders if o["symbol"] == symbol]
        return orders

    async def execute_order(self, order: dict) -> dict:
        """Place an order; market orders are filled immediately, limit orders are queued."""
        self._ensure_dry_run_state()
        order_id = f"dry_run_{next(_dry_run_id_counter)}"
        qty = abs(order.get("qty", order.get("amount", 0.0)))
        if not order.get("position_side"):
            logging.warning(f"[DRY RUN] execute_order missing position_side, defaulting to 'long': {order}")
        pending = {
            "id": order_id,
            "symbol": order["symbol"],
            "side": order.get("side", ""),
            "position_side": order.get("position_side") or "long",
            "qty": qty,
            "price": float(order["price"]),
            "amount": qty,
            "reduce_only": order.get("reduce_only", False),
            "timestamp": utc_ms(),
            "status": "open",
            "type": order.get("type", "limit"),
        }

        # Immediate fill for market orders
        if order.get("type") == "market":
            if not hasattr(self, "cm"):
                reason = "market order rejected: no price source available"
                logging.warning(f"[DRY RUN] {reason}")
                return self._make_rejected_order_result(pending, reason, order_id=None)
            try:
                prices = await self.cm.get_last_prices(
                    {order["symbol"]}, max_age_ms=10_000
                )
            except Exception as e:
                reason = f"market order rejected: price lookup failed ({e})"
                logging.warning(f"[DRY RUN] {reason}")
                return self._make_rejected_order_result(pending, reason, order_id=None)
            fill_price = prices.get(order["symbol"])
            if not fill_price:
                reason = "market order rejected: no fresh price available"
                logging.warning(f"[DRY RUN] {reason}")
                return self._make_rejected_order_result(pending, reason, order_id=None)
            fill_result = await self._dry_run_fill_order(
                pending, fill_price, mark_prices=prices
            )
            if fill_result and not fill_result.get("rejected", False):
                logging.info(
                    f"[DRY RUN] market fill {pending['side']} {pending['position_side']}"
                    f" {pending['symbol']} qty={qty} @ {fill_price}"
                )
                return {**pending, "filled": qty, "remaining": 0.0, "status": "closed"}
            return fill_result

        can_place, reason, _, _ = await self._check_margin_available(
            pending, pending["price"]
        )
        if not can_place:
            logging.warning(f"[DRY RUN] rejecting order placement: {reason} {pending}")
            return self._make_rejected_order_result(
                pending, reason, order_id=None, remaining=qty
            )

        self._dry_run_open_orders[order_id] = pending
        logging.info(
            f"[DRY RUN] placed {pending['side']} {pending['position_side']}"
            f" {pending['symbol']} qty={qty} @ {pending['price']}"
        )
        return {**pending, "filled": 0.0, "remaining": qty}

    async def execute_cancellation(self, order: dict) -> dict:
        """Remove a pending order from the in-memory order book."""
        self._ensure_dry_run_state()
        self._dry_run_open_orders.pop(order.get("id", ""), None)
        return {"id": order.get("id", ""), "symbol": order.get("symbol", ""), "status": "canceled"}

    async def execute_orders(self, orders):
        """Override batch creates — route each through dry-run execute_order.

        Without this, exchange adapters with batch implementations (e.g. Lighter's
        _sign_and_send_batch) would bypass dry-run and send real orders.
        """
        return [await self.execute_order(o) for o in orders]

    async def execute_cancellations(self, orders):
        """Override batch cancels — route each through dry-run execute_cancellation.

        Prevents any future exchange batch-cancel implementation from bypassing dry-run.
        """
        return [await self.execute_cancellation(o) for o in orders]

    async def start_data_maintainers(self):
        """Start only base maintainers, skipping exchange-specific WS (e.g. TxWebSocket)."""
        if hasattr(self, "maintainers"):
            self.stop_data_maintainers()
        maintainer_names = ["maintain_hourly_cycle"]
        if self.ws_enabled:
            maintainer_names.append("watch_orders")
        else:
            logging.info("Websocket maintainers skipped (ws disabled via custom endpoints).")
        self.maintainers = {
            name: asyncio.create_task(getattr(self, name)()) for name in maintainer_names
        }

    async def _start_ws_early(self):
        """No-op in dry-run: avoids waiting for WS caches that never populate."""
        pass

    async def fetch_pnls(self, start_time=None, end_time=None, limit=None):
        """No historical fill records in dry-run mode."""
        return []

    async def init_pnls(self):
        """Skip fetching PnL history; start with an empty list."""
        if not hasattr(self, "pnls"):
            self.pnls = []
        # already initialized — preserve accumulated dry-run fills

    # ------------------------------------------------------------------ #
    #  Exchange config calls that would touch private endpoints            #
    # ------------------------------------------------------------------ #

    async def update_exchange_config(self):
        """Skip setting hedge mode / account type on the exchange."""
        pass

    async def update_exchange_config_by_symbols(self, symbols):
        """Skip setting per-symbol leverage / margin mode on the exchange."""
        pass

    async def determine_utc_offset(self, verbose=True):
        """Skip private fetch_balance() call; assume exchange is UTC."""
        self.utc_offset = 0
        if verbose:
            logging.info("[DRY RUN] assuming UTC offset = 0ms")

    async def watch_orders(self):
        """Idle loop replacing the private authenticated WS order stream."""
        while not self.stop_websocket:
            await asyncio.sleep(1.0)

    def did_create_order(self, executed) -> bool:
        """Accept any response that carries a non-None id (shadows exchange overrides)."""
        try:
            return (
                "id" in executed
                and executed["id"] is not None
                and executed.get("status") != "rejected"
            )
        except Exception:
            return False

    def did_cancel_order(self, executed, order=None) -> bool:
        """Accept any response that carries a non-None id (shadows exchange overrides)."""
        try:
            return "id" in executed and executed["id"] is not None
        except Exception:
            return False
