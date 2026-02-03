"""Integration tests for market data service with mock WebSocket."""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from polymoney.data.market_data_service import (
    MarketDataService,
    EVENT_PRICE_UPDATE,
    EVENT_TRADE,
    EVENT_ORDERBOOK,
    EVENT_CANDLESTICK_COMPLETE,
    EVENT_MARKET_START,
    EVENT_MARKET_END,
    EVENT_MARKET_SETTLED,
)
from polymoney.data.websocket_client import (
    WebSocketManager,
    ConnectionState,
    MarketSubscription,
)
from polymoney.core.models import PriceData, TokenType


class TestWebSocketManager:
    """Tests for WebSocketManager."""

    def test_initialization(self):
        """Test WebSocketManager initialization."""
        manager = WebSocketManager()
        assert manager.state == ConnectionState.DISCONNECTED
        assert manager.is_connected is False
        assert len(manager.subscribed_markets) == 0

    def test_add_callback(self):
        """Test adding event callbacks."""
        manager = WebSocketManager()
        callbacks_called = []

        def callback(event_type, market_id, data):
            callbacks_called.append((event_type, market_id, data))

        manager.add_callback(callback)
        assert callback in manager._callbacks

    def test_remove_callback(self):
        """Test removing event callbacks."""
        manager = WebSocketManager()

        def callback(event_type, market_id, data):
            pass

        manager.add_callback(callback)
        manager.remove_callback(callback)
        assert callback not in manager._callbacks

    def test_emit_event(self):
        """Test event emission to callbacks."""
        manager = WebSocketManager()
        events = []

        def callback(event_type, market_id, data):
            events.append({"type": event_type, "market": market_id, "data": data})

        manager.add_callback(callback)
        manager._emit_event("test_event", "test-market", {"key": "value"})

        assert len(events) == 1
        assert events[0]["type"] == "test_event"
        assert events[0]["market"] == "test-market"

    @pytest.mark.asyncio
    async def test_start_stop(self):
        """Test starting and stopping the manager."""
        manager = WebSocketManager()

        await manager.start()
        assert manager._running is True

        await manager.stop()
        assert manager._running is False
        assert manager.state == ConnectionState.DISCONNECTED

    def test_health_status(self):
        """Test health status reporting."""
        manager = WebSocketManager()
        status = manager.get_health_status()

        assert "state" in status
        assert "is_connected" in status
        assert "subscribed_markets" in status
        assert "markets" in status

    def test_market_subscription_state(self):
        """Test MarketSubscription state management."""
        sub = MarketSubscription(
            market_id="test-market",
            token_ids=["token1", "token2"],
        )

        assert sub.market_id == "test-market"
        assert len(sub.token_ids) == 2
        assert sub.best_bid == 0.0
        assert sub.best_ask == 1.0
        assert sub.last_update is None


