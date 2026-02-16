"""
LiveExecutor - Real order execution via Polymarket CLOB API.

Submits and manages real orders on Polymarket using py-clob-client.
Tracks fills by polling order status from the exchange.

Requires:
- Configured ClobClient with valid private key
- Market registration with correct token IDs
- FillManager for fill tracking and reconciliation
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from polymoney.core.logging import get_logger
from polymoney.execution.executor import (
    ExecutionOrder,
    FillEvent,
    MarketExecutionConfig,
    OrderExecutor,
    OrderResult,
    OrderResultStatus,
)

logger = get_logger("execution.live")


@dataclass
class LivePendingOrder:
    """Tracks a live order submitted to the exchange."""
    order_id: str           # Internal order ID
    exchange_order_id: str  # Polymarket CLOB order ID
    market_id: str
    side: str
    token_id: str           # Actual Polymarket token ID
    price: float
    size: float             # Original size
    filled_size: float = 0.0
    created_at: float = 0.0
    last_checked: float = 0.0
    is_taker: bool = False


class LiveExecutor(OrderExecutor):
    """
    Real order execution via Polymarket CLOB.
    
    Uses py-clob-client for:
    - Order creation and signing (EIP-712)
    - Order submission (POST /order)
    - Order cancellation (DELETE /order)
    - Order status polling (GET /order)
    
    Token ID resolution:
    - Markets are registered with up_token_id and down_token_id
    - submit_order() resolves (market_id, side) -> token_id
    
    Fill tracking:
    - Polls order status every 2s for active orders
    - Recently submitted orders polled more frequently (500ms for 5s)
    - Reports fills back via check_fills() return value
    """

    def __init__(self, clob_client=None):
        """
        Initialize live executor.
        
        Args:
            clob_client: Initialized py-clob-client ClobClient instance.
                         Can be set later via set_client().
        """
        self._client = clob_client
        self._markets: Dict[str, MarketExecutionConfig] = {}
        self._pending: Dict[str, LivePendingOrder] = {}  # order_id -> order
        self._max_retries = 3
        self._retry_delay = 1.0  # seconds, exponential backoff

    def set_client(self, clob_client) -> None:
        """Set or update the CLOB client."""
        self._client = clob_client
        logger.info("CLOB client configured for live execution")

    @property
    def is_ready(self) -> bool:
        """Check if executor has a configured client."""
        return self._client is not None

    # ------------------------------------------------------------------
    # OrderExecutor interface
    # ------------------------------------------------------------------

    async def submit_order(self, order: ExecutionOrder) -> OrderResult:
        """Submit a real order to Polymarket CLOB."""
        if not self._client:
            return OrderResult(
                status=OrderResultStatus.REJECTED,
                order_id=order.order_id or f"live_{uuid.uuid4().hex[:8]}",
                error="CLOB client not configured",
            )

        market_config = self._markets.get(order.market_id)
        if not market_config:
            return OrderResult(
                status=OrderResultStatus.REJECTED,
                order_id=order.order_id or f"live_{uuid.uuid4().hex[:8]}",
                error=f"Market {order.market_id} not registered",
            )

        # Resolve token ID from (market_id, side)
        token_id = self._resolve_token_id(market_config, order.side)
        if not token_id:
            return OrderResult(
                status=OrderResultStatus.REJECTED,
                order_id=order.order_id or f"live_{uuid.uuid4().hex[:8]}",
                error=f"Cannot resolve token_id for {order.market_id} {order.side}",
            )

        order_id = order.order_id or f"live_{uuid.uuid4().hex[:8]}"

        for attempt in range(self._max_retries):
            try:
                from py_clob_client.clob_types import OrderArgs
                from py_clob_client.order_builder.constants import BUY

                order_args = OrderArgs(
                    price=order.price,
                    size=order.size,
                    side=BUY,  # Strategy always buys tokens
                    token_id=token_id,
                )

                signed_order = self._client.create_order(order_args)
                response = self._client.post_order(signed_order, "GTC")

                exchange_order_id = ""
                if response and isinstance(response, dict):
                    exchange_order_id = response.get("orderID", "")

                # Track pending order
                pending = LivePendingOrder(
                    order_id=order_id,
                    exchange_order_id=exchange_order_id or order_id,
                    market_id=order.market_id,
                    side=order.side,
                    token_id=token_id,
                    price=order.price,
                    size=order.size,
                    created_at=time.time(),
                    last_checked=time.time(),
                    is_taker=order.is_taker,
                )
                self._pending[order_id] = pending

                logger.info(
                    f"Live order placed: {order_id} {order.side.upper()} "
                    f"{order.size}@{order.price} token={token_id[:16]}... "
                    f"exchange_id={exchange_order_id}"
                )

                return OrderResult(
                    status=OrderResultStatus.PENDING,
                    order_id=order_id,
                    exchange_order_id=exchange_order_id,
                )

            except Exception as e:
                delay = self._retry_delay * (2 ** attempt)
                logger.warning(
                    f"Order submission failed (attempt {attempt+1}/{self._max_retries}): {e}"
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Order submission exhausted retries: {e}")
                    return OrderResult(
                        status=OrderResultStatus.REJECTED,
                        order_id=order_id,
                        error=str(e),
                    )

        # Should not reach here
        return OrderResult(
            status=OrderResultStatus.REJECTED,
            order_id=order_id,
            error="Unknown error",
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a live order on the exchange."""
        if not self._client:
            return False

        pending = self._pending.get(order_id)
        if not pending:
            return False

        try:
            self._client.cancel(pending.exchange_order_id)
            del self._pending[order_id]
            logger.info(f"Live order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def check_fills(
        self, market_id: str, up_price: float, down_price: float
    ) -> List[FillEvent]:
        """
        Poll exchange for fill updates on pending orders.
        
        Checks order status via CLOB API and reports fills.
        """
        if not self._client:
            return []

        events: List[FillEvent] = []
        now = time.time()
        to_remove: List[str] = []

        for order_id, pending in self._pending.items():
            if pending.market_id != market_id:
                continue

            # Adaptive polling interval
            age = now - pending.created_at
            interval = 0.5 if age < 5.0 else 2.0 if age < 60.0 else 10.0
            if now - pending.last_checked < interval:
                continue

            pending.last_checked = now

            try:
                # Poll order status from exchange
                order_status = self._client.get_order(pending.exchange_order_id)
                if not order_status:
                    continue

                status = order_status.get("status", "")
                filled_size = float(order_status.get("size_matched", 0))

                if status == "MATCHED" or filled_size >= pending.size - 0.01:
                    # Fully filled
                    avg_price = float(order_status.get("price", pending.price))
                    events.append(FillEvent(
                        order_id=order_id,
                        market_id=market_id,
                        side=pending.side,
                        fill_price=avg_price,
                        fill_size=filled_size,
                        is_taker=pending.is_taker,
                        timestamp=now,
                    ))
                    to_remove.append(order_id)
                    logger.info(
                        f"Live fill: {order_id} {pending.side.upper()} "
                        f"{filled_size}@{avg_price}"
                    )

                elif filled_size > pending.filled_size + 0.01:
                    # Partial fill — new shares filled since last check
                    new_fill = filled_size - pending.filled_size
                    avg_price = float(order_status.get("price", pending.price))
                    events.append(FillEvent(
                        order_id=order_id,
                        market_id=market_id,
                        side=pending.side,
                        fill_price=avg_price,
                        fill_size=new_fill,
                        is_taker=pending.is_taker,
                        is_partial=True,
                        remaining_size=pending.size - filled_size,
                        timestamp=now,
                    ))
                    pending.filled_size = filled_size

                elif status in ("CANCELLED", "EXPIRED"):
                    events.append(FillEvent(
                        order_id=order_id,
                        market_id=market_id,
                        side=pending.side,
                        fill_price=0.0,
                        fill_size=0.0,
                        is_cancelled=True,
                        cancel_reason=f"exchange: {status}",
                        timestamp=now,
                    ))
                    to_remove.append(order_id)

            except Exception as e:
                logger.warning(f"Failed to check order {order_id}: {e}")

        for order_id in to_remove:
            self._pending.pop(order_id, None)

        return events

    async def cancel_all(self, market_id: Optional[str] = None) -> int:
        """Cancel all pending live orders."""
        count = 0
        to_cancel = [
            oid
            for oid, p in self._pending.items()
            if market_id is None or p.market_id == market_id
        ]
        for order_id in to_cancel:
            if await self.cancel_order(order_id):
                count += 1
        return count

    def register_market(self, config: MarketExecutionConfig) -> None:
        """Register a market with its token IDs."""
        self._markets[config.market_id] = config
        logger.info(
            f"Market registered for live trading: {config.market_id} "
            f"(up={config.up_token_id[:16]}..., down={config.down_token_id[:16]}...)"
        )

    def unregister_market(self, market_id: str) -> None:
        """Unregister a market and cancel its orders."""
        self._markets.pop(market_id, None)
        # Cancel any remaining orders for this market
        to_remove = [
            oid for oid, p in self._pending.items()
            if p.market_id == market_id
        ]
        for oid in to_remove:
            self._pending.pop(oid, None)

    def get_pending_count(self, market_id: Optional[str] = None) -> int:
        """Get number of pending orders."""
        if market_id:
            return sum(
                1 for p in self._pending.values() if p.market_id == market_id
            )
        return len(self._pending)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_token_id(
        self, config: MarketExecutionConfig, side: str
    ) -> Optional[str]:
        """Resolve (market, side) to actual Polymarket token ID."""
        if side in ("up", "yes"):
            return config.up_token_id
        elif side in ("down", "no"):
            return config.down_token_id
        return None
