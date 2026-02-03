"""
Order Manager - Abstract base and implementations for order execution.

Provides Paper (simulated) and Live (real) order execution modes.
"""

import random
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Dict, List, Optional

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    Order,
    OrderSignal,
    OrderStatus,
    OrderType,
    TradeResult,
    TradeSide,
)

logger = get_logger("strategy.order_manager")


# Type alias for fill callbacks
FillCallback = Callable[[TradeResult], None]


class OrderManager(ABC):
    """
    Abstract base class for order management.

    Provides a unified interface for both paper and live trading.
    """

    def __init__(self):
        self._pending_orders: Dict[str, Order] = {}
        self._fill_callbacks: List[FillCallback] = []

    @property
    def pending_orders(self) -> List[Order]:
        """Get list of pending orders."""
        return list(self._pending_orders.values())

    def add_fill_callback(self, callback: FillCallback) -> None:
        """Add a fill notification callback."""
        self._fill_callbacks.append(callback)

    def remove_fill_callback(self, callback: FillCallback) -> None:
        """Remove a fill callback."""
        if callback in self._fill_callbacks:
            self._fill_callbacks.remove(callback)

    def _notify_fill(self, trade: TradeResult) -> None:
        """Notify all callbacks of a fill."""
        for callback in self._fill_callbacks:
            try:
                callback(trade)
            except Exception as e:
                logger.error(f"Error in fill callback: {e}")

    @abstractmethod
    async def place_order(
        self,
        signal: OrderSignal,
        strategy_id: str,
        market_id: str,
        order_type: OrderType = OrderType.GTC,
    ) -> Order:
        """
        Place an order.

        Args:
            signal: Order signal from strategy
            strategy_id: Strategy identifier
            market_id: Market condition ID
            order_type: Order type (GTC, FOK, etc.)

        Returns:
            Created Order object
        """
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order.

        Args:
            order_id: Order ID to cancel

        Returns:
            True if cancelled successfully
        """
        pass

    @abstractmethod
    async def cancel_all_orders(self, strategy_id: Optional[str] = None) -> int:
        """
        Cancel all pending orders.

        Args:
            strategy_id: Optional filter by strategy

        Returns:
            Number of orders cancelled
        """
        pass

    def get_order(self, order_id: str) -> Optional[Order]:
        """Get an order by ID."""
        return self._pending_orders.get(order_id)

    def get_orders_by_strategy(self, strategy_id: str) -> List[Order]:
        """Get all orders for a strategy."""
        return [o for o in self._pending_orders.values() if o.strategy_id == strategy_id]


class PaperOrderManager(OrderManager):
    """
    Paper (simulated) order manager.

    Simulates order execution for testing strategies without real funds.
    """

    def __init__(
        self,
        slippage_pct: float = 0.001,
        fill_probability: float = 1.0,
        latency_ms: float = 0.0,
    ):
        """
        Initialize paper order manager.

        Args:
            slippage_pct: Simulated slippage as percentage (0.001 = 0.1%)
            fill_probability: Probability of order fill (0-1)
            latency_ms: Simulated latency in milliseconds
        """
        super().__init__()
        self.slippage_pct = slippage_pct
        self.fill_probability = fill_probability
        self.latency_ms = latency_ms

        # Market prices for simulation
        self._market_prices: Dict[str, Dict[str, float]] = {}

    def set_market_price(self, market_id: str, up_price: float, down_price: float) -> None:
        """Set current market prices for simulation."""
        self._market_prices[market_id] = {
            "up": up_price,
            "down": down_price,
        }

    def _get_market_price(self, market_id: str, token_type: str) -> float:
        """Get current market price for a token."""
        prices = self._market_prices.get(market_id, {"up": 0.5, "down": 0.5})
        token = token_type.lower()
        if token in ("yes", "up"):
            return prices.get("up", 0.5)
        return prices.get("down", 0.5)

    def _apply_slippage(self, price: float, side: TradeSide) -> float:
        """Apply slippage to a price."""
        if self.slippage_pct <= 0:
            return price

        slippage = price * self.slippage_pct

        if side == TradeSide.BUY:
            # Buying: price goes up (worse for buyer)
            return min(0.99, price + slippage)
        else:
            # Selling: price goes down (worse for seller)
            return max(0.01, price - slippage)

    async def place_order(
        self,
        signal: OrderSignal,
        strategy_id: str,
        market_id: str,
        order_type: OrderType = OrderType.GTC,
    ) -> Order:
        """Place a simulated order."""
        order_id = f"paper_{uuid.uuid4().hex[:8]}"

        order = Order(
            order_id=order_id,
            strategy_id=strategy_id,
            market_id=market_id,
            side=signal.side,
            token_type=signal.token_type,
            price=signal.target_price,
            size=signal.size,
            order_type=order_type,
        )

        self._pending_orders[order_id] = order
        logger.info(f"Paper order placed: {order_id} {signal.side} {signal.size}@{signal.target_price}")

        # Simulate immediate fill for market orders or matching limit orders
        # Handle both enum and string token_type (due to Pydantic's use_enum_values)
        token_type_str = signal.token_type.value if hasattr(signal.token_type, 'value') else signal.token_type
        market_price = self._get_market_price(market_id, token_type_str)

        should_fill = False
        if order_type == OrderType.FOK:
            # FOK: Fill if price is acceptable
            if signal.side == TradeSide.BUY and market_price <= signal.target_price:
                should_fill = True
            elif signal.side == TradeSide.SELL and market_price >= signal.target_price:
                should_fill = True
        elif order_type == OrderType.GTC:
            # GTC: Fill if limit price is reached
            if signal.side == TradeSide.BUY and market_price <= signal.target_price:
                should_fill = True
            elif signal.side == TradeSide.SELL and market_price >= signal.target_price:
                should_fill = True

        # Apply fill probability
        if should_fill and random.random() <= self.fill_probability:
            await self._simulate_fill(order, market_price)

        return order

    async def _simulate_fill(self, order: Order, market_price: float) -> None:
        """Simulate order fill."""
        fill_price = self._apply_slippage(market_price, order.side)

        # Update order status
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.updated_at = datetime.now()

        # Remove from pending
        if order.order_id in self._pending_orders:
            del self._pending_orders[order.order_id]

        # Create trade result
        trade = TradeResult(
            trade_id=f"trade_{uuid.uuid4().hex[:8]}",
            order_id=order.order_id,
            strategy_id=order.strategy_id,
            market_id=order.market_id,
            side=order.side,
            token_type=order.token_type,
            price=fill_price,
            size=order.size,
            mode="paper",
        )

        logger.info(f"Paper order filled: {order.order_id} @ {fill_price}")

        # Notify callbacks
        self._notify_fill(trade)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a paper order."""
        if order_id not in self._pending_orders:
            return False

        order = self._pending_orders[order_id]
        order.status = OrderStatus.CANCELLED
        order.updated_at = datetime.now()

        del self._pending_orders[order_id]
        logger.info(f"Paper order cancelled: {order_id}")

        return True

    async def cancel_all_orders(self, strategy_id: Optional[str] = None) -> int:
        """Cancel all paper orders."""
        to_cancel = []

        for order_id, order in self._pending_orders.items():
            if strategy_id is None or order.strategy_id == strategy_id:
                to_cancel.append(order_id)

        for order_id in to_cancel:
            await self.cancel_order(order_id)

        return len(to_cancel)

    async def check_fills(self) -> List[TradeResult]:
        """
        Check and execute fills for pending orders based on current prices.

        Returns:
            List of fills that occurred
        """
        fills = []

        for order_id in list(self._pending_orders.keys()):
            order = self._pending_orders.get(order_id)
            if not order:
                continue

            # Handle both enum and string token_type
            token_type_str = order.token_type.value if hasattr(order.token_type, 'value') else order.token_type
            market_price = self._get_market_price(order.market_id, token_type_str)

            should_fill = False
            if order.side == TradeSide.BUY and market_price <= order.price:
                should_fill = True
            elif order.side == TradeSide.SELL and market_price >= order.price:
                should_fill = True

            if should_fill and random.random() <= self.fill_probability:
                await self._simulate_fill(order, market_price)
                fills.append(
                    TradeResult(
                        trade_id=f"trade_{uuid.uuid4().hex[:8]}",
                        order_id=order.order_id,
                        strategy_id=order.strategy_id,
                        market_id=order.market_id,
                        side=order.side,
                        token_type=order.token_type,
                        price=self._apply_slippage(market_price, order.side),
                        size=order.size,
                        mode="paper",
                    )
                )

        return fills


