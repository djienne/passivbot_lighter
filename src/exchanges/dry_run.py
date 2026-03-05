"""
DryRunMixin: paper trading simulation overlay.

Mix this in before any real exchange bot class to intercept all private
API calls.  Public endpoints (OHLCV, tickers, market metadata) continue
to use the real exchange so the bot's signal logic is exercised against
live data.

Usage (handled automatically by setup_bot when live.dry_run is true):
    DryRunBybitBot = type("DryRunBybitBot", (DryRunMixin, BybitBot), {})
    bot = DryRunBybitBot(config)
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
        self._dry_run_initialized = True
        logging.info(
            f"[DRY RUN] paper wallet initialised at {self._dry_run_balance} USDT"
        )

    def _get_fee_rate(self, symbol: str) -> float:
        try:
            return self.markets_dict[symbol].get("maker", 0.0002) or 0.0002
        except Exception:
            return 0.0002

    def _dry_run_fill_order(self, order: dict, fill_price: float):
        """Apply a filled limit order to paper state (synchronous)."""
        symbol = order["symbol"]
        pside = order.get("position_side", "long")
        qty = abs(order.get("qty", order.get("amount", 0.0)))
        reduce_only = order.get("reduce_only", False)
        c_mult = self.c_mults.get(symbol, 1.0) if hasattr(self, "c_mults") else 1.0
        fee_rate = self._get_fee_rate(symbol)

        key = (symbol, pside)

        if reduce_only:
            # Closing an existing position — clamp qty, realise PnL
            pos = self._dry_run_positions.get(key, {"size": 0.0, "price": 0.0})
            old_size = pos["size"]
            effective_qty = min(qty, old_size)
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
        else:
            # Opening / adding to a position — update weighted average entry
            fee = fill_price * qty * c_mult * fee_rate
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
                self._dry_run_fill_order(order, fill_price=limit_price)
                filled_ids.append(order_id)
        for oid in filled_ids:
            self._dry_run_open_orders.pop(oid, None)

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
        return positions, self._dry_run_balance

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
        pending = {
            "id": order_id,
            "symbol": order["symbol"],
            "side": order.get("side", ""),
            "position_side": order.get("position_side", "long"),
            "qty": qty,
            "price": float(order["price"]),
            "amount": qty,
            "reduce_only": order.get("reduce_only", False),
            "timestamp": utc_ms(),
            "status": "open",
        }

        # Immediate fill for market orders
        if order.get("type") == "market" and hasattr(self, "cm"):
            try:
                prices = await self.cm.get_last_prices(
                    {order["symbol"]}, max_age_ms=10_000
                )
                fill_price = prices.get(order["symbol"])
                if fill_price:
                    self._dry_run_fill_order(pending, fill_price)
                    logging.info(
                        f"[DRY RUN] market fill {pending['side']} {pending['position_side']}"
                        f" {pending['symbol']} qty={qty} @ {fill_price}"
                    )
                    return {**pending, "filled": qty, "remaining": 0.0, "status": "closed"}
            except Exception:
                pass  # fall through to limit queue

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
            return "id" in executed and executed["id"] is not None
        except Exception:
            return False

    def did_cancel_order(self, executed, order=None) -> bool:
        """Accept any response that carries a non-None id (shadows exchange overrides)."""
        try:
            return "id" in executed and executed["id"] is not None
        except Exception:
            return False
