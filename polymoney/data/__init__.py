"""
Data module - Market data service, WebSocket client, candlestick aggregation, storage.
"""

from .candlestick_aggregator import CandlestickAggregator
from .market_data_service import (
    EVENT_CANDLESTICK_COMPLETE,
    EVENT_CONNECTION_LOST,
    EVENT_CONNECTION_RESTORED,
    EVENT_MARKET_END,
    EVENT_MARKET_SETTLED,
    EVENT_MARKET_START,
    EVENT_ORDERBOOK,
    EVENT_PRICE_UPDATE,
    EVENT_TRADE,
    MarketDataService,
)
from .storage import DataStorage
from .websocket_client import ConnectionState, WebSocketManager

__all__ = [
    "MarketDataService",
    "WebSocketManager",
    "CandlestickAggregator",
    "ConnectionState",
    "DataStorage",
    # Event types
    "EVENT_PRICE_UPDATE",
    "EVENT_TRADE",
    "EVENT_ORDERBOOK",
    "EVENT_CANDLESTICK_COMPLETE",
    "EVENT_MARKET_START",
    "EVENT_MARKET_END",
    "EVENT_MARKET_SETTLED",
    "EVENT_CONNECTION_LOST",
    "EVENT_CONNECTION_RESTORED",
]
