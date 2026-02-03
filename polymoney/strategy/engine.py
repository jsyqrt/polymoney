"""
Strategy Engine - Coordinates strategies, market data, and order execution.

Central component that manages the lifecycle and execution of trading strategies.
"""

import asyncio
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    Order,
    OrderType,
    PriceData,
    StrategyState,
    TradeResult,
)
from polymoney.core.strategy import BaseStrategy, create_strategy, list_strategies
from polymoney.data import EVENT_MARKET_END, EVENT_MARKET_SETTLED, EVENT_PRICE_UPDATE, MarketDataService

from .order_manager import LiveOrderManager, OrderManager, PaperOrderManager

logger = get_logger("strategy.engine")


class StrategyInstance:
    """Container for a strategy instance and its state."""

    def __init__(
        self,
        strategy: BaseStrategy,
        subscribed_markets: Optional[Set[str]] = None,
    ):
        self.strategy = strategy
        self.subscribed_markets = subscribed_markets or set()
        self.last_update: Optional[datetime] = None
        self.order_count = 0
        self.error_count = 0


class StrategyEngine:
    """
    Manages multiple trading strategies.

    Features:
    - Strategy lifecycle management (start, stop, pause)
    - Market data subscription
    - Order execution coordination
    - Risk controls
    - Trading mode switching (paper/live)
    """

    def __init__(
        self,
        market_data_service: MarketDataService,
        trading_mode: str = "paper",
        max_position_size: float = 1000.0,
        order_rate_limit: int = 10,
    ):
        """
        Initialize strategy engine.

        Args:
            market_data_service: Market data service for real-time data
            trading_mode: 'paper' or 'live'
            max_position_size: Default maximum position size per strategy
            order_rate_limit: Maximum orders per second
        """
        self._market_data = market_data_service
        self._trading_mode = trading_mode
        self._max_position_size = max_position_size
        self._order_rate_limit = order_rate_limit

        # Strategies
        self._strategies: Dict[str, StrategyInstance] = {}

        # Order managers
        self._paper_order_manager = PaperOrderManager()
        self._live_order_manager = LiveOrderManager()

        # Rate limiting
        self._order_timestamps: List[float] = []

        # Running state
        self._running = False

        # Setup market data callback
        self._market_data.add_listener(self._on_market_event)

    @property
    def trading_mode(self) -> str:
        """Get current trading mode."""
        return self._trading_mode

    @property
    def order_manager(self) -> OrderManager:
        """Get the active order manager based on trading mode."""
        if self._trading_mode == "live":
            return self._live_order_manager
        return self._paper_order_manager

    @property
    def is_running(self) -> bool:
        """Check if engine is running."""
        return self._running

    def set_trading_mode(self, mode: str) -> None:
        """
        Set trading mode.

        Args:
            mode: 'paper' or 'live'
        """
        if mode not in ("paper", "live"):
            raise ValueError(f"Invalid trading mode: {mode}")

        if mode == "live" and self._live_order_manager._client is None:
            raise RuntimeError("Live trading requires configured CLOB client")

        old_mode = self._trading_mode
        self._trading_mode = mode
        logger.info(f"Trading mode changed: {old_mode} -> {mode}")

    def set_clob_client(self, client) -> None:
        """Set the CLOB client for live trading."""
        self._live_order_manager.set_client(client)

    async def start(self) -> None:
        """Start the strategy engine."""
        if self._running:
            return

        self._running = True

        # Set fill callback
        self.order_manager.add_fill_callback(self._on_order_fill)

        logger.info(f"Strategy engine started (mode: {self._trading_mode})")

    async def stop(self) -> None:
        """Stop the strategy engine."""
        if not self._running:
            return

        # Stop all strategies
        for strategy_id in list(self._strategies.keys()):
            await self.stop_strategy(strategy_id)

        self._running = False
        logger.info("Strategy engine stopped")

    def add_strategy(
        self,
        strategy_type: str,
        strategy_id: str,
        position_size: Optional[float] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[BaseStrategy]:
        """
        Add a new strategy instance.

        Args:
            strategy_type: Registered strategy name
            strategy_id: Unique instance ID
            position_size: Maximum position size (default: engine default)
            params: Strategy-specific parameters

        Returns:
            Created strategy or None if type not found
        """
        if strategy_id in self._strategies:
            logger.warning(f"Strategy {strategy_id} already exists")
            return self._strategies[strategy_id].strategy

        strategy = create_strategy(
            name=strategy_type,
            strategy_id=strategy_id,
            position_size=position_size or self._max_position_size,
            params=params,
        )

        if strategy is None:
            logger.error(f"Unknown strategy type: {strategy_type}")
            return None

        self._strategies[strategy_id] = StrategyInstance(strategy)
        logger.info(f"Strategy added: {strategy_id} ({strategy_type})")

        return strategy

    def remove_strategy(self, strategy_id: str) -> bool:
        """
        Remove a strategy.

        Args:
            strategy_id: Strategy to remove

        Returns:
            True if removed
        """
        if strategy_id not in self._strategies:
            return False

        instance = self._strategies[strategy_id]

        # Ensure strategy is stopped
        if instance.strategy.state == StrategyState.RUNNING:
            instance.strategy.state = StrategyState.STOPPED

        del self._strategies[strategy_id]
        logger.info(f"Strategy removed: {strategy_id}")

        return True

    def get_strategy(self, strategy_id: str) -> Optional[BaseStrategy]:
        """Get a strategy by ID."""
        instance = self._strategies.get(strategy_id)
        return instance.strategy if instance else None

    def list_strategy_ids(self) -> List[str]:
        """Get list of strategy IDs."""
        return list(self._strategies.keys())

    def list_available_strategies(self) -> List[str]:
        """Get list of registered strategy types."""
        return list_strategies()

    async def start_strategy(self, strategy_id: str, markets: Optional[List[str]] = None) -> bool:
        """
        Start a strategy.

        Args:
            strategy_id: Strategy to start
            markets: Markets to subscribe to

        Returns:
            True if started
        """
        instance = self._strategies.get(strategy_id)
        if not instance:
            logger.error(f"Strategy not found: {strategy_id}")
            return False

        if instance.strategy.state == StrategyState.RUNNING:
            logger.warning(f"Strategy {strategy_id} is already running")
            return False

        # Subscribe to markets
        if markets:
            for market_id in markets:
                instance.subscribed_markets.add(market_id)

        # Start strategy
        instance.strategy.state = StrategyState.RUNNING
        instance.strategy.reset()

        logger.info(f"Strategy started: {strategy_id} (markets: {instance.subscribed_markets})")
        return True

    async def stop_strategy(self, strategy_id: str) -> bool:
        """
        Stop a strategy.

        Args:
            strategy_id: Strategy to stop

        Returns:
            True if stopped
        """
        instance = self._strategies.get(strategy_id)
        if not instance:
            return False

        if instance.strategy.state != StrategyState.RUNNING:
            return True

        # Cancel pending orders
        await self.order_manager.cancel_all_orders(strategy_id)

        # Stop strategy
        instance.strategy.state = StrategyState.STOPPED

        logger.info(f"Strategy stopped: {strategy_id}")
        return True

    async def pause_strategy(self, strategy_id: str) -> bool:
        """
        Pause a strategy.

        Args:
            strategy_id: Strategy to pause

        Returns:
            True if paused
        """
        instance = self._strategies.get(strategy_id)
        if not instance:
            return False

        if instance.strategy.state != StrategyState.RUNNING:
            return False

        instance.strategy.state = StrategyState.PAUSED
        logger.info(f"Strategy paused: {strategy_id}")
        return True

    async def resume_strategy(self, strategy_id: str) -> bool:
        """
        Resume a paused strategy.

        Args:
            strategy_id: Strategy to resume

        Returns:
            True if resumed
        """
        instance = self._strategies.get(strategy_id)
        if not instance:
            return False

        if instance.strategy.state != StrategyState.PAUSED:
            return False

        instance.strategy.state = StrategyState.RUNNING
        logger.info(f"Strategy resumed: {strategy_id}")
        return True

    def _on_market_event(self, event_type: str, market_id: str, data: Any) -> None:
        """Handle events from market data service."""
        if event_type == EVENT_PRICE_UPDATE:
            asyncio.create_task(self._handle_price_update(market_id, data))
        elif event_type == EVENT_MARKET_END:
            asyncio.create_task(self._handle_market_end(market_id, data))
        elif event_type == EVENT_MARKET_SETTLED:
            asyncio.create_task(self._handle_market_settled(market_id, data))

    async def _handle_price_update(self, market_id: str, price_data: PriceData) -> None:
        """Handle price update for strategies."""
        # Update paper order manager prices
        self._paper_order_manager.set_market_price(
            market_id,
            price_data.up_price,
            price_data.down_price,
        )

        # Check paper fills
        await self._paper_order_manager.check_fills()

        # Process each subscribed strategy
        for strategy_id, instance in self._strategies.items():
            if instance.strategy.state != StrategyState.RUNNING:
                continue

            if market_id not in instance.subscribed_markets:
                continue

            try:
                # Get signals from strategy
                signals = instance.strategy.on_price_update(price_data)
                instance.last_update = datetime.now()

                # Execute signals
                for signal in signals:
                    await self._execute_signal(strategy_id, market_id, signal)

            except Exception as e:
                instance.error_count += 1
                logger.error(f"Error in strategy {strategy_id}: {e}")

    async def _handle_market_end(self, market_id: str, event) -> None:
        """Handle market end event."""
        for strategy_id, instance in self._strategies.items():
            if market_id in instance.subscribed_markets:
                try:
                    instance.strategy.on_market_end(market_id, None)
                except Exception as e:
                    logger.error(f"Error handling market end for {strategy_id}: {e}")

    async def _handle_market_settled(self, market_id: str, event) -> None:
        """Handle market settlement."""
        for strategy_id, instance in self._strategies.items():
            if market_id in instance.subscribed_markets:
                try:
                    instance.strategy.on_market_end(market_id, event.winner)
                except Exception as e:
                    logger.error(f"Error handling settlement for {strategy_id}: {e}")

    async def _execute_signal(self, strategy_id: str, market_id: str, signal) -> Optional[Order]:
        """Execute an order signal with risk controls."""
        instance = self._strategies.get(strategy_id)
        if not instance:
            return None

        # Rate limiting
        if not self._check_rate_limit():
            logger.warning(f"Order rate limit exceeded for {strategy_id}")
            return None

        # Position size check
        position = instance.strategy.get_position(market_id)
        current_cost = position.total_cost
        order_cost = signal.size * signal.target_price

        if current_cost + order_cost > instance.strategy.position_size:
            logger.warning(f"Position size limit exceeded for {strategy_id}")
            # Reduce order size to fit limit
            available = instance.strategy.position_size - current_cost
            if available <= 0:
                return None
            signal.size = available / signal.target_price

        # Place order
        try:
            order = await self.order_manager.place_order(
                signal=signal,
                strategy_id=strategy_id,
                market_id=market_id,
            )
            instance.order_count += 1
            return order

        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return None

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        now = datetime.now().timestamp()

        # Remove old timestamps
        cutoff = now - 1.0  # 1 second window
        self._order_timestamps = [t for t in self._order_timestamps if t > cutoff]

        # Check limit
        if len(self._order_timestamps) >= self._order_rate_limit:
            return False

        self._order_timestamps.append(now)
        return True

    def _on_order_fill(self, trade: TradeResult) -> None:
        """Handle order fill notification."""
        instance = self._strategies.get(trade.strategy_id)
        if instance:
            instance.strategy.on_order_filled(trade)

    def get_status(self) -> Dict[str, Any]:
        """Get engine status."""
        return {
            "is_running": self._running,
            "trading_mode": self._trading_mode,
            "strategy_count": len(self._strategies),
            "strategies": {
                sid: {
                    "name": inst.strategy.name,
                    "type": inst.strategy.strategy_type,
                    "state": inst.strategy.state.value,
                    "subscribed_markets": list(inst.subscribed_markets),
                    "last_update": inst.last_update.isoformat() if inst.last_update else None,
                    "order_count": inst.order_count,
                    "error_count": inst.error_count,
                }
                for sid, inst in self._strategies.items()
            },
            "pending_orders": len(self.order_manager.pending_orders),
        }