class LiveOrderManager(OrderManager):
    """
    Live order manager using py-clob-client.

    Executes real orders on Polymarket.
    """

    def __init__(self, clob_client=None):
        """
        Initialize live order manager.

        Args:
            clob_client: Initialized py-clob-client ClobClient instance
        """
        super().__init__()
        self._client = clob_client

    def set_client(self, clob_client) -> None:
        """Set the CLOB client."""
        self._client = clob_client

    async def place_order(
        self,
        signal: OrderSignal,
        strategy_id: str,
        market_id: str,
        order_type: OrderType = OrderType.GTC,
    ) -> Order:
        """Place a live order on Polymarket."""
        if self._client is None:
            raise RuntimeError("CLOB client not configured")

        order_id = f"live_{uuid.uuid4().hex[:8]}"

        # Create order object
        order = Order(
            order_id=order_id,
            strategy_id=strategy_id,
            market_id=market_id,
            side=signal.side,
            token_type=signal.token_type,
            price=signal.target_price,
            size=signal.size,
            order_type=order_type,
        )

        try:
            # Import py-clob-client types
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY, SELL

            # Build order args
            side = BUY if signal.side == TradeSide.BUY else SELL
            order_args = OrderArgs(
                price=signal.target_price,
                size=signal.size,
                side=side,
                # Handle both enum and string token_type
                token_id=signal.token_type.value if hasattr(signal.token_type, 'value') else signal.token_type,
            )

            # Create and submit order
            signed_order = self._client.create_order(order_args)
            response = self._client.post_order(signed_order, order_type.value)

            # Update order with response
            if response and "orderID" in response:
                order.order_id = response["orderID"]

            self._pending_orders[order.order_id] = order
            logger.info(f"Live order placed: {order.order_id}")

        except Exception as e:
            logger.error(f"Failed to place live order: {e}")
            order.status = OrderStatus.REJECTED
            raise

        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a live order."""
        if self._client is None:
            raise RuntimeError("CLOB client not configured")

        if order_id not in self._pending_orders:
            return False

        try:
            self._client.cancel(order_id)

            order = self._pending_orders[order_id]
            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.now()

            del self._pending_orders[order_id]
            logger.info(f"Live order cancelled: {order_id}")

            return True

        except Exception as e:
            logger.error(f"Failed to cancel live order: {e}")
            return False

    async def cancel_all_orders(self, strategy_id: Optional[str] = None) -> int:
        """Cancel all live orders."""
        if self._client is None:
            raise RuntimeError("CLOB client not configured")

        to_cancel = []

        for order_id, order in self._pending_orders.items():
            if strategy_id is None or order.strategy_id == strategy_id:
                to_cancel.append(order_id)

        cancelled = 0
        for order_id in to_cancel:
            if await self.cancel_order(order_id):
                cancelled += 1

        return cancelled
