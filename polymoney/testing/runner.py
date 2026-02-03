"""
Test Runner - Execute strategy tests with real market data.

Supports two modes:
- Replay mode: Use historical data from database
- Live mode: Connect to real WebSocket for paper trading
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    MarketEnd,
    MarketSettled,
    MarketStart,
    OrderSignal,
    PriceData,
    TokenType,
)
from polymoney.core.strategy import BaseStrategy
from polymoney.data.storage import DataStorage
from polymoney.strategy.order_manager import PaperOrderManager

from .replayer import MarketDataReplayer, MarketReplayData

logger = get_logger("testing.runner")


@dataclass
class TestResult:
    """Result of a test run."""

    market_id: str
    strategy_id: str
    start_time: datetime
    end_time: datetime
    settlement_winner: Optional[str]

    # Position
    up_shares: float = 0.0
    up_cost: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0

    # Metrics
    total_cost: float = 0.0
    settlement_value: float = 0.0
    pnl: float = 0.0
    roi: float = 0.0
    effective_cost_rate: float = 0.0
    balance_ratio: float = 0.0
    hedged_position: float = 0.0

    # Trading stats
    orders_submitted: int = 0
    orders_filled: int = 0
    fill_rate: float = 0.0

    # Price history for analysis
    price_history: List[Dict[str, Any]] = field(default_factory=list)
    trade_log: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "market_id": self.market_id,
            "strategy_id": self.strategy_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "settlement_winner": self.settlement_winner,
            "position": {
                "up_shares": self.up_shares,
                "up_cost": self.up_cost,
                "down_shares": self.down_shares,
                "down_cost": self.down_cost,
            },
            "metrics": {
                "total_cost": self.total_cost,
                "settlement_value": self.settlement_value,
                "pnl": self.pnl,
                "roi": self.roi,
                "effective_cost_rate": self.effective_cost_rate,
                "balance_ratio": self.balance_ratio,
                "hedged_position": self.hedged_position,
            },
            "trading_stats": {
                "orders_submitted": self.orders_submitted,
                "orders_filled": self.orders_filled,
                "fill_rate": self.fill_rate,
            },
        }


@dataclass
class TestConfig:
    """Configuration for test run."""

    # Strategy settings
    strategy_class: Type[BaseStrategy]
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    position_size: float = 100.0

    # Replay settings
    replay_speed: float = 0.0  # 0 = max speed
    market_id: Optional[str] = None
    market_title: str = ""
    settlement_winner: Optional[str] = None  # Override settlement


class TestRunner:
    """
    Runs strategy tests with real market data.

    Features:
    - Replay mode: Historical data from database
    - Live mode: Real-time WebSocket (paper trading)
    - Integrates with OrderManager for order execution
    - Collects detailed metrics and trade logs
    """

    def __init__(
        self,
        storage: DataStorage,
        config: TestConfig,
    ):
        """
        Initialize test runner.

        Args:
            storage: DataStorage instance
            config: Test configuration
        """
        self.storage = storage
        self.config = config

        # Initialize strategy
        self.strategy = config.strategy_class(
            strategy_id=f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            name=f"Test {config.strategy_class.__name__}",
            position_size=config.position_size,
            params=config.strategy_params,
        )

        # Initialize order manager in paper mode
        self.order_manager = PaperOrderManager(
            slippage_pct=0.0,  # No slippage for testing
            fill_probability=1.0,  # 100% fill rate for testing
        )

        # Initialize replayer
        self.replayer = MarketDataReplayer(
            storage=storage,
            replay_speed=config.replay_speed,
        )

        # State
        self._running = False
        self._current_market_id: Optional[str] = None
        self._settlement_winner: Optional[str] = None
        self._start_time: Optional[datetime] = None

        # Tracking
        self._price_history: List[Dict[str, Any]] = []
        self._trade_log: List[Dict[str, Any]] = []
        self._orders_submitted = 0
        self._orders_filled = 0
        
        # Pending orders for fill checking
        self._pending_orders: List[Dict[str, Any]] = []

    async def run_replay(
        self,
        market_id: str,
        title: str = "",
        settlement_winner: Optional[str] = None,
    ) -> TestResult:
        """
        Run test with historical data replay.

        Args:
            market_id: Market ID to replay
            title: Market title
            settlement_winner: Settlement result (or None to detect)

        Returns:
            TestResult with all metrics
        """
        logger.info(f"Starting replay test for {market_id}")

        # Reset state
        self._reset()
        self._current_market_id = market_id
        self._start_time = datetime.now()

        # Load market data
        market_data = await self.replayer.load_market(
            market_id=market_id,
            title=title,
            settlement_winner=settlement_winner,
            interval="1m",
        )

        if not market_data.candlesticks:
            logger.error(f"No data available for market {market_id}")
            return self._create_result(market_data)

        # Set up event handling
        self.replayer.add_callback(self._on_replay_event)

        # Run replay
        self._running = True
        await self.replayer.replay(market_data)
        self._running = False

        # Create result
        result = self._create_result(market_data)

        logger.info(
            f"Replay complete: PnL ${result.pnl:.2f}, "
            f"ROI {result.roi:.1%}, "
            f"Effective cost {result.effective_cost_rate:.1%}"
        )

        return result

    def _reset(self) -> None:
        """Reset state for new test."""
        self.strategy.reset()
        self._price_history.clear()
        self._trade_log.clear()
        self._orders_submitted = 0
        self._orders_filled = 0
        self._settlement_winner = None
        self._pending_orders.clear()

    def _on_replay_event(self, event_type: str, market_id: str, data: Any) -> None:
        """Handle events from replayer."""
        if event_type == "market_start":
            self._on_market_start(market_id, data)
        elif event_type == "price_update":
            self._on_price_update(market_id, data)
        elif event_type == "market_end":
            self._on_market_end(market_id, data)
        elif event_type == "market_settled":
            self._on_market_settled(market_id, data)

    def _on_market_start(self, market_id: str, event: MarketStart) -> None:
        """Handle market start."""
        logger.debug(f"Market started: {market_id}")
        self.strategy.on_market_start(market_id, event.metadata)

    def _on_price_update(self, market_id: str, price_data: PriceData) -> None:
        """Handle price update."""
        # Record price
        self._price_history.append({
            "timestamp": price_data.timestamp.isoformat() if price_data.timestamp else datetime.now().isoformat(),
            "up_price": price_data.up_price,
            "down_price": price_data.down_price,
        })

        # Check pending orders for fills at new price
        self._check_pending_fills(price_data)

        # Get signals from strategy
        signals = self.strategy.on_price_update(price_data)

        # Process signals
        for signal in signals:
            self._process_signal(market_id, signal, price_data)

    def _check_pending_fills(self, price_data: PriceData) -> None:
        """Check pending orders and fill any that can be filled at new price."""
        filled_indices = []
        
        for i, order in enumerate(self._pending_orders):
            is_yes_token = order["token_type"] in ("yes", "YES")
            market_price = price_data.up_price if is_yes_token else price_data.down_price
            
            # Limit BUY order fills if market price <= limit price
            if market_price <= order["price"]:
                fill_price = market_price
                self._execute_fill(order, fill_price)
                filled_indices.append(i)
        
        # Remove filled orders (in reverse to maintain indices)
        for i in reversed(filled_indices):
            self._pending_orders.pop(i)

    def _process_signal(
        self,
        market_id: str,
        signal: OrderSignal,
        price_data: PriceData,
    ) -> None:
        """Process an order signal."""
        self._orders_submitted += 1

        # Handle both enum and string values (due to Pydantic use_enum_values)
        side_str = signal.side.value if hasattr(signal.side, 'value') else signal.side
        token_type_str = signal.token_type.value if hasattr(signal.token_type, 'value') else signal.token_type

        # Create pending order
        order = {
            "market_id": market_id,
            "side": side_str,
            "token_type": token_type_str,
            "price": signal.target_price,
            "size": signal.size,
        }

        # Log order submission
        self._trade_log.append({
            "type": "order_submitted",
            "timestamp": datetime.now().isoformat(),
            **order,
        })

        # Paper trading: check if price is already favorable for immediate fill
        is_yes_token = token_type_str in ("yes", "YES", TokenType.YES)
        market_price = price_data.up_price if is_yes_token else price_data.down_price

        # Limit order fills if market price <= limit price
        if market_price <= signal.target_price:
            fill_price = market_price  # Fill at market price
            self._execute_fill(order, fill_price)
        else:
            # Add to pending orders for later fill check
            self._pending_orders.append(order)

    def _execute_fill(self, order: Dict[str, Any], fill_price: float) -> None:
        """Execute a fill for an order."""
        self._orders_filled += 1

        # Update strategy position
        is_yes_token = order["token_type"] in ("yes", "YES", TokenType.YES)
        if is_yes_token:
            self.strategy.up_position.add(order["size"], fill_price)
        else:
            self.strategy.down_position.add(order["size"], fill_price)

        # Log fill
        self._trade_log.append({
            "type": "order_filled",
            "timestamp": datetime.now().isoformat(),
            "market_id": order["market_id"],
            "side": order["side"],
            "token_type": order["token_type"],
            "price": fill_price,
            "size": order["size"],
            "cost": order["size"] * fill_price,
        })

    def _on_market_end(self, market_id: str, event: MarketEnd) -> None:
        """Handle market end."""
        logger.debug(f"Market ended: {market_id}")

    def _on_market_settled(self, market_id: str, event: MarketSettled) -> None:
        """Handle market settlement."""
        # Handle both enum and string values
        self._settlement_winner = event.winner.value if hasattr(event.winner, 'value') else event.winner
        logger.debug(f"Market settled: {market_id}, winner: {self._settlement_winner}")

    def _create_result(self, market_data: MarketReplayData) -> TestResult:
        """Create test result from current state."""
        up_shares = self.strategy.up_position.shares
        down_shares = self.strategy.down_position.shares
        up_cost = self.strategy.up_position.cost
        down_cost = self.strategy.down_position.cost
        total_cost = up_cost + down_cost

        # Settlement calculation
        winner = self._settlement_winner or market_data.settlement_winner
        if winner in ("up", "yes"):
            settlement_value = up_shares
        elif winner in ("down", "no"):
            settlement_value = down_shares
        else:
            # Unknown: average
            settlement_value = (up_shares + down_shares) / 2

        pnl = settlement_value - total_cost
        roi = pnl / total_cost if total_cost > 0 else 0.0

        # Key metrics
        min_shares = min(up_shares, down_shares)
        max_shares = max(up_shares, down_shares)
        effective_cost_rate = total_cost / min_shares if min_shares > 0 else float("inf")
        balance_ratio = min_shares / max_shares if max_shares > 0 else 1.0
        hedged_position = min_shares

        fill_rate = self._orders_filled / self._orders_submitted if self._orders_submitted > 0 else 0.0

        return TestResult(
            market_id=market_data.market_id,
            strategy_id=self.strategy.strategy_id,
            start_time=self._start_time or datetime.now(),
            end_time=datetime.now(),
            settlement_winner=winner,
            up_shares=up_shares,
            up_cost=up_cost,
            down_shares=down_shares,
            down_cost=down_cost,
            total_cost=total_cost,
            settlement_value=settlement_value,
            pnl=pnl,
            roi=roi,
            effective_cost_rate=effective_cost_rate if effective_cost_rate != float("inf") else 999.99,
            balance_ratio=balance_ratio,
            hedged_position=hedged_position,
            orders_submitted=self._orders_submitted,
            orders_filled=self._orders_filled,
            fill_rate=fill_rate,
            price_history=self._price_history.copy(),
            trade_log=self._trade_log.copy(),
        )

    @property
    def is_running(self) -> bool:
        """Check if test is running."""
        return self._running
