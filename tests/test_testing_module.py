"""
Tests for the testing module (replayer, runner, analytics).
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polymoney.core.models import Candlestick, PriceData, TokenType
from polymoney.testing.replayer import MarketDataReplayer, MarketReplayData
from polymoney.testing.runner import TestRunner, TestResult, TestConfig
from polymoney.testing.analytics import PerformanceAnalytics, AggregateMetrics


# ============================================================================
# Test Data Fixtures
# ============================================================================

@pytest.fixture
def sample_candlesticks():
    """Generate sample candlestick data for testing."""
    base_time = datetime(2026, 2, 3, 10, 0, 0)
    candlesticks = []
    
    # Simulate 15 minutes of 1-minute candles with price movement
    prices = [
        0.50, 0.52, 0.48, 0.55, 0.53,  # Minutes 0-4
        0.51, 0.49, 0.47, 0.50, 0.52,  # Minutes 5-9
        0.58, 0.62, 0.65, 0.60, 0.58,  # Minutes 10-14 (trending up)
    ]
    
    for i, close_price in enumerate(prices):
        candle = Candlestick(
            market_id="test-market-123",
            interval="1m",
            timestamp=base_time + timedelta(minutes=i),
            token_type=TokenType.YES,
            open=close_price - 0.01,
            high=close_price + 0.02,
            low=close_price - 0.02,
            close=close_price,
            volume=100.0,
        )
        candlesticks.append(candle)
    
    return candlesticks


@pytest.fixture
def mock_storage(sample_candlesticks):
    """Create a mock DataStorage that returns sample candlesticks."""
    storage = MagicMock()
    storage.get_candlesticks = AsyncMock(return_value=sample_candlesticks)
    return storage


@pytest.fixture
def sample_test_result():
    """Create a sample TestResult for analytics testing."""
    return TestResult(
        market_id="test-market-123",
        strategy_id="test_strategy_001",
        start_time=datetime(2026, 2, 3, 10, 0, 0),
        end_time=datetime(2026, 2, 3, 10, 15, 0),
        settlement_winner="up",
        up_shares=50.0,
        up_cost=24.0,
        down_shares=45.0,
        down_cost=22.5,
        total_cost=46.5,
        settlement_value=50.0,
        pnl=3.5,
        roi=0.0753,
        effective_cost_rate=1.033,
        balance_ratio=0.9,
        hedged_position=45.0,
        orders_submitted=10,
        orders_filled=8,
        fill_rate=0.8,
        price_history=[
            {"timestamp": "2026-02-03T10:00:00", "up_price": 0.50, "down_price": 0.50},
            {"timestamp": "2026-02-03T10:05:00", "up_price": 0.55, "down_price": 0.45},
            {"timestamp": "2026-02-03T10:10:00", "up_price": 0.60, "down_price": 0.40},
            {"timestamp": "2026-02-03T10:15:00", "up_price": 0.58, "down_price": 0.42},
        ],
        trade_log=[
            {"type": "order_submitted", "timestamp": "2026-02-03T10:01:00", "token_type": "yes", "price": 0.48, "size": 10.0},
            {"type": "order_filled", "timestamp": "2026-02-03T10:01:00", "token_type": "yes", "price": 0.48, "size": 10.0, "cost": 4.8},
        ],
    )


# ============================================================================
# MarketDataReplayer Tests
# ============================================================================

class TestMarketDataReplayer:
    """Tests for MarketDataReplayer."""

    @pytest.mark.asyncio
    async def test_load_market_data(self, mock_storage, sample_candlesticks):
        """Test loading market data from storage."""
        replayer = MarketDataReplayer(storage=mock_storage, replay_speed=0)
        
        market_data = await replayer.load_market(
            market_id="test-market-123",
            title="Test Market",
            settlement_winner="up",
        )
        
        assert market_data.market_id == "test-market-123"
        assert market_data.title == "Test Market"
        assert len(market_data.candlesticks) == 15
        assert market_data.settlement_winner == "up"
        mock_storage.get_candlesticks.assert_called_once()

    def test_candlestick_to_price_data(self, mock_storage):
        """Test converting candlestick to PriceData."""
        replayer = MarketDataReplayer(storage=mock_storage, replay_speed=0)
        
        candle = Candlestick(
            market_id="test-market-123",
            interval="1m",
            timestamp=datetime.now(),
            token_type=TokenType.YES,
            open=0.50,
            high=0.55,
            low=0.48,
            close=0.52,
            volume=100.0,
        )
        
        price_data = replayer.candlestick_to_price_data(candle)
        
        assert price_data.up_price == 0.52
        assert price_data.down_price == 0.48
        assert price_data.up_price + price_data.down_price == 1.0

    def test_price_data_bounds(self, mock_storage):
        """Test that price data stays within valid bounds."""
        replayer = MarketDataReplayer(storage=mock_storage, replay_speed=0)
        
        # Test extreme high price
        candle_high = Candlestick(
            market_id="test",
            interval="1m",
            timestamp=datetime.now(),
            token_type=TokenType.YES,
            open=0.99,
            high=1.0,
            low=0.98,
            close=0.999,  # Very high
            volume=100.0,
        )
        price_data = replayer.candlestick_to_price_data(candle_high)
        assert 0.01 <= price_data.up_price <= 0.99
        assert 0.01 <= price_data.down_price <= 0.99
        
        # Test extreme low price
        candle_low = Candlestick(
            market_id="test",
            interval="1m",
            timestamp=datetime.now(),
            token_type=TokenType.YES,
            open=0.01,
            high=0.02,
            low=0.005,
            close=0.001,  # Very low
            volume=100.0,
        )
        price_data = replayer.candlestick_to_price_data(candle_low)
        assert 0.01 <= price_data.up_price <= 0.99
        assert 0.01 <= price_data.down_price <= 0.99

    @pytest.mark.asyncio
    async def test_replay_emits_events(self, mock_storage, sample_candlesticks):
        """Test that replay emits correct events."""
        replayer = MarketDataReplayer(storage=mock_storage, replay_speed=0)
        
        events_received = []
        def callback(event_type, market_id, data):
            events_received.append((event_type, market_id))
        
        replayer.add_callback(callback)
        
        market_data = await replayer.load_market(
            market_id="test-market-123",
            title="Test Market",
            settlement_winner="up",
        )
        
        await replayer.replay(market_data)
        
        # Should have: market_start, 15 price_updates, market_end, market_settled
        event_types = [e[0] for e in events_received]
        assert event_types[0] == "market_start"
        assert event_types[-2] == "market_end"
        assert event_types[-1] == "market_settled"
        assert event_types.count("price_update") == 15

    def test_callback_management(self, mock_storage):
        """Test adding and removing callbacks."""
        replayer = MarketDataReplayer(storage=mock_storage, replay_speed=0)
        
        callback = lambda *args: None
        
        replayer.add_callback(callback)
        assert callback in replayer._callbacks
        
        replayer.remove_callback(callback)
        assert callback not in replayer._callbacks


# ============================================================================
# TestRunner Tests
# ============================================================================

class TestTestRunner:
    """Tests for TestRunner."""

    def test_test_result_to_dict(self, sample_test_result):
        """Test TestResult.to_dict() method."""
        result_dict = sample_test_result.to_dict()
        
        assert result_dict["market_id"] == "test-market-123"
        assert result_dict["settlement_winner"] == "up"
        assert result_dict["metrics"]["pnl"] == 3.5
        assert result_dict["trading_stats"]["fill_rate"] == 0.8

    @pytest.mark.asyncio
    async def test_runner_initialization(self, mock_storage):
        """Test TestRunner initialization."""
        from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy
        
        config = TestConfig(
            strategy_class=PositionArbitrageStrategy,
            strategy_params={"target_cost": 0.98},
            position_size=100.0,
            replay_speed=0,
        )
        
        runner = TestRunner(storage=mock_storage, config=config)
        
        assert runner.strategy is not None
        assert runner.order_manager is not None
        assert runner.replayer is not None
        assert not runner.is_running

    @pytest.mark.asyncio
    async def test_runner_replay_no_data(self, mock_storage):
        """Test runner handles missing data gracefully."""
        from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy
        
        # Return empty candlesticks
        mock_storage.get_candlesticks = AsyncMock(return_value=[])
        
        config = TestConfig(
            strategy_class=PositionArbitrageStrategy,
            position_size=100.0,
            replay_speed=0,
        )
        
        runner = TestRunner(storage=mock_storage, config=config)
        result = await runner.run_replay(market_id="empty-market", title="Empty")
        
        # Should return result with zeros
        assert result.market_id == "empty-market"
        assert result.up_shares == 0.0
        assert result.down_shares == 0.0


# ============================================================================
# PerformanceAnalytics Tests
# ============================================================================

class TestPerformanceAnalytics:
    """Tests for PerformanceAnalytics."""

    def test_add_result(self, sample_test_result):
        """Test adding results to analytics."""
        analytics = PerformanceAnalytics()
        
        analytics.add_result(sample_test_result)
        
        assert len(analytics.results) == 1
        assert analytics.results[0].market_id == "test-market-123"

    def test_compute_aggregate_metrics_empty(self):
        """Test aggregate metrics with no results."""
        analytics = PerformanceAnalytics()
        
        metrics = analytics.compute_aggregate_metrics()
        
        assert metrics.total_markets == 0
        assert metrics.total_pnl == 0.0

    def test_compute_aggregate_metrics(self, sample_test_result):
        """Test aggregate metrics computation."""
        analytics = PerformanceAnalytics()
        
        # Add multiple results
        analytics.add_result(sample_test_result)
        
        # Create a second result (losing)
        result2 = TestResult(
            market_id="test-market-456",
            strategy_id="test_strategy_002",
            start_time=datetime.now(),
            end_time=datetime.now(),
            settlement_winner="down",
            up_shares=30.0,
            up_cost=15.0,
            down_shares=25.0,
            down_cost=12.5,
            total_cost=27.5,
            settlement_value=25.0,
            pnl=-2.5,
            roi=-0.091,
            effective_cost_rate=1.1,
            balance_ratio=0.833,
            hedged_position=25.0,
            orders_submitted=8,
            orders_filled=6,
            fill_rate=0.75,
        )
        analytics.add_result(result2)
        
        metrics = analytics.compute_aggregate_metrics()
        
        assert metrics.total_markets == 2
        assert metrics.profitable_markets == 1
        assert metrics.win_rate == 0.5
        assert metrics.total_pnl == 1.0  # 3.5 - 2.5
        assert metrics.best_pnl == 3.5
        assert metrics.worst_pnl == -2.5

    def test_generate_report(self, sample_test_result):
        """Test report generation."""
        analytics = PerformanceAnalytics()
        analytics.add_result(sample_test_result)
        
        report = analytics.generate_report()
        
        assert "PERFORMANCE ANALYTICS REPORT" in report
        assert "test-market-123" in report
        assert "Total Markets Tested: 1" in report
        assert "PnL" in report

    def test_generate_report_empty(self):
        """Test report generation with no results."""
        analytics = PerformanceAnalytics()
        
        report = analytics.generate_report()
        
        assert "No results to report" in report

    def test_clear_results(self, sample_test_result):
        """Test clearing results."""
        analytics = PerformanceAnalytics()
        analytics.add_result(sample_test_result)
        
        analytics.clear()
        
        assert len(analytics.results) == 0


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_replay_workflow(self, mock_storage, sample_candlesticks):
        """Test complete workflow: replay -> analyze -> report."""
        from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy
        
        # Setup
        config = TestConfig(
            strategy_class=PositionArbitrageStrategy,
            strategy_params={"target_cost": 0.98, "batch_size": 5.0},
            position_size=100.0,
            replay_speed=0,
        )
        
        runner = TestRunner(storage=mock_storage, config=config)
        analytics = PerformanceAnalytics()
        
        # Run replay
        result = await runner.run_replay(
            market_id="test-market-123",
            title="BTC 15min Up/Down",
            settlement_winner="up",
        )
        
        # Add to analytics
        analytics.add_result(result)
        
        # Generate report
        report = analytics.generate_report()
        
        # Verify
        assert result.market_id == "test-market-123"
        assert len(analytics.results) == 1
        assert "test-market-123" in report

    @pytest.mark.asyncio
    async def test_multiple_markets_replay(self, mock_storage, sample_candlesticks):
        """Test running multiple market replays."""
        from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy
        
        analytics = PerformanceAnalytics()
        
        # Run multiple markets
        markets = [
            ("market-1", "up"),
            ("market-2", "down"),
            ("market-3", "up"),
        ]
        
        for market_id, winner in markets:
            config = TestConfig(
                strategy_class=PositionArbitrageStrategy,
                position_size=100.0,
                replay_speed=0,
            )
            runner = TestRunner(storage=mock_storage, config=config)
            result = await runner.run_replay(
                market_id=market_id,
                settlement_winner=winner,
            )
            analytics.add_result(result)
        
        # Verify aggregate
        metrics = analytics.compute_aggregate_metrics()
        assert metrics.total_markets == 3
