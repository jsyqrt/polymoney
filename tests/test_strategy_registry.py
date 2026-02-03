"""Tests for strategy registry."""

import pytest

from polymoney.core.strategy import (
    BaseStrategy,
    register_strategy,
    get_strategy_registry,
    list_strategies,
    get_strategy_class,
    create_strategy,
    unregister_strategy,
)
from polymoney.core.models import OrderSignal, PriceData


class TestStrategyRegistry:
    """Tests for strategy registration system."""

    def setup_method(self):
        """Clean up registry before each test."""
        # Unregister test strategies if they exist
        for name in ["test-strategy", "another-test"]:
            unregister_strategy(name)

    def teardown_method(self):
        """Clean up registry after each test."""
        for name in ["test-strategy", "another-test"]:
            unregister_strategy(name)

    def test_register_strategy(self):
        """Test registering a strategy."""

        @register_strategy("test-strategy")
        class TestStrategy(BaseStrategy):
            @property
            def strategy_type(self):
                return "test"

            def on_price_update(self, price_data):
                return []

            def on_market_start(self, market_id, market_info):
                pass

            def on_market_end(self, market_id, winner):
                return 0, 0, "unknown"

            def get_status(self):
                return {}

        assert "test-strategy" in list_strategies()
        assert get_strategy_class("test-strategy") == TestStrategy

    def test_list_strategies(self):
        """Test listing registered strategies."""
        names = list_strategies()
        assert isinstance(names, list)

    def test_get_strategy_class(self):
        """Test getting strategy class by name."""

        @register_strategy("another-test")
        class AnotherStrategy(BaseStrategy):
            @property
            def strategy_type(self):
                return "test"

            def on_price_update(self, price_data):
                return []

            def on_market_start(self, market_id, market_info):
                pass

            def on_market_end(self, market_id, winner):
                return 0, 0, "unknown"

            def get_status(self):
                return {}

        cls = get_strategy_class("another-test")
        assert cls == AnotherStrategy

        # Non-existent
        assert get_strategy_class("non-existent") is None

    def test_create_strategy_instance(self):
        """Test creating strategy instance."""

        @register_strategy("test-strategy")
        class TestStrategy(BaseStrategy):
            @property
            def strategy_type(self):
                return "test"

            def on_price_update(self, price_data):
                return []

            def on_market_start(self, market_id, market_info):
                pass

            def on_market_end(self, market_id, winner):
                return 0, 0, "unknown"

            def get_status(self):
                return {}

        instance = create_strategy(
            name="test-strategy",
            strategy_id="instance-1",
            position_size=500,
            params={"key": "value"},
        )

        assert instance is not None
        assert instance.strategy_id == "instance-1"
        assert instance.position_size == 500
        assert instance.params == {"key": "value"}

    def test_create_unknown_strategy(self):
        """Test creating instance of unknown strategy."""
        instance = create_strategy(
            name="non-existent",
            strategy_id="test",
        )
        assert instance is None

    def test_unregister_strategy(self):
        """Test unregistering a strategy."""

        @register_strategy("test-strategy")
        class TestStrategy(BaseStrategy):
            @property
            def strategy_type(self):
                return "test"

            def on_price_update(self, price_data):
                return []

            def on_market_start(self, market_id, market_info):
                pass

            def on_market_end(self, market_id, winner):
                return 0, 0, "unknown"

            def get_status(self):
                return {}

        assert "test-strategy" in list_strategies()

        result = unregister_strategy("test-strategy")
        assert result is True
        assert "test-strategy" not in list_strategies()

        # Unregistering non-existent returns False
        result = unregister_strategy("non-existent")
        assert result is False
