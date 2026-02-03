"""
Strategy base class and registry for Polymoney.

Provides the abstract base class for all trading strategies and a
decorator-based registration system for strategy discovery.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel, Field

from .models import (
    OrderSignal,
    PerformanceMetrics,
    Position,
    PositionPair,
    PriceData,
    StrategyState,
    TradeResult,
)


class StrategyConfig(BaseModel):
    """Base configuration for strategies."""

    strategy_id: str = Field(description="Unique strategy identifier")
    name: str = Field(description="Human-readable strategy name")
    position_size: float = Field(default=100.0, gt=0, description="Max capital allocation")
    params: Dict[str, Any] = Field(default_factory=dict, description="Strategy-specific parameters")


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.

    All strategies must inherit from this class and implement the required methods.
    """

    def __init__(
        self,
        strategy_id: str,
        name: str,
        position_size: float = 100.0,
        params: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize the strategy.

        Args:
            strategy_id: Unique identifier for this strategy instance
            name: Human-readable name
            position_size: Maximum capital allocation
            params: Strategy-specific parameters
        """
        self.strategy_id = strategy_id
        self.name = name
        self.position_size = position_size
        self.params = params or {}
        self._state = StrategyState.INITIALIZED
        self._positions: Dict[str, PositionPair] = {}  # market_id -> PositionPair
        self._metrics = PerformanceMetrics(strategy_id=strategy_id)

    @property
    @abstractmethod
    def strategy_type(self) -> str:
        """Return the strategy type identifier (e.g., 'market_maker', 'directional')."""
        pass

    @property
    def description(self) -> str:
        """Return a human-readable description of the strategy."""
        return f"{self.name} ({self.strategy_type})"

    @property
    def state(self) -> StrategyState:
        """Get current strategy state."""
        return self._state

    @state.setter
    def state(self, value: StrategyState) -> None:
        """Set strategy state."""
        self._state = value

    @property
    def is_running(self) -> bool:
        """Check if strategy is currently running."""
        return self._state == StrategyState.RUNNING

    @property
    def metrics(self) -> PerformanceMetrics:
        """Get current performance metrics."""
        return self._metrics

    def get_position(self, market_id: str) -> PositionPair:
        """Get position for a specific market."""
        if market_id not in self._positions:
            self._positions[market_id] = PositionPair()
        return self._positions[market_id]

    @abstractmethod
    def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
        """
        Handle a price update event.

        Called whenever new price data is available for a subscribed market.

        Args:
            price_data: Current price information

        Returns:
            List of order signals to execute
        """
        pass

    @abstractmethod
    def on_market_start(self, market_id: str, market_info: Dict[str, Any]) -> None:
        """
        Handle market start event.

        Called when a new market becomes active.

        Args:
            market_id: Market condition ID
            market_info: Market metadata
        """
        pass

    @abstractmethod
    def on_market_end(self, market_id: str, winner: Optional[str]) -> Tuple[float, float, str]:
        """
        Handle market end/settlement event.

        Called when a market closes or settles.

        Args:
            market_id: Market condition ID
            winner: Winning outcome ('up'/'down' or 'yes'/'no'), None if unknown

        Returns:
            Tuple of (total_cost, pnl, outcome)
        """
        pass

    def on_order_filled(self, trade: TradeResult) -> None:
        """
        Handle order fill notification.

        Called when an order is filled. Default implementation updates positions.

        Args:
            trade: Trade execution details
        """
        position = self.get_position(trade.market_id)
        token_type = trade.token_type.lower()

        if token_type in ("yes", "up"):
            position.up.add(trade.size, trade.price)
        else:
            position.down.add(trade.size, trade.price)

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """
        Get current strategy status.

        Returns:
            Dictionary with strategy state information
        """
        pass

    def reset(self) -> None:
        """Reset strategy state for a new trading session."""
        self._positions.clear()
        self._metrics = PerformanceMetrics(strategy_id=self.strategy_id)


# Strategy Registry
_strategy_registry: Dict[str, Type[BaseStrategy]] = {}


def register_strategy(name: str) -> Callable[[Type[BaseStrategy]], Type[BaseStrategy]]:
    """
    Decorator to register a strategy class.

    Usage:
        @register_strategy("my-strategy")
        class MyStrategy(BaseStrategy):
            ...

    Args:
        name: Unique name for the strategy (kebab-case recommended)

    Returns:
        Decorator function
    """

    def decorator(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
        if name in _strategy_registry:
            raise ValueError(f"Strategy '{name}' is already registered")
        if not issubclass(cls, BaseStrategy):
            raise TypeError(f"Class {cls.__name__} must inherit from BaseStrategy")
        _strategy_registry[name] = cls
        return cls

    return decorator


def get_strategy_registry() -> Dict[str, Type[BaseStrategy]]:
    """Get the strategy registry (read-only copy)."""
    return dict(_strategy_registry)


def list_strategies() -> List[str]:
    """List all registered strategy names."""
    return list(_strategy_registry.keys())


def get_strategy_class(name: str) -> Optional[Type[BaseStrategy]]:
    """
    Get a strategy class by name.

    Args:
        name: Registered strategy name

    Returns:
        Strategy class or None if not found
    """
    return _strategy_registry.get(name)


def create_strategy(
    name: str,
    strategy_id: str,
    position_size: float = 100.0,
    params: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Optional[BaseStrategy]:
    """
    Create a strategy instance by name.

    Args:
        name: Registered strategy name
        strategy_id: Unique instance ID
        position_size: Capital allocation
        params: Strategy-specific parameters
        **kwargs: Additional arguments passed to constructor

    Returns:
        Strategy instance or None if name not found
    """
    cls = get_strategy_class(name)
    if cls is None:
        return None

    return cls(
        strategy_id=strategy_id,
        name=name,
        position_size=position_size,
        params=params,
        **kwargs,
    )


def unregister_strategy(name: str) -> bool:
    """
    Unregister a strategy (mainly for testing).

    Args:
        name: Strategy name to unregister

    Returns:
        True if unregistered, False if not found
    """
    if name in _strategy_registry:
        del _strategy_registry[name]
        return True
    return False
