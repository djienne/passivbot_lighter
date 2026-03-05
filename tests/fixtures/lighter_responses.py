"""Mock API response data for Lighter exchange tests."""

# Simulates order_books() response from Lighter OrderApi
# Focused on HYPE market
MOCK_ORDER_BOOKS_RESPONSE = {
    "order_books": [
        {
            "market_id": 5,
            "symbol": "HYPE",
            "supported_price_decimals": 4,
            "supported_size_decimals": 2,
            "min_base_amount": 0.01,
            "min_quote_amount": 1.0,
            "best_bid": 15.1234,
            "best_ask": 15.1345,
            "max_leverage": 20,
        },
        {
            "market_id": 0,
            "symbol": "BTC",
            "supported_price_decimals": 1,
            "supported_size_decimals": 5,
            "min_base_amount": 0.00001,
            "min_quote_amount": 1.0,
            "best_bid": 95000.1,
            "best_ask": 95001.2,
            "max_leverage": 100,
        },
        {
            "market_id": 1,
            "symbol": "ETH",
            "supported_price_decimals": 2,
            "supported_size_decimals": 4,
            "min_base_amount": 0.001,
            "min_quote_amount": 1.0,
            "best_bid": 3200.45,
            "best_ask": 3200.67,
            "max_leverage": 50,
        },
    ]
}

# Simulates AccountApi.account() response
MOCK_ACCOUNT_RESPONSE = {
    "accounts": [
        {
            "available_balance": 5000.0,
            "collateral": 5500.0,
            "total_asset_value": 5500.0,
            "positions": {
                "5": {
                    "position": 10.0,
                    "sign": 1,
                    "entry_price": 14.50,
                },
            },
        }
    ]
}

# No position variant
MOCK_ACCOUNT_RESPONSE_EMPTY = {
    "accounts": [
        {
            "available_balance": 10000.0,
            "collateral": 10000.0,
            "total_asset_value": 10000.0,
            "positions": {},
        }
    ]
}

# Simulates accountActiveOrders REST response
MOCK_ACTIVE_ORDERS = {
    "orders": [
        {
            "client_order_index": 123456789,
            "order_index": 987654,
            "market_id": 5,
            "is_ask": False,
            "status": "open",
            "price": 14.80,
            "remaining_base_amount": 5.0,
            "initial_base_amount": 5.0,
            "timestamp": 1709500000000,
        },
        {
            "client_order_index": 123456790,
            "order_index": 987655,
            "market_id": 5,
            "is_ask": True,
            "status": "open",
            "price": 15.50,
            "remaining_base_amount": 5.0,
            "initial_base_amount": 5.0,
            "timestamp": 1709500001000,
        },
    ]
}

# Simulates successful order creation
MOCK_ORDER_CREATION = {
    "tx": "0xabc123",
    "tx_hash": "0xdef456",
    "err": None,
}

# Simulates candles response
MOCK_CANDLES = {
    "candlesticks": [
        {
            "timestamp": 1709500000000,
            "open": 15.00,
            "high": 15.20,
            "low": 14.90,
            "close": 15.10,
            "volume": 1000.0,
        },
        {
            "timestamp": 1709500060000,
            "open": 15.10,
            "high": 15.25,
            "low": 15.05,
            "close": 15.15,
            "volume": 800.0,
        },
        {
            "timestamp": 1709500120000,
            "open": 15.15,
            "high": 15.30,
            "low": 15.10,
            "close": 15.20,
            "volume": 1200.0,
        },
    ]
}

# Simulates inactive orders (for PnL)
MOCK_INACTIVE_ORDERS = {
    "orders": [
        {
            "order_index": 100001,
            "client_order_index": 200001,
            "market_id": 5,
            "is_ask": True,
            "status": "filled",
            "price": 15.50,
            "initial_base_amount": 2.0,
            "remaining_base_amount": 0,
            "timestamp": 1709400000000,
            "realized_pnl": 1.50,
        },
        {
            "order_index": 100002,
            "client_order_index": 200002,
            "market_id": 5,
            "is_ask": False,
            "status": "filled",
            "price": 14.80,
            "initial_base_amount": 3.0,
            "remaining_base_amount": 0,
            "timestamp": 1709400100000,
            "realized_pnl": -0.30,
        },
    ]
}

# Simulates cancel order response
MOCK_CANCEL_RESPONSE = {
    "code": 0,
    "message": "OK",
}

# Simulates failed cancel
MOCK_CANCEL_RESPONSE_FAIL = {
    "code": 1,
    "message": "Order not found",
}
