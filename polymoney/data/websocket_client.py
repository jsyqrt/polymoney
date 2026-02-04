"""
WebSocket client for Polymarket real-time data.

Provides connection management with auto-reconnect for market data streams.
Supports per-token price tracking for dual-token markets (UP/DOWN).
"""

import asyncio
import json
from dataclasses import dataclass
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


@dataclass
class TokenPrice:
    """Price data for a single token."""
    token_id: str
    best_bid: Optional[float] = None  # None = no data yet
    best_ask: Optional[float] = None  # None = no data yet
    mid_price: Optional[float] = None  # None = no data yet
    last_update: Optional[datetime] = None
    
    @property
    def is_valid(self) -> bool:
        """Check if we have valid price data."""
        return self.mid_price is not None and self.best_bid is not None and self.best_ask is not None
    
    def update(self, bid: Optional[float] = None, ask: Optional[float] = None) -> bool:
        """
        Update prices and recalculate mid.
        
        Returns True if prices were successfully updated.
        """
        if bid is not None:
            self.best_bid = bid
        if ask is not None:
            self.best_ask = ask
        
        # Only calculate mid_price if we have both bid and ask
        if self.best_bid is not None and self.best_ask is not None:
            self.mid_price = (self.best_bid + self.best_ask) / 2
            self.last_update = datetime.now()
            return True
        return False


class MarketSubscription:
    """Subscription state for a single market with per-token tracking."""

    def __init__(self, market_id: str, token_ids: List[str]):
        self.market_id = market_id
        self.token_ids = token_ids
        # Track prices per token
        self.token_prices: Dict[str, TokenPrice] = {
            tid: TokenPrice(token_id=tid) for tid in token_ids
        }
        self.last_update: Optional[datetime] = None
        # Legacy fields for backward compatibility
        self.orderbook: Dict[str, Any] = {"bids": [], "asks": []}
        self.best_bid: float = 0.0
        self.best_ask: float = 1.0


