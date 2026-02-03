"""Integration tests for order managers."""

import pytest
import asyncio

from polymoney.strategy.order_manager import (
    PaperOrderManager,
    LiveOrderManager,
    OrderManager,
)
from polymoney.core.models import (
    OrderSignal,
    OrderStatus,
    OrderType,
    TradeSide,
    TokenType,
    TradeResult,
)


class TestPaperOrderManager:
    """Integration tests for PaperOrderManager."""

    @pytest.fixture
    def manager(self):
        """Create a PaperOrderManager instance."""
        return PaperOrderManager(
            slippage_pct=0.001,
            fill_probability=1.0,
            latency_ms=0.0,
        )

    @pytest.mark.asyncio
    async def test_place_order(self, manager):
        """Test placing an order."""
        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        order = await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        assert order is not None
        assert order.strategy_id == "test-strategy"
        assert order.market_id == "test-market"
        assert order.side == TradeSide.BUY
        assert order.size == 100

    @pytest.mark.asyncio
    async def test_order_fill_with_market_price(self, manager):
        """Test order fills when market price matches."""
        # Set market price below our buy limit
        manager.set_market_price("test-market", up_price=0.45, down_price=0.55)

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        order = await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        # Order should be filled since market price < limit
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == 100

    @pytest.mark.asyncio
    async def test_order_no_fill_above_limit(self, manager):
        """Test order does not fill when market price is above limit."""
        # Set market price above our buy limit
        manager.set_market_price("test-market", up_price=0.60, down_price=0.40)

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        order = await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        # Order should remain pending
        assert order.status == OrderStatus.PENDING

    @pytest.mark.asyncio
    async def test_sell_order_fill(self, manager):
        """Test sell order fills when price meets limit."""
        # Set market price above our sell limit
        manager.set_market_price("test-market", up_price=0.55, down_price=0.45)

        signal = OrderSignal(
            side=TradeSide.SELL,
            token_type=TokenType.YES,
            target_price=0.50,
            size=50,
        )

        order = await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        # Sell order should fill since market price > limit
        assert order.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_cancel_order(self, manager):
        """Test cancelling a pending order."""
        # Set price so order won't fill
        manager.set_market_price("test-market", up_price=0.80, down_price=0.20)

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        order = await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        assert order.status == OrderStatus.PENDING

        result = await manager.cancel_order(order.order_id)
        assert result is True

        # Order should be removed from pending
        assert manager.get_order(order.order_id) is None

    @pytest.mark.asyncio
    async def test_cancel_all_orders(self, manager):
        """Test cancelling all orders."""
        manager.set_market_price("test-market", up_price=0.80, down_price=0.20)

        # Place multiple orders
        for i in range(3):
            signal = OrderSignal(
                side=TradeSide.BUY,
                token_type=TokenType.YES,
                target_price=0.50,
                size=100,
            )
            await manager.place_order(
                signal=signal,
                strategy_id="test-strategy",
                market_id="test-market",
            )

        assert len(manager.pending_orders) == 3

        cancelled = await manager.cancel_all_orders()
        assert cancelled == 3
        assert len(manager.pending_orders) == 0

    @pytest.mark.asyncio
    async def test_cancel_orders_by_strategy(self, manager):
        """Test cancelling orders filtered by strategy."""
        manager.set_market_price("test-market", up_price=0.80, down_price=0.20)

        # Place orders for different strategies
        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        await manager.place_order(signal, "strategy-1", "test-market")
        await manager.place_order(signal, "strategy-1", "test-market")
        await manager.place_order(signal, "strategy-2", "test-market")

        assert len(manager.pending_orders) == 3

        # Cancel only strategy-1 orders
        cancelled = await manager.cancel_all_orders(strategy_id="strategy-1")
        assert cancelled == 2
        assert len(manager.pending_orders) == 1

    @pytest.mark.asyncio
    async def test_fill_callback(self, manager):
        """Test fill notification callbacks."""
        fills = []

        def on_fill(trade: TradeResult):
            fills.append(trade)

        manager.add_fill_callback(on_fill)
        manager.set_market_price("test-market", up_price=0.45, down_price=0.55)

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        # Callback should have been called
        assert len(fills) == 1
        assert fills[0].strategy_id == "test-strategy"
        assert fills[0].mode == "paper"

    @pytest.mark.asyncio
    async def test_slippage_simulation(self, manager):
        """Test that slippage is applied correctly."""
        fills = []

        def on_fill(trade: TradeResult):
            fills.append(trade)

        manager.add_fill_callback(on_fill)

        # Use a significant slippage
        manager.slippage_pct = 0.01  # 1%
        manager.set_market_price("test-market", up_price=0.50, down_price=0.50)

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.55,  # Above market price
            size=100,
        )

        await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        # Fill price should be higher than market (worse for buyer)
        assert len(fills) == 1
        assert fills[0].price > 0.50

    @pytest.mark.asyncio
    async def test_get_orders_by_strategy(self, manager):
        """Test getting orders filtered by strategy."""
        manager.set_market_price("test-market", up_price=0.80, down_price=0.20)

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        await manager.place_order(signal, "strategy-1", "test-market")
        await manager.place_order(signal, "strategy-2", "test-market")
        await manager.place_order(signal, "strategy-1", "test-market")

        orders = manager.get_orders_by_strategy("strategy-1")
        assert len(orders) == 2

        orders = manager.get_orders_by_strategy("strategy-2")
        assert len(orders) == 1

    @pytest.mark.asyncio
    async def test_check_fills(self, manager):
        """Test checking and executing fills for pending orders."""
        # Set price so orders won't fill initially
        manager.set_market_price("test-market", up_price=0.80, down_price=0.20)

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        order = await manager.place_order(
            signal=signal,
            strategy_id="test-strategy",
            market_id="test-market",
        )

        assert order.status == OrderStatus.PENDING
        assert len(manager.pending_orders) == 1

        # Now change price to trigger fill
        manager.set_market_price("test-market", up_price=0.40, down_price=0.60)
        await manager.check_fills()

        # Order should now be filled and removed
        assert len(manager.pending_orders) == 0


class TestLiveOrderManager:
    """Tests for LiveOrderManager (without real client)."""

    def test_initialization(self):
        """Test LiveOrderManager initialization."""
        manager = LiveOrderManager()
        assert manager._client is None
        assert len(manager.pending_orders) == 0

    def test_set_client(self):
        """Test setting the CLOB client."""
        manager = LiveOrderManager()
        mock_client = object()  # Mock client
        manager.set_client(mock_client)
        assert manager._client == mock_client

    @pytest.mark.asyncio
    async def test_place_order_without_client(self):
        """Test placing order without client raises error."""
        manager = LiveOrderManager()

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )

        with pytest.raises(RuntimeError, match="CLOB client not configured"):
            await manager.place_order(
                signal=signal,
                strategy_id="test-strategy",
                market_id="test-market",
            )

    @pytest.mark.asyncio
    async def test_cancel_order_without_client(self):
        """Test cancelling order without client raises error."""
        manager = LiveOrderManager()

        with pytest.raises(RuntimeError, match="CLOB client not configured"):
            await manager.cancel_order("some-order-id")

    @pytest.mark.asyncio
    async def test_cancel_all_without_client(self):
        """Test cancelling all orders without client raises error."""
        manager = LiveOrderManager()

        with pytest.raises(RuntimeError, match="CLOB client not configured"):
            await manager.cancel_all_orders()
