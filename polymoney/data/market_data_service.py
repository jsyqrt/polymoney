"""
Market Data Service - Central coordinator for real-time market data.

Combines WebSocket connections, candlestick aggregation, and event distribution.
"""

import asyncio
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    Candlestick,
    MarketEnd,
    MarketSettled,
    MarketStart,
    PriceData,
    TokenType,
)

from .candlestick_aggregator import CandlestickAggregator
from .websocket_client import WebSocketManager

logger = get_logger("data.service")


# Event types
EVENT_PRICE_UPDATE = "price_update"
EVENT_TRADE = "trade"
EVENT_ORDERBOOK = "orderbook"
EVENT_CANDLESTICK_COMPLETE = "candlestick_complete"
EVENT_MARKET_START = "market_start"
EVENT_MARKET_END = "market_end"
EVENT_MARKET_SETTLED = "market_settled"
EVENT_CONNECTION_LOST = "connection_lost"
EVENT_CONNECTION_RESTORED = "connection_restored"


# Type alias for event listeners
EventListener = Callable[[str, str, Any], None]  # (event_type, market_id, data)


class MarketDataService:
    """
    Central service for managing market data streams.

    Features:
    - Multi-market WebSocket subscriptions
    - Real-time price updates
    - Candlestick aggregation
    - Event distribution to strategies
    - Connection health monitoring
    """

    def __init__(
        self,
        intervals: Optional[List[str]] = None,
        reconnect_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ):
        """
        Initialize MarketDataService.

        Args:
            intervals: Candlestick intervals to aggregate (default: 1m, 5m, 15m, 1h)
            reconnect_delay: Initial WebSocket reconnect delay
            reconnect_max_delay: Maximum WebSocket reconnect delay
        """
        self.intervals = intervals or ["1m", "5m", "15m", "1h"]

        # Components
        self._ws_manager = WebSocketManager(
            reconnect_delay=reconnect_delay,
            reconnect_max_delay=reconnect_max_delay,
        )
        self._aggregators: Dict[str, CandlestickAggregator] = {}

        # Event listeners
        self._listeners: List[EventListener] = []

        # Market state
        self._active_markets: Set[str] = set()
        self._market_info: Dict[str, Dict[str, Any]] = {}

        # Running state
        self._running = False

        # Setup internal event handling
        self._ws_manager.add_callback(self._on_ws_event)

    @property
    def is_running(self) -> bool:
        """Check if service is running."""
        return self._running

    @property
    def subscribed_markets(self) -> List[str]:
        """Get list of subscribed market IDs."""
        return list(self._active_markets)

    def add_listener(self, listener: EventListener) -> None:
        """Add an event listener."""
        self._listeners.append(listener)

    def remove_listener(self, listener: EventListener) -> None:
        """Remove an event listener."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, event_type: str, market_id: str, data: Any) -> None:
        """Emit an event to all listeners."""
        for listener in self._listeners:
            try:
                listener(event_type, market_id, data)
            except Exception as e:
                logger.error(f"Error in event listener: {e}")

    async def start(self) -> None:
        """Start the market data service."""
        if self._running:
            return

        self._running = True
        await self._ws_manager.start()
        logger.info("Market data service started")

    async def stop(self) -> None:
        """Stop the market data service."""
        if not self._running:
            return

        self._running = False
        await self._ws_manager.stop()
        self._aggregators.clear()
        logger.info("Market data service stopped")

    async def subscribe_market(
        self,
        market_id: str,
        token_ids: List[str],
        title: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Subscribe to a market's real-time data.

        Args:
            market_id: Market condition ID
            token_ids: Token IDs for YES/NO outcomes
            title: Market title/question
            metadata: Additional market metadata
        """
        if market_id in self._active_markets:
            logger.warning(f"Already subscribed to market {market_id}")
            return

        # Create candlestick aggregator for this market
        aggregator = CandlestickAggregator(
            market_id=market_id,
            intervals=self.intervals,
            on_candle_complete=self._on_candle_complete,
        )
        self._aggregators[market_id] = aggregator

        # Store market info
        self._market_info[market_id] = {
            "title": title,
            "token_ids": token_ids,
            "metadata": metadata or {},
            "subscribed_at": datetime.now(),
        }

        # Subscribe via WebSocket
        await self._ws_manager.subscribe(market_id, token_ids)
        self._active_markets.add(market_id)

        # Emit market start event
        event = MarketStart(
            market_id=market_id,
            title=title,
            metadata=metadata or {},
        )
        self._emit(EVENT_MARKET_START, market_id, event)

        logger.info(f"Subscribed to market: {market_id} - {title}")

    async def unsubscribe_market(self, market_id: str) -> None:
        """
        Unsubscribe from a market.

        Args:
            market_id: Market condition ID
        """
        if market_id not in self._active_markets:
            return

        await self._ws_manager.unsubscribe(market_id)
        self._active_markets.discard(market_id)

        if market_id in self._aggregators:
            del self._aggregators[market_id]
        if market_id in self._market_info:
            del self._market_info[market_id]

        logger.info(f"Unsubscribed from market: {market_id}")

    def emit_market_end(
        self,
        market_id: str,
        final_up_price: Optional[float] = None,
        final_down_price: Optional[float] = None,
    ) -> None:
        """
        Emit a market end event.

        Args:
            market_id: Market condition ID
            final_up_price: Final UP/YES price
            final_down_price: Final DOWN/NO price
        """
        event = MarketEnd(
            market_id=market_id,
            final_up_price=final_up_price,
            final_down_price=final_down_price,
        )
        self._emit(EVENT_MARKET_END, market_id, event)

    def emit_market_settled(self, market_id: str, winner: str) -> None:
        """
        Emit a market settlement event.

        Args:
            market_id: Market condition ID
            winner: Winning outcome ('up'/'down' or 'yes'/'no')
        """
        token_type = TokenType(winner.lower())
        event = MarketSettled(
            market_id=market_id,
            winner=token_type,
        )
        self._emit(EVENT_MARKET_SETTLED, market_id, event)

    def get_market_info(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get stored market information."""
        return self._market_info.get(market_id)

    def get_latest_price(self, market_id: str) -> Optional[PriceData]:
        """Get the latest price data for a market."""
        health = self._ws_manager.get_health_status()
        market_health = health.get("markets", {}).get(market_id)

        if not market_health:
            return None

        return PriceData(
            market_id=market_id,
            up_price=market_health.get("best_ask", 0.5),
            down_price=1.0 - market_health.get("best_bid", 0.5),
        )

    def get_health_status(self) -> Dict[str, Any]:
        """Get overall health status."""
        ws_health = self._ws_manager.get_health_status()

        return {
            "is_running": self._running,
            "active_markets": len(self._active_markets),
            "websocket": ws_health,
            "aggregators": len(self._aggregators),
        }

    def _on_ws_event(self, event_type: str, market_id: str, data: Any) -> None:
        """Handle events from WebSocket manager."""
        if event_type == "price_update":
            # Forward price updates
            self._emit(EVENT_PRICE_UPDATE, market_id, data)

        elif event_type == "trade":
            # Forward trades and update aggregator
            self._emit(EVENT_TRADE, market_id, data)

            # Update candlestick aggregator
            aggregator = self._aggregators.get(market_id)
            if aggregator:
                price = float(data.get("price", 0))
                size = float(data.get("size", 0))
                aggregator.on_trade(price, size, TokenType.YES)

        elif event_type == "orderbook":
            # Forward orderbook updates
            self._emit(EVENT_ORDERBOOK, market_id, data)

    def _on_candle_complete(self, candle: Candlestick) -> None:
        """Handle completed candlestick from aggregator."""
        self._emit(EVENT_CANDLESTICK_COMPLETE, candle.market_id, candle)