# Type alias for event callbacks
# (event_type, identifier, data) - identifier can be market_id or token_id
EventCallback = Callable[[str, str, Any], None]


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
        self._original_exception_handler: Optional[Any] = None

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

    def _handle_loop_exception(self, loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
        """
        Custom asyncio exception handler to suppress known websockets library bugs.
        
        Known suppressed errors:
        1. recv_messages AttributeError: connection_lost() on partially initialized connection
        2. status_code AttributeError: eof_received() on HTTPProxyConnection with no response
        3. EOFError "stream ended": cascading from the proxy connection EOF
        
        These are all harmless since we handle reconnection ourselves.
        """
        exception = context.get("exception")
        if isinstance(exception, AttributeError):
            err_str = str(exception)
            if "recv_messages" in err_str or "status_code" in err_str:
                logger.debug(f"Suppressed known websockets error: {err_str}")
                return
        
        if isinstance(exception, EOFError) and "stream ended" in str(exception):
            logger.debug("Suppressed known websockets EOF error during connection reset")
            return

        # Delegate to original handler or default
        if self._original_exception_handler:
            self._original_exception_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    async def start(self) -> None:
        """Start the WebSocket manager."""
        if self._running:
            return

        self._running = True

        # Install custom exception handler to suppress known websockets bug
        loop = asyncio.get_event_loop()
        self._original_exception_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._handle_loop_exception)

        logger.info("WebSocket manager started")

    async def stop(self) -> None:
        """Stop the WebSocket manager and close all connections."""
        self._running = False

        # Cancel all tasks (copy set to avoid modification during iteration)
        tasks = list(self._tasks)
        for task in tasks:
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

        # Restore original exception handler
        try:
            loop = asyncio.get_event_loop()
            if self._original_exception_handler:
                loop.set_exception_handler(self._original_exception_handler)
            else:
                loop.set_exception_handler(None)
            self._original_exception_handler = None
        except RuntimeError:
            pass  # Event loop already closed

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
            except (EOFError, OSError) as e:
                # Network-level errors (proxy EOF, connection reset, etc.)
                logger.warning(f"Network error for {market_id}: {e}")
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

            # Send subscription message (official format from Polymarket docs)
            # https://docs.polymarket.com/quickstart/websocket/WSS-Quickstart
            sub_message = {
                "assets_ids": subscription.token_ids,
                "type": "market",  # Channel type, not action
            }
            await ws.send(json.dumps(sub_message))
            logger.debug(f"Sent subscription for {len(subscription.token_ids)} tokens")

            # Start ping task to keep connection alive (required every 10s)
            ping_task = asyncio.create_task(self._ping_loop(ws))

            try:
                # Process messages
                async for message in ws:
                    if not self._running or market_id not in self._subscriptions:
                        break
                    await self._process_message(market_id, message)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

    async def _ping_loop(self, ws: WebSocketClientProtocol) -> None:
        """Send PING every 10 seconds to keep connection alive."""
        try:
            while True:
                await ws.send("PING")
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Ping loop ended: {e}")

    async def _process_message(self, market_id: str, raw_message: str) -> None:
        """Process an incoming WebSocket message."""
        try:
            # Handle PONG response (not JSON)
            if raw_message == "PONG":
                return
            
            message = json.loads(raw_message)
            
            # Handle case where message is a list (initial orderbook snapshot)
            # Format: [{"asset_id": "...", "bids": [...], "asks": [...], ...}, ...]
            if isinstance(message, list):
                if len(message) == 0:
                    logger.debug(f"Received empty array for {market_id}")
                    return
                    
                # Process each orderbook in the array
                for item in message:
                    if isinstance(item, dict):
                        # Check if this is an orderbook snapshot (has bids/asks)
                        if "bids" in item or "asks" in item:
                            await self._handle_orderbook_snapshot(market_id, item)
                        else:
                            await self._process_single_message(market_id, item)
                return
            
            # Single message dict
            if isinstance(message, dict):
                await self._process_single_message(market_id, message)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse message: {e}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _handle_orderbook_snapshot(self, market_id: str, data: Dict) -> None:
        """Handle initial orderbook snapshot from WebSocket array response."""
        subscription = self._subscriptions.get(market_id)
        if not subscription:
            return
        
        subscription.last_update = datetime.now()
        
        # Extract asset_id and process
        asset_id = data.get("asset_id") or data.get("token_id")
        if not asset_id:
            logger.debug(f"Orderbook snapshot without asset_id for {market_id}")
            return
        
        # Ensure this token is tracked
        if asset_id not in subscription.token_prices:
            subscription.token_prices[asset_id] = TokenPrice(token_id=asset_id)
        
        token_price = subscription.token_prices[asset_id]
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        # Extract best bid/ask
        bid = None
        ask = None
        
        if bids:
            first_bid = bids[0]
            if isinstance(first_bid, dict):
                bid = float(first_bid.get("price", 0))
            elif isinstance(first_bid, (list, tuple)) and len(first_bid) > 0:
                bid = float(first_bid[0])
        
        if asks:
            first_ask = asks[0]
            if isinstance(first_ask, dict):
                ask = float(first_ask.get("price", 1))
            elif isinstance(first_ask, (list, tuple)) and len(first_ask) > 0:
                ask = float(first_ask[0])
        
        if bid is not None and ask is not None:
            spread = ask - bid
            logger.info(
                f"[WS] Initial orderbook for {asset_id[:12]}...: "
                f"bid={bid:.4f} ask={ask:.4f} mid={(bid+ask)/2:.4f} spread={spread:.4f}"
            )
            
            # Filter garbage data at source: when WS first connects, it often
            # sends bid=0.01 ask=0.99 (full range = no real liquidity).
            # Don't emit ANY events for this — not even orderbook depth.
            if spread > 0.50:
                logger.info(
                    f"[WS] Filtering garbage initial orderbook for {asset_id[:12]}... "
                    f"(spread={spread:.2f} > 0.50, no real data)"
                )
                return
            
            token_price.update(bid=bid, ask=ask)
            
            # Emit price event (only for valid data)
            self._emit_event("token_price", asset_id, {
                "token_id": asset_id,
                "mid_price": token_price.mid_price,
                "best_bid": token_price.best_bid,
                "best_ask": token_price.best_ask,
            })
            
            # Emit full orderbook depth for fill simulation
            self._emit_event("token_orderbook", asset_id, {
                "token_id": asset_id,
                "bids": bids,
                "asks": asks,
            })
    
    async def _process_single_message(self, market_id: str, message: Dict) -> None:
        """Process a single WebSocket message dict."""
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
        elif msg_type == "last_trade_price":
            await self._handle_last_trade_price(market_id, subscription, message)
        else:
            logger.debug(f"Unknown message type: {msg_type}")

    async def _handle_orderbook(
        self, market_id: str, subscription: MarketSubscription, message: Dict
    ) -> None:
        """Handle orderbook snapshot/update."""
        data = message.get("data", message)
        subscription.last_update = datetime.now()
        
        # Handle case where data is a list of orderbooks (one per token)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._process_single_orderbook(market_id, subscription, item)
            return
        
        # Single orderbook dict
        await self._process_single_orderbook(market_id, subscription, data)
    
    async def _process_single_orderbook(
        self, market_id: str, subscription: MarketSubscription, data: Dict
    ) -> None:
        """Process a single orderbook update."""
        if not isinstance(data, dict):
            return
        
        # Check if this is a per-token orderbook
        asset_id = data.get("asset_id") or data.get("token_id")
        
        if asset_id and asset_id in subscription.token_prices:
            # Per-token orderbook update
            token_price = subscription.token_prices[asset_id]
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            # Handle bids/asks that might be lists of dicts or lists of lists
            bid = token_price.best_bid
            ask = token_price.best_ask
            
            if bids:
                first_bid = bids[0]
                if isinstance(first_bid, dict):
                    bid = float(first_bid.get("price", 0))
                elif isinstance(first_bid, (list, tuple)) and len(first_bid) > 0:
                    bid = float(first_bid[0])
            
            if asks:
                first_ask = asks[0]
                if isinstance(first_ask, dict):
                    ask = float(first_ask.get("price", 1))
                elif isinstance(first_ask, (list, tuple)) and len(first_ask) > 0:
                    ask = float(first_ask[0])
            
            # Filter garbage orderbook data before emitting
            spread = (ask - bid) if (bid is not None and ask is not None) else 0.0
            if spread > 0.50:
                logger.debug(
                    f"[WS] Filtering garbage book update for {asset_id[:12]}... "
                    f"(spread={spread:.2f})"
                )
                return
            
            if token_price.update(bid=bid, ask=ask):
                # Only emit event if we have valid prices
                self._emit_event("token_price", asset_id, {
                    "token_id": asset_id,
                    "mid_price": token_price.mid_price,
                    "best_bid": token_price.best_bid,
                    "best_ask": token_price.best_ask,
                })
                
                # Emit full orderbook depth for fill simulation
                self._emit_event("token_orderbook", asset_id, {
                    "token_id": asset_id,
                    "bids": bids,
                    "asks": asks,
                })
        else:
            # Legacy: combined orderbook (no asset_id)
            subscription.orderbook = data
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if bids:
                first_bid = bids[0]
                if isinstance(first_bid, dict):
                    subscription.best_bid = float(first_bid.get("price", 0))
                elif isinstance(first_bid, (list, tuple)) and len(first_bid) > 0:
                    subscription.best_bid = float(first_bid[0])
            
            if asks:
                first_ask = asks[0]
                if isinstance(first_ask, dict):
                    subscription.best_ask = float(first_ask.get("price", 1))
                elif isinstance(first_ask, (list, tuple)) and len(first_ask) > 0:
                    subscription.best_ask = float(first_ask[0])
            
            # Emit legacy market-level event
            self._emit_event("orderbook", market_id, subscription.orderbook)
            
            price_data = PriceData(
                market_id=market_id,
                up_price=subscription.best_ask,
                down_price=1.0 - subscription.best_bid,
                spread=subscription.best_ask - subscription.best_bid,
            )
            self._emit_event("price_update", market_id, price_data)

    async def _handle_trade(self, market_id: str, message: Dict) -> None:
        """Handle trade message."""
        data = message.get("data", message)
        
        # Extract token_id from trade if available
        asset_id = data.get("asset_id") or data.get("token_id")
        price = data.get("price")
        
        if asset_id and price:
            # Emit token-specific trade event
            self._emit_event("token_trade", asset_id, {
                "token_id": asset_id,
                "price": float(price),
                "size": data.get("size", 0),
                "side": data.get("side", ""),
            })
        
        # Also emit market-level trade event
        self._emit_event("trade", market_id, data)

    async def _handle_last_trade_price(
        self, market_id: str, subscription: MarketSubscription, message: Dict
    ) -> None:
        """Handle last_trade_price message (real-time trade notification).
        
        Format:
        {
            "market": "0x...",
            "asset_id": "...",
            "price": "0.998",
            "size": "5",
            "side": "BUY",
            "event_type": "last_trade_price"
        }
        """
        asset_id = message.get("asset_id")
        price = message.get("price")
        
        if not asset_id or price is None:
            return
        
        # Emit trade event
        self._emit_event("token_trade", asset_id, {
            "token_id": asset_id,
            "price": float(price),
            "size": float(message.get("size", 0)),
            "side": message.get("side", ""),
        })

    async def _handle_price_change(
        self, market_id: str, subscription: MarketSubscription, message: Dict
    ) -> None:
        """Handle price change message.
        
        New format (from actual WebSocket):
        {
            "market": "0x...",
            "price_changes": [
                {
                    "asset_id": "...",
                    "price": "0.83",
                    "best_bid": "0.997",
                    "best_ask": "0.998"
                },
                ...
            ],
            "event_type": "price_change"
        }
        """
        subscription.last_update = datetime.now()
        
        # Handle new format with price_changes array
        price_changes = message.get("price_changes", [])
        if price_changes:
            for change in price_changes:
                asset_id = change.get("asset_id")
                if not asset_id:
                    continue
                
                # Ensure this token is tracked
                if asset_id not in subscription.token_prices:
                    subscription.token_prices[asset_id] = TokenPrice(token_id=asset_id)
                
                token_price = subscription.token_prices[asset_id]
                
                # Extract best_bid and best_ask from the change
                best_bid = change.get("best_bid")
                best_ask = change.get("best_ask")
                
                if best_bid is not None and best_ask is not None:
                    bid = float(best_bid)
                    ask = float(best_ask)
                    if token_price.update(bid=bid, ask=ask):
                        self._emit_event("token_price", asset_id, {
                            "token_id": asset_id,
                            "mid_price": token_price.mid_price,
                            "best_bid": token_price.best_bid,
                            "best_ask": token_price.best_ask,
                        })
            return
        
        # Legacy format: single asset_id directly in message
        data = message.get("data", message)
        asset_id = data.get("asset_id") or data.get("token_id")
        price = data.get("price")
        
        if asset_id and asset_id in subscription.token_prices and price is not None:
            # Per-token price update
            token_price = subscription.token_prices[asset_id]
            price_val = float(price)
            
            # For price_change, treat the price as both bid and ask (approximate)
            # This updates mid_price and marks as valid
            token_price.mid_price = price_val
            token_price.best_bid = price_val
            token_price.best_ask = price_val
            token_price.last_update = datetime.now()
            
            # Emit token-specific price update with valid data
            self._emit_event("token_price", asset_id, {
                "token_id": asset_id,
                "mid_price": token_price.mid_price,
                "best_bid": token_price.best_bid,
                "best_ask": token_price.best_ask,
            })
        elif price is not None:
            # Legacy: market-level price change
            subscription.best_ask = float(price)
            subscription.best_bid = float(price)  # Approximate
            
            price_data = PriceData(
                market_id=market_id,
                up_price=subscription.best_ask,
                down_price=1.0 - subscription.best_bid,
                spread=0.0,  # No spread info
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