class TestMarketDataService:
    """Integration tests for MarketDataService."""

    @pytest.fixture
    def service(self):
        """Create a MarketDataService instance."""
        return MarketDataService(intervals=["1m", "5m"])

    @pytest.mark.asyncio
    async def test_initialization(self, service):
        """Test service initialization."""
        assert service.is_running is False
        assert len(service.subscribed_markets) == 0

    @pytest.mark.asyncio
    async def test_start_stop(self, service):
        """Test starting and stopping the service."""
        await service.start()
        assert service.is_running is True

        await service.stop()
        assert service.is_running is False

    @pytest.mark.asyncio
    async def test_add_listener(self, service):
        """Test adding event listeners."""
        events = []

        def listener(event_type, market_id, data):
            events.append((event_type, market_id, data))

        service.add_listener(listener)
        assert listener in service._listeners

    @pytest.mark.asyncio
    async def test_remove_listener(self, service):
        """Test removing event listeners."""
        def listener(event_type, market_id, data):
            pass

        service.add_listener(listener)
        service.remove_listener(listener)
        assert listener not in service._listeners

    @pytest.mark.asyncio
    async def test_emit_event(self, service):
        """Test event emission."""
        events = []

        def listener(event_type, market_id, data):
            events.append({"type": event_type, "market": market_id})

        service.add_listener(listener)
        service._emit("test_event", "test-market", {})

        assert len(events) == 1
        assert events[0]["type"] == "test_event"

    @pytest.mark.asyncio
    async def test_subscribe_market_emits_start_event(self, service):
        """Test that subscribing emits market start event."""
        events = []

        def listener(event_type, market_id, data):
            events.append({"type": event_type, "market": market_id, "data": data})

        service.add_listener(listener)
        await service.start()

        # Mock the WebSocket subscription to avoid real connection
        with patch.object(service._ws_manager, "subscribe", new_callable=AsyncMock):
            await service.subscribe_market(
                market_id="test-market",
                token_ids=["token1", "token2"],
                title="Test Market?",
            )

        # Should have received market start event
        start_events = [e for e in events if e["type"] == EVENT_MARKET_START]
        assert len(start_events) == 1
        assert start_events[0]["data"].market_id == "test-market"
        assert start_events[0]["data"].title == "Test Market?"

        await service.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_market(self, service):
        """Test unsubscribing from a market."""
        await service.start()

        with patch.object(service._ws_manager, "subscribe", new_callable=AsyncMock):
            await service.subscribe_market(
                market_id="test-market",
                token_ids=["token1"],
                title="Test",
            )

        assert "test-market" in service.subscribed_markets

        with patch.object(service._ws_manager, "unsubscribe", new_callable=AsyncMock):
            await service.unsubscribe_market("test-market")

        assert "test-market" not in service.subscribed_markets
        await service.stop()

    @pytest.mark.asyncio
    async def test_emit_market_end(self, service):
        """Test emitting market end event."""
        events = []

        def listener(event_type, market_id, data):
            events.append({"type": event_type, "data": data})

        service.add_listener(listener)
        service.emit_market_end("test-market", final_up_price=0.95, final_down_price=0.05)

        end_events = [e for e in events if e["type"] == EVENT_MARKET_END]
        assert len(end_events) == 1
        assert end_events[0]["data"].final_up_price == 0.95

    @pytest.mark.asyncio
    async def test_emit_market_settled(self, service):
        """Test emitting market settlement event."""
        events = []

        def listener(event_type, market_id, data):
            events.append({"type": event_type, "data": data})

        service.add_listener(listener)
        service.emit_market_settled("test-market", winner="yes")

        settled_events = [e for e in events if e["type"] == EVENT_MARKET_SETTLED]
        assert len(settled_events) == 1
        assert settled_events[0]["data"].winner == TokenType.YES

    @pytest.mark.asyncio
    async def test_get_market_info(self, service):
        """Test getting market info after subscription."""
        await service.start()

        with patch.object(service._ws_manager, "subscribe", new_callable=AsyncMock):
            await service.subscribe_market(
                market_id="test-market",
                token_ids=["token1"],
                title="Test Question?",
                metadata={"category": "sports"},
            )

        info = service.get_market_info("test-market")
        assert info is not None
        assert info["title"] == "Test Question?"
        assert info["metadata"]["category"] == "sports"

        await service.stop()

    @pytest.mark.asyncio
    async def test_health_status(self, service):
        """Test health status reporting."""
        status = service.get_health_status()

        assert "is_running" in status
        assert "active_markets" in status
        assert "websocket" in status
        assert "aggregators" in status

    @pytest.mark.asyncio
    async def test_price_update_forwarding(self, service):
        """Test that price updates are forwarded to listeners."""
        events = []

        def listener(event_type, market_id, data):
            events.append({"type": event_type, "market": market_id, "data": data})

        service.add_listener(listener)

        # Simulate a price update event from WebSocket
        price_data = PriceData(
            market_id="test-market",
            up_price=0.6,
            down_price=0.4,
        )
        service._on_ws_event("price_update", "test-market", price_data)

        price_events = [e for e in events if e["type"] == EVENT_PRICE_UPDATE]
        assert len(price_events) == 1
        assert price_events[0]["data"].up_price == 0.6

    @pytest.mark.asyncio
    async def test_trade_event_forwarding(self, service):
        """Test that trade events are forwarded and update aggregator."""
        await service.start()

        with patch.object(service._ws_manager, "subscribe", new_callable=AsyncMock):
            await service.subscribe_market(
                market_id="test-market",
                token_ids=["token1"],
                title="Test",
            )

        events = []

        def listener(event_type, market_id, data):
            events.append({"type": event_type})

        service.add_listener(listener)

        # Simulate a trade event
        trade_data = {"price": "0.55", "size": "100"}
        service._on_ws_event("trade", "test-market", trade_data)

        trade_events = [e for e in events if e["type"] == EVENT_TRADE]
        assert len(trade_events) == 1

        await service.stop()

    @pytest.mark.asyncio
    async def test_orderbook_event_forwarding(self, service):
        """Test that orderbook events are forwarded."""
        events = []

        def listener(event_type, market_id, data):
            events.append({"type": event_type})

        service.add_listener(listener)

        # Simulate an orderbook event
        orderbook_data = {"bids": [], "asks": []}
        service._on_ws_event("orderbook", "test-market", orderbook_data)

        ob_events = [e for e in events if e["type"] == EVENT_ORDERBOOK]
        assert len(ob_events) == 1


class TestCandlestickIntegration:
    """Tests for candlestick aggregation integration."""

    @pytest.fixture
    def service(self):
        """Create a MarketDataService instance."""
        return MarketDataService(intervals=["1m"])

    @pytest.mark.asyncio
    async def test_aggregator_created_on_subscribe(self, service):
        """Test that aggregator is created when subscribing."""
        await service.start()

        with patch.object(service._ws_manager, "subscribe", new_callable=AsyncMock):
            await service.subscribe_market(
                market_id="test-market",
                token_ids=["token1"],
                title="Test",
            )

        assert "test-market" in service._aggregators
        await service.stop()

    @pytest.mark.asyncio
    async def test_aggregator_removed_on_unsubscribe(self, service):
        """Test that aggregator is removed when unsubscribing."""
        await service.start()

        with patch.object(service._ws_manager, "subscribe", new_callable=AsyncMock):
            await service.subscribe_market(
                market_id="test-market",
                token_ids=["token1"],
                title="Test",
            )

        assert "test-market" in service._aggregators

        with patch.object(service._ws_manager, "unsubscribe", new_callable=AsyncMock):
            await service.unsubscribe_market("test-market")

        assert "test-market" not in service._aggregators
        await service.stop()
