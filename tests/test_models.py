"""Tests for core data models."""

import pytest
from datetime import datetime

from polymoney.core.models import (
    PriceData,
    Candlestick,
    Order,
    OrderSignal,
    Position,
    TradeResult,
    MarketStart,
    MarketEnd,
    MarketSettled,
    OrderStatus,
    OrderType,
    TradeSide,
    TokenType,
)


class TestPriceData:
    """Tests for PriceData model."""

    def test_creation(self):
        price = PriceData(
            market_id="test-market",
            up_price=0.6,
            down_price=0.4,
        )
        assert price.market_id == "test-market"
        assert price.up_price == 0.6
        assert price.down_price == 0.4

    def test_mid_price(self):
        price = PriceData(
            market_id="test",
            up_price=0.6,
            down_price=0.4,
        )
        assert price.mid_price == 0.5

    def test_spread(self):
        price = PriceData(
            market_id="test",
            up_price=0.6,
            down_price=0.4,
            spread=0.02,
        )
        assert price.spread == 0.02


class TestCandlestick:
    """Tests for Candlestick model."""

    def test_creation(self):
        candle = Candlestick(
            market_id="test-market",
            interval="1m",
            timestamp=datetime.now(),
            token_type=TokenType.YES,
            open=0.50,
            high=0.55,
            low=0.48,
            close=0.52,
            volume=1000,
        )
        assert candle.market_id == "test-market"
        assert candle.interval == "1m"
        assert candle.open == 0.50
        assert candle.high == 0.55
        assert candle.low == 0.48
        assert candle.close == 0.52
        assert candle.volume == 1000


class TestOrder:
    """Tests for Order model."""

    def test_creation(self):
        order = Order(
            order_id="order-123",
            strategy_id="test-strategy",
            market_id="test-market",
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            price=0.50,
            size=100,
        )
        assert order.order_id == "order-123"
        assert order.status == OrderStatus.PENDING
        assert order.remaining_size == 100

    def test_remaining_size(self):
        order = Order(
            order_id="order-123",
            strategy_id="test-strategy",
            market_id="test-market",
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            price=0.50,
            size=100,
            filled_size=30,
        )
        assert order.remaining_size == 70

    def test_is_complete(self):
        order = Order(
            order_id="order-123",
            strategy_id="test-strategy",
            market_id="test-market",
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            price=0.50,
            size=100,
            status=OrderStatus.FILLED,
        )
        assert order.is_complete is True


class TestPosition:
    """Tests for Position model."""

    def test_creation(self):
        pos = Position()
        assert pos.shares == 0
        assert pos.cost == 0
        assert pos.avg_price == 0

    def test_add(self):
        pos = Position()
        pos.add(100, 0.50)
        assert pos.shares == 100
        assert pos.cost == 50
        assert pos.avg_price == 0.50

    def test_add_multiple(self):
        pos = Position()
        pos.add(100, 0.50)  # 50
        pos.add(100, 0.60)  # 60
        assert pos.shares == 200
        assert pos.cost == 110
        assert pos.avg_price == 0.55

    def test_reset(self):
        pos = Position()
        pos.add(100, 0.50)
        pos.reset()
        assert pos.shares == 0
        assert pos.cost == 0


class TestOrderSignal:
    """Tests for OrderSignal model."""

    def test_creation(self):
        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            target_price=0.50,
            size=100,
        )
        assert signal.side == TradeSide.BUY
        assert signal.target_price == 0.50
        assert signal.size == 100


class TestTradeResult:
    """Tests for TradeResult model."""

    def test_creation(self):
        trade = TradeResult(
            trade_id="trade-123",
            order_id="order-123",
            strategy_id="test-strategy",
            market_id="test-market",
            side=TradeSide.BUY,
            token_type=TokenType.YES,
            price=0.50,
            size=100,
        )
        assert trade.trade_id == "trade-123"
        assert trade.price == 0.50
        assert trade.size == 100
        assert trade.mode == "paper"


class TestMarketEvents:
    """Tests for market event models."""

    def test_market_start(self):
        event = MarketStart(
            market_id="test-market",
            title="Test Question?",
        )
        assert event.market_id == "test-market"
        assert event.title == "Test Question?"

    def test_market_end(self):
        event = MarketEnd(
            market_id="test-market",
            final_up_price=0.95,
            final_down_price=0.05,
        )
        assert event.market_id == "test-market"
        assert event.final_up_price == 0.95

    def test_market_settled(self):
        event = MarketSettled(
            market_id="test-market",
            winner=TokenType.YES,
        )
        assert event.market_id == "test-market"
        assert event.winner == TokenType.YES
