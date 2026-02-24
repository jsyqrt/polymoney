"""
OrderExecutor - Abstract base class for order execution.

Defines the interface that both SimulatedExecutor and LiveExecutor implement.
The StrategyEngine routes order signals through this interface, enabling
seamless switching between paper trading and real execution.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class OrderResultStatus(Enum):
    """Status of an order submission."""
    FILLED = "filled"              # Immediately fully filled
    PARTIALLY_FILLED = "partial"   # Partially filled, rest is pending
    PENDING = "pending"            # Placed on book, awaiting fill
    REJECTED = "rejected"          # Rejected by executor or exchange


@dataclass
class ExecutionOrder:
    """
    Order to submit to an executor.
    
    Contains all information needed for both simulated and live execution.
    The executor resolves market_id + side to the correct token_id for
    live trading.
    """
    market_id: str          # Market slug (e.g. "btc-updown-15m-...")
    side: str               # "up" or "down"
    price: float            # Limit price
    size: float             # Size in shares
    is_taker: bool = False  # Whether this is a taker order (crosses spread)
    trade_side: str = "buy" # "buy" or "sell" — direction at the exchange level
    
    # Metadata for tracking
    order_id: str = ""      # Unique order ID (assigned by executor if empty)
    strategy_id: str = ""   # Originating strategy
    timestamp: float = 0.0  # Creation time


@dataclass
class OrderResult:
    """
    Result of an order submission.
    
    For immediate fills (simulated or aggressive taker), fill_price and
    fill_size are populated. For pending orders, they are None.
    """
    status: OrderResultStatus
    order_id: str
    
    # Fill details (for FILLED or PARTIALLY_FILLED)
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None
    
    # Remaining pending (for PARTIALLY_FILLED)
    pending_size: Optional[float] = None
    
    # Error info (for REJECTED)
    error: Optional[str] = None
    
    # Exchange-specific data (for live trading)
    exchange_order_id: Optional[str] = None


@dataclass
class FillEvent:
    """
    A fill that occurred for a pending order.
    
    Returned by check_fills() to notify the engine of order completions.
    The engine applies these to update MarketContext positions.
    """
    order_id: str
    market_id: str
    side: str               # "up" or "down"
    fill_price: float
    fill_size: float
    is_taker: bool = False
    is_partial: bool = False  # True if order has remaining size
    remaining_size: float = 0.0
    timestamp: float = 0.0
    is_sell: bool = False     # True for sell (position exit) fills
    
    # Cancellation events use this
    is_cancelled: bool = False
    cancel_reason: str = ""


@dataclass
class MarketExecutionConfig:
    """
    Per-market configuration for the executor.
    
    Provided when a market is registered with the executor,
    containing market-specific parameters needed for execution.
    """
    market_id: str          # Market slug
    condition_id: str       # Polymarket condition ID
    up_token_id: str        # Polymarket token ID for UP/YES
    down_token_id: str      # Polymarket token ID for DOWN/NO
    
    # Fill simulation parameters (used by SimulatedExecutor)
    order_timeout: float = 30.0
    stale_order_threshold: float = 0.20
    min_order_shares: float = 5.0       # Polymarket per-market minimum


class OrderExecutor(ABC):
    """
    Abstract base class for order execution.
    
    Implementations:
    - SimulatedExecutor: Paper trading with depth-based fill simulation
    - LiveExecutor: Real order execution via Polymarket CLOB API
    
    The executor manages pending orders internally and reports fills
    back to the engine via check_fills().
    """

    @abstractmethod
    async def submit_order(self, order: ExecutionOrder) -> OrderResult:
        """
        Submit an order for execution.
        
        For SimulatedExecutor: attempts immediate depth-based fill,
        returns PENDING if no immediate fill.
        
        For LiveExecutor: sends order to CLOB API, returns exchange
        order ID.
        
        Args:
            order: Order to execute
            
        Returns:
            OrderResult with status and fill details
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if cancelled successfully
        """
        ...

    @abstractmethod
    async def check_fills(
        self, market_id: str, up_price: float, down_price: float
    ) -> List[FillEvent]:
        """
        Check pending orders for fills.
        
        For SimulatedExecutor: evaluates pending orders against current
        prices and orderbook depth.
        
        For LiveExecutor: polls exchange for order status updates.
        
        Args:
            market_id: Market to check
            up_price: Current UP token price
            down_price: Current DOWN token price
            
        Returns:
            List of fill events (fills + cancellations)
        """
        ...

    @abstractmethod
    async def cancel_all(self, market_id: Optional[str] = None) -> int:
        """
        Cancel all pending orders, optionally filtered by market.
        
        Args:
            market_id: If provided, cancel only orders for this market
            
        Returns:
            Number of orders cancelled
        """
        ...

    @abstractmethod
    def register_market(self, config: MarketExecutionConfig) -> None:
        """
        Register a new market with the executor.
        
        Provides market-specific configuration (token IDs, parameters)
        needed for order execution.
        
        Args:
            config: Market-specific execution configuration
        """
        ...

    @abstractmethod
    def unregister_market(self, market_id: str) -> None:
        """
        Unregister a market (e.g. after settlement).
        
        Cleans up any per-market state (orderbooks, pending orders).
        
        Args:
            market_id: Market to unregister
        """
        ...

    def update_orderbook(
        self, market_id: str, side: str, orderbook: Any
    ) -> None:
        """
        Update cached orderbook for a market token.
        
        Only meaningful for SimulatedExecutor (uses orderbook for fill
        simulation). LiveExecutor can ignore this.
        
        Args:
            market_id: Market slug
            side: "up" or "down"
            orderbook: OrderbookSnapshot instance
        """
        pass  # Default no-op; SimulatedExecutor overrides

    def update_orderbook_incremental(
        self, market_id: str, side: str, changes: Dict[str, Any]
    ) -> None:
        """
        Apply incremental orderbook changes.
        
        Args:
            market_id: Market slug
            side: "up" or "down"
            changes: Dict with "bids" and/or "asks" level changes
        """
        pass  # Default no-op; SimulatedExecutor overrides

    def update_spread(
        self, market_id: str, side: str, spread: float
    ) -> None:
        """
        Update the WS-derived bid-ask spread for a market token.
        
        Used by SimulatedExecutor as a fallback when no orderbook depth
        is available for the spread-based fill model.
        
        Args:
            market_id: Market slug
            side: "up" or "down"
            spread: Bid-ask spread from WebSocket
        """
        pass  # Default no-op; SimulatedExecutor overrides

    def get_pending_count(self, market_id: Optional[str] = None) -> int:
        """
        Get number of pending orders, optionally for a specific market.
        
        Args:
            market_id: Optional market filter
            
        Returns:
            Number of pending orders
        """
        return 0  # Subclasses override
