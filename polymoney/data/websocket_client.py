"""
WebSocket client for Polymarket real-time data.

Provides connection management with auto-reconnect for market data streams.
"""

import asyncio
import json
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

import websockets
from websockets.client import WebSocketClientProtocol

from polymoney.core.logging import get_logger
from polymoney.core.models import PriceData

logger = get_logger("data.websocket")


class ConnectionState(str, Enum):
    """WebSocket connection state."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class MarketSubscription:
    """Subscription state for a single market."""

    def __init__(self, market_id: str, token_ids: List[str]):
        self.market_id = market_id
        self.token_ids = token_ids
        self.orderbook: Dict[str, Any] = {"bids": [], "asks": []}
        self.last_update: Optional[datetime] = None
        self.best_bid: float = 0.0
        self.best_ask: float = 1.0


# Type alias for event callbacks
EventCallback = Callable[[str, str, Any], None]  # (event_type, market_id, data)


class WebSocketManager:
    """
    Manages WebSocket connections to Polymarket CLOB API.

    Features:
    - Auto-reconnect with exponential backoff
    - Multi-market subscription
    - Orderbook state management
    - Event callback system
    """

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        reconnect_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ):
        """
        Initialize WebSocket manager.

        Args:
            reconnect_delay: Initial reconnect delay in seconds
            reconnect_max_delay: Maximum reconnect delay in seconds
        """
        self.reconnect_delay = reconnect_delay
        self.reconnect_max_delay = reconnect_max_delay

        self._running = False
        self._state = ConnectionState.DISCONNECTED
        self._ws: Optional[WebSocketClientProtocol] = None
        self._subscriptions: Dict[str, MarketSubscription] = {}
        self._callbacks: List[EventCallback] = []
        self._tasks: Set[asyncio.Task] = set()

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if connected."""
        return self._state == ConnectionState.CONNECTED

    @property
    def subscribed_markets(self) -> List[str]:
        """Get list of subscribed market IDs."""
        return list(self._subscriptions.keys())

    def add_callback(self, callback: EventCallback) -> None:
        """Add an event callback."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: EventCallback) -> None:
        """Remove an event callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _emit_event(self, event_type: str, market_id: str, data: Any) -> None:
        """Emit an event to all callbacks."""
        for callback in self._callbacks:
            try:
                callback(event_type, market_id, data)
            except Exception as e:
                logger.error(f"Error in event callback: {e}")

    async def start(self) -> None:
        """Start the WebSocket manager."""
        if self._running:
            return

        self._running = True
        logger.info("WebSocket manager started")

    async def stop(self) -> None:
        """Stop the WebSocket manager and close all connections."""
        self._running = False

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()

        # Close WebSocket
        if self._ws:
            await self._ws.close()
            self._ws = None

        self._state = ConnectionState.DISCONNECTED
        logger.info("WebSocket manager stopped")

    async def subscribe(self, market_id: str, token_ids: List[str]) -> None:
        """
        Subscribe to a market's data feed.

        Args:
            market_id: Market condition ID
            token_ids: List of token IDs (YES/NO)
        """
        if market_id in self._subscriptions:
            logger.warning(f"Already subscribed to market {market_id}")
            return

        self._subscriptions[market_id] = MarketSubscription(market_id, token_ids)

        # Start connection task if not already running
        task = asyncio.create_task(self._run_subscription(market_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        logger.info(f"Subscribed to market {market_id}")

    async def unsubscribe(self, market_id: str) -> None:
        """
        Unsubscribe from a market.

        Args:
            market_id: Market condition ID
        """
        if market_id in self._subscriptions:
            del self._subscriptions[market_id]
            logger.info(f"Unsubscribed from market {market_id}")

    async def _run_subscription(self, market_id: str) -> None:
        """Run the subscription loop for a market with auto-reconnect."""
        delay = self.reconnect_delay

        while self._running and market_id in self._subscriptions:
            try:
                self._state = ConnectionState.CONNECTING
                await self._connect_and_subscribe(market_id)
            except websockets.ConnectionClosed as e:
                logger.warning(f"Connection closed for {market_id}: {e}")
                self._state = ConnectionState.RECONNECTING
            except Exception as e:
                logger.error(f"WebSocket error for {market_id}: {e}")
                self._state = ConnectionState.RECONNECTING

            if self._running and market_id in self._subscriptions:
                logger.info(f"Reconnecting to {market_id} in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_delay)

    async def _connect_and_subscribe(self, market_id: str) -> None:
        """Connect to WebSocket and subscribe to market."""
        subscription = self._subscriptions.get(market_id)
        if not subscription:
            return

        async with websockets.connect(self.WS_URL) as ws:
            self._ws = ws
            self._state = ConnectionState.CONNECTED
            logger.info(f"Connected to WebSocket for {market_id}")

            # Send subscription message
            sub_message = {
                "type": "subscribe",
                "market": market_id,
                "assets_ids": subscription.token_ids,
            }
            await ws.send(json.dumps(sub_message))

            # Process messages
            async for message in ws:
                if not self._running or market_id not in self._subscriptions:
                    break
                await self._process_message(market_id, message)

    async def _process_message(self, market_id: str, raw_message: str) -> None:
        """Process an incoming WebSocket message."""
        try:
            message = json.loads(raw_message)
            msg_type = message.get("type", message.get("event_type", ""))

            subscription = self._subscriptions.get(market_id)
            if not subscription:
                return

            if msg_type == "book":
                await self._handle_orderbook(market_id, subscription, message)
            elif msg_type == "trade":
                await self._handle_trade(market_id, message)
            elif msg_type == "price_change":
                await self._handle_price_change(market_id, subscription, message)
            else:
                logger.debug(f"Unknown message type: {msg_type}")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _handle_orderbook(
        self, market_id: str, subscription: MarketSubscription, message: Dict
    ) -> None:
        """Handle orderbook snapshot/update."""
        subscription.orderbook = message.get("data", {})
        subscription.last_update = datetime.now()

        # Extract best bid/ask
        bids = subscription.orderbook.get("bids", [])
        asks = subscription.orderbook.get("asks", [])

        if bids:
            subscription.best_bid = float(bids[0].get("price", 0))
        if asks:
            subscription.best_ask = float(asks[0].get("price", 1))

        # Emit orderbook event
        self._emit_event("orderbook", market_id, subscription.orderbook)

        # Emit price update
        price_data = PriceData(
            market_id=market_id,
            up_price=subscription.best_ask,
            down_price=1.0 - subscription.best_bid,
            spread=subscription.best_ask - subscription.best_bid,
        )
        self._emit_event("price_update", market_id, price_data)

    async def _handle_trade(self, market_id: str, message: Dict) -> None:
        """Handle trade message."""
        trade_data = message.get("data", message)
        self._emit_event("trade", market_id, trade_data)

    async def _handle_price_change(
        self, market_id: str, subscription: MarketSubscription, message: Dict
    ) -> None:
        """Handle price change message."""
        data = message.get("data", message)

        # Update prices
        if "price" in data:
            # Single price update
            subscription.best_ask = float(data.get("price", subscription.best_ask))

        subscription.last_update = datetime.now()

        # Emit price update
        price_data = PriceData(
            market_id=market_id,
            up_price=subscription.best_ask,
            down_price=1.0 - subscription.best_bid,
            spread=subscription.best_ask - subscription.best_bid,
        )
        self._emit_event("price_update", market_id, price_data)

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of WebSocket connections."""
        return {
            "state": self._state.value,
            "is_connected": self.is_connected,
            "subscribed_markets": len(self._subscriptions),
            "markets": {
                market_id: {
                    "last_update": sub.last_update.isoformat() if sub.last_update else None,
                    "best_bid": sub.best_bid,
                    "best_ask": sub.best_ask,
                }
                for market_id, sub in self._subscriptions.items()
            },
        }
