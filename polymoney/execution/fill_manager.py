"""
FillManager - Real-time fill tracking and position reconciliation.

Bridges the gap between the OrderExecutor and the StrategyEngine by:
- Collecting fill events from the executor
- Applying fills to position tracking (MarketContext)
- Reconciling local positions with exchange state
- Logging trades for audit and analysis
- Simulating redemption delays (capital locked until redeemed)
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


@dataclass
class PendingRedemption:
    """Capital locked in a settled market awaiting redemption.

    Models the real-world delay between settlement detection and USDC
    availability in the wallet.  During this window, the capital is not
    available for new positions but is no longer at market risk.
    """

    market_id: str
    settlement_value: float  # USDC to be recovered (winning shares)
    total_cost: float        # Original cost of the position
    settled_at: float        # time.time() when settlement was detected
    release_at: float        # time.time() when capital becomes available


class FillManager:
    """
    Manages fill events and position tracking.
    
    Receives FillEvents from OrderExecutor and:
    1. Updates internal position tracking
    2. Notifies registered callbacks (for strategy/engine)
    3. Logs trades to JSONL for audit
    4. Provides position reconciliation interface
    5. Tracks pending redemptions (capital locked during settlement)
    """

    def __init__(
        self,
        trade_log_path: Optional[Path] = None,
        redemption_delay: float = 0.0,
    ):
        """
        Args:
            trade_log_path: Path to JSONL file for trade logging.
                           None disables logging.
            redemption_delay: Seconds to hold settled capital before releasing.
                             0 = instant release (live mode uses real redemption).
                             >0 = simulated delay (paper mode, models real latency).
                             Recommended: 30-120s for realistic simulation.
        """
        self._positions: Dict[str, PositionState] = {}
        self._callbacks: List[FillCallback] = []
        self._trade_log_path = trade_log_path
        self._total_fills = 0
        self._total_cancellations = 0

        # Redemption simulation
        self._redemption_delay = redemption_delay
        self._pending_redemptions: List[PendingRedemption] = []
        self._total_redemptions: int = 0
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

        If a redemption_delay is configured and a winner is provided,
        the settlement value is queued as a pending redemption.  The
        locked capital counts toward exposure until the delay elapses,
        preventing the system from over-allocating to new markets.

        Args:
            market_id: Market slug to unregister.
            winner: "up" or "down" if market settled, None if unknown.

        Returns:
            The final PositionState for the market.
        """
        pos = self._positions.pop(market_id, PositionState(market_id=market_id))

        if self._redemption_delay > 0 and winner:
            settlement_value = pos.up_shares if winner == "up" else pos.down_shares
            if settlement_value > 0:
                now = time.time()
                pending = PendingRedemption(
                    market_id=market_id,
                    settlement_value=settlement_value,
                    total_cost=pos.total_cost,
                    settled_at=now,
                    release_at=now + self._redemption_delay,
                )
                self._pending_redemptions.append(pending)
                logger.info(
                    f"[{market_id}] Redemption queued: ${settlement_value:.2f} "
                    f"available in {self._redemption_delay:.0f}s"
                )

        return pos

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

        Pending redemptions lock capital until the redemption delay elapses.
        This prevents the system from entering new markets with capital that
        hasn't actually been recovered yet.
        """
        self._flush_matured_redemptions()
        active = sum(p.total_cost for p in self._positions.values())
        pending = sum(r.total_cost for r in self._pending_redemptions)
        return active + pending

    def get_pending_redemption_value(self) -> float:
        """Get total value locked in pending redemptions."""
        self._flush_matured_redemptions()
        return sum(r.settlement_value for r in self._pending_redemptions)

    def _flush_matured_redemptions(self) -> None:
        """Release redemptions whose delay has elapsed."""
        if not self._pending_redemptions:
            return

        now = time.time()
        matured = [r for r in self._pending_redemptions if now >= r.release_at]

        for r in matured:
            self._total_redemptions += 1
            self._total_redeemed_value += r.settlement_value
            logger.info(
                f"[{r.market_id}] Redemption complete: "
                f"${r.settlement_value:.2f} USDC released "
                f"(waited {now - r.settled_at:.0f}s)"
            )

        if matured:
            self._pending_redemptions = [
                r for r in self._pending_redemptions if now < r.release_at
            ]

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregate fill statistics."""
        self._flush_matured_redemptions()
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
