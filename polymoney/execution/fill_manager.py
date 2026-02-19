"""
FillManager - Real-time fill tracking and position reconciliation.

Bridges the gap between the OrderExecutor and the StrategyEngine by:
- Collecting fill events from the executor
- Applying fills to position tracking (MarketContext)
- Reconciling local positions with exchange state
- Logging trades for audit and analysis
- Tracking pending redemptions (capital locked until redeemed)
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from polymoney.core.logging import get_logger
from polymoney.execution.executor import FillEvent

logger = get_logger("execution.fill_manager")


# Type for fill callbacks
FillCallback = Callable[[FillEvent], None]


@dataclass
class PositionState:
    """Tracked position for a single market."""
    market_id: str
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    orders_filled: int = 0
    orders_cancelled: int = 0

    @property
    def total_cost(self) -> float:
        return self.up_cost + self.down_cost

    @property
    def min_shares(self) -> float:
        return min(self.up_shares, self.down_shares)

    @property
    def ecr(self) -> float:
        if self.min_shares <= 0:
            return float("inf")
        return self.total_cost / self.min_shares

    @property
    def balance_ratio(self) -> float:
        max_s = max(self.up_shares, self.down_shares)
        if max_s <= 0:
            return 1.0
        return self.min_shares / max_s


class FillManager:
    """
    Manages fill events and position tracking.
    
    Receives FillEvents from OrderExecutor and:
    1. Updates internal position tracking
    2. Notifies registered callbacks (for strategy/engine)
    3. Logs trades to JSONL for audit
    4. Provides position reconciliation interface
    5. Tracks pending redemptions (capital locked until redeemed)
    """

    def __init__(
        self,
        trade_log_path: Optional[Path] = None,
    ):
        """
        Args:
            trade_log_path: Path to JSONL file for trade logging.
                           None disables logging.
        """
        self._positions: Dict[str, PositionState] = {}
        self._callbacks: List[FillCallback] = []
        self._trade_log_path = trade_log_path
        self._total_fills = 0
        self._total_cancellations = 0

        # Pending redemptions: market_id -> total_cost locked until redeemed.
        # Capital in this dict is NOT available for new positions.
        self._pending_redemptions: Dict[str, float] = {}
        self._total_redeemed_value: float = 0.0

    def register_market(self, market_id: str) -> None:
        """Initialize position tracking for a market."""
        if market_id not in self._positions:
            self._positions[market_id] = PositionState(market_id=market_id)

    def unregister_market(
        self,
        market_id: str,
        winner: Optional[str] = None,
    ) -> PositionState:
        """Remove and return final position state for a market.

        When a winner is known (market settled), the position's cost is moved
        to pending redemptions.  This locks the capital until redemption
        succeeds and ``release_redemption`` is called.

        Args:
            market_id: Market slug to unregister.
            winner: "up" or "down" if market settled, None if unknown.

        Returns:
            The final PositionState for the market.
        """
        pos = self._positions.pop(market_id, PositionState(market_id=market_id))

        if winner and pos.total_cost > 0:
            self._pending_redemptions[market_id] = pos.total_cost
            logger.info(
                f"[{market_id}] Capital locked pending redemption: "
                f"${pos.total_cost:.2f}"
            )

        return pos

    def release_redemption(self, market_id: str) -> None:
        """Release capital locked by a pending redemption.

        Called after successful redemption (real or simulated) to make
        the capital available for new positions again.
        """
        cost = self._pending_redemptions.pop(market_id, 0.0)
        if cost > 0:
            self._total_redeemed_value += cost
            logger.info(
                f"[{market_id}] Redemption released: ${cost:.2f} now available"
            )

    @property
    def has_pending_redemptions(self) -> bool:
        """Whether there are any markets awaiting redemption."""
        return len(self._pending_redemptions) > 0

    @property
    def pending_redemption_count(self) -> int:
        """Number of markets awaiting redemption."""
        return len(self._pending_redemptions)

    def add_callback(self, callback: FillCallback) -> None:
        """Add a fill notification callback."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: FillCallback) -> None:
        """Remove a fill callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def process_fills(self, events: List[FillEvent]) -> None:
        """
        Process a batch of fill events from the executor.
        
        Updates positions, notifies callbacks, and logs trades.
        """
        for event in events:
            if event.is_cancelled:
                self._handle_cancellation(event)
            else:
                self._handle_fill(event)

    def _handle_fill(self, event: FillEvent) -> None:
        """Process a single fill event."""
        pos = self._positions.get(event.market_id)
        if not pos:
            logger.warning(
                f"Fill for unregistered market {event.market_id}: "
                f"{event.side} {event.fill_size}@{event.fill_price}"
            )
            return

        # Update position
        cost = event.fill_size * event.fill_price
        if event.side == "up":
            pos.up_shares += event.fill_size
            pos.up_cost += cost
        else:
            pos.down_shares += event.fill_size
            pos.down_cost += cost
        pos.orders_filled += 1
        self._total_fills += 1

        logger.info(
            f"[{event.market_id}] Fill: {event.side.upper()} "
            f"{event.fill_size:.1f}@{event.fill_price:.4f} "
            f"(ECR={pos.ecr:.4f}, bal={pos.balance_ratio:.0%})"
        )

        # Log trade
        self._log_trade(event)

        # Notify callbacks
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Fill callback error: {e}")

    def _handle_cancellation(self, event: FillEvent) -> None:
        """Process an order cancellation event."""
        pos = self._positions.get(event.market_id)
        if pos:
            pos.orders_cancelled += 1
        self._total_cancellations += 1

        logger.debug(
            f"[{event.market_id}] Cancel: {event.order_id} "
            f"({event.cancel_reason})"
        )

        # Notify callbacks (they can handle cancellations too)
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Fill callback error: {e}")

    def _log_trade(self, event: FillEvent) -> None:
        """Append trade to JSONL log."""
        if not self._trade_log_path:
            return

        record = {
            "timestamp": time.time(),
            "order_id": event.order_id,
            "market_id": event.market_id,
            "side": event.side,
            "price": event.fill_price,
            "size": event.fill_size,
            "is_taker": event.is_taker,
            "is_partial": event.is_partial,
        }

        try:
            with open(self._trade_log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.warning(f"Failed to log trade: {e}")

    def get_position(self, market_id: str) -> Optional[PositionState]:
        """Get current position state for a market."""
        return self._positions.get(market_id)

    def get_all_positions(self) -> Dict[str, PositionState]:
        """Get all position states."""
        return dict(self._positions)

    def get_total_exposure(self) -> float:
        """Get total capital committed: active positions + pending redemptions.

        Pending redemptions lock capital until the redemption completes.
        This prevents the system from entering new markets with capital
        that hasn't actually been recovered yet.
        """
        active = sum(p.total_cost for p in self._positions.values())
        pending = sum(self._pending_redemptions.values())
        return active + pending

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregate fill statistics."""
        return {
            "total_fills": self._total_fills,
            "total_cancellations": self._total_cancellations,
            "active_markets": len(self._positions),
            "total_exposure": self.get_total_exposure(),
            "pending_redemptions": len(self._pending_redemptions),
            "total_redeemed": self._total_redeemed_value,
        }

    def reconcile(
        self, market_id: str, exchange_up_shares: float, exchange_down_shares: float
    ) -> Dict[str, float]:
        """
        Compare local position with exchange state.
        
        Returns discrepancies (positive = local has more, negative = exchange has more).
        """
        pos = self._positions.get(market_id)
        if not pos:
            return {
                "up_diff": -exchange_up_shares,
                "down_diff": -exchange_down_shares,
            }

        return {
            "up_diff": pos.up_shares - exchange_up_shares,
            "down_diff": pos.down_shares - exchange_down_shares,
        }
