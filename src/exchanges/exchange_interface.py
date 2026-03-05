from abc import ABC, abstractmethod


class ExchangeInterface(ABC):
    """Contract that every exchange adapter must fulfill.

    Extracted from the Passivbot base class to formalize the methods each
    exchange subclass is expected to implement.
    """

    # --- Session lifecycle ---

    @abstractmethod
    def create_ccxt_sessions(self):
        """Initialize exchange client sessions (REST + optional WebSocket)."""

    @abstractmethod
    async def close(self):
        """Close all exchange client sessions."""

    # --- Market metadata ---

    @abstractmethod
    def set_market_specific_settings(self):
        """Populate per-symbol: symbol_ids, min_costs, min_qtys, qty_steps,
        price_steps, c_mults, max_leverage."""

    @abstractmethod
    def symbol_is_eligible(self, symbol: str) -> bool:
        """Return True if symbol can be traded."""

    # --- Data fetching ---

    @abstractmethod
    async def fetch_positions(self):
        """Return (positions_list, balance).
        Position: {symbol, position_side, size, price}."""

    @abstractmethod
    async def fetch_open_orders(self, symbol=None):
        """Return list of open orders.
        Order: {id, symbol, side, qty, price, position_side, ...}."""

    @abstractmethod
    async def fetch_tickers(self):
        """Return {symbol: {bid, ask, last}}."""

    @abstractmethod
    async def fetch_ohlcv(self, symbol, timeframe="1m"):
        """Return OHLCV candles as [[timestamp, O, H, L, C, V], ...]."""

    @abstractmethod
    async def fetch_ohlcvs_1m(self, symbol, since=None, limit=None):
        """Return 1m candles for EMA calculation."""

    @abstractmethod
    async def fetch_pnls(self, start_time=None, end_time=None, limit=None):
        """Return PnL records:
        [{id, symbol, timestamp, pnl, position_side, qty, price}]."""

    # --- Order execution ---

    @abstractmethod
    async def execute_order(self, order: dict) -> dict:
        """Place order {symbol, side, qty, price, type, position_side,
        reduce_only, custom_id}."""

    @abstractmethod
    async def execute_cancellation(self, order: dict) -> dict:
        """Cancel order by {id, symbol}."""

    @abstractmethod
    def did_create_order(self, executed) -> bool:
        """Return True if execute_order response indicates success."""

    @abstractmethod
    def did_cancel_order(self, executed, order=None) -> bool:
        """Return True if execute_cancellation response indicates success."""

    @abstractmethod
    def get_order_execution_params(self, order: dict) -> dict:
        """Return exchange-specific params to pass with order creation."""

    # --- Exchange config ---

    @abstractmethod
    async def update_exchange_config(self):
        """One-time exchange-level setup (hedge mode, account type, etc.)."""

    @abstractmethod
    async def update_exchange_config_by_symbols(self, symbols: list):
        """Per-symbol setup (leverage, margin mode)."""

    # --- WebSocket ---

    @abstractmethod
    async def watch_orders(self):
        """Background loop: listen for order fill/cancel events via WebSocket."""
