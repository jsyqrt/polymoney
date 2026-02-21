"""
SimulatedExecutor - Paper trading with depth-based fill simulation.

Extracts all fill simulation logic from the original MarketSimulation class
into a clean OrderExecutor implementation. Supports:
- Depth-based fills using OrderbookSnapshot (with cross-book consistency)
- Cross-orderbook derived spread when one book is missing
- Spread-based probabilistic fills calibrated against real fill rates
- Fill delay simulation with maker vs taker distinction
- Accurate fee model: maker (GTC) pays 0%, taker (FAK/FOK) pays fees
- Fill calibration tracking
- Liquidity competition modeling
"""

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from polymoney.core.logging import get_logger
from polymoney.execution.executor import (
    ExecutionOrder,
    FillEvent,
    MarketExecutionConfig,
    OrderExecutor,
    OrderResult,
    OrderResultStatus,
)

# Import shared types from live_runner (will be relocated in Phase 4)
from polymoney.simulation.live_runner import (
    FillCalibrationTracker,
    LiquidityCompetitionTracker,
    OrderbookSnapshot,
    calculate_taker_fee_rate,
    apply_taker_fee_to_shares,
)

logger = get_logger("execution.simulated")


@dataclass
class PendingOrder:
    """Internal representation of a pending simulated order."""
    order_id: str
    market_id: str
    side: str           # "up" or "down"
    price: float        # Limit price
    size: float         # Remaining size
    original_size: float
    is_taker: bool
    created_at: float   # time.time() when submitted
    
    @property
    def age(self) -> float:
        return time.time() - self.created_at


@dataclass
class MarketState:
    """Per-market execution state for simulation."""
    config: MarketExecutionConfig
    up_orderbook: Optional[OrderbookSnapshot] = None
    down_orderbook: Optional[OrderbookSnapshot] = None
    up_spread: float = 0.0
    down_spread: float = 0.0
    pending_orders: List[PendingOrder] = field(default_factory=list)
    fill_tracker: FillCalibrationTracker = field(default_factory=FillCalibrationTracker)
    next_order_id: int = 0


class SimulatedExecutor(OrderExecutor):
    """
    Paper trading executor with realistic fill simulation.
    
    Simulates order fills using a three-tier model:
    1. Depth-based: Walks orderbook ask levels when snapshot available
    2. Spread-based: Probabilistic fills (20%-90%) based on proximity to market
    3. Taker: Immediate fill at market + slippage for aggressive orders
    
    Also models:
    - Fill delay based on distance from market (1-10s)
    - Taker fees (up to 1.56% at 50c, Polymarket model)
    - Liquidity competition across concurrent markets
    - Orderbook depletion (prevents double-counting liquidity)
    """

    def __init__(
        self,
        liquidity_tracker: Optional[LiquidityCompetitionTracker] = None,
    ):
        self._markets: Dict[str, MarketState] = {}
        self._liquidity_tracker = liquidity_tracker or LiquidityCompetitionTracker()

    # ------------------------------------------------------------------
    # OrderExecutor interface
    # ------------------------------------------------------------------

    async def submit_order(self, order: ExecutionOrder) -> OrderResult:
        """Submit a simulated order, attempting immediate fill."""
        state = self._markets.get(order.market_id)
        if not state:
            return OrderResult(
                status=OrderResultStatus.REJECTED,
                order_id=order.order_id or self._gen_order_id(order.market_id, order.side),
                error=f"Market {order.market_id} not registered",
            )

        # Generate order ID if not provided
        if not order.order_id:
            order.order_id = self._gen_order_id(order.market_id, order.side)

        # Attempt immediate fill via depth/spread model
        fill_result = self._simulate_fill(
            state, order.side, order.size, order.price, order.price,
            order.market_id, is_taker=order.is_taker,
        )

        if fill_result:
            fill_price, filled_size = fill_result

            # Fee model: taker orders (FAK/FOK) pay fees; maker orders (GTC)
            # pay 0% and may receive rebates (modeled as slight share increase).
            if order.is_taker:
                filled_size = apply_taker_fee_to_shares(filled_size, fill_price)
            elif fill_price < order.price:
                # Maker got price improvement — no fees, model rebate as
                # the cost savings from getting a better price
                pass

            if filled_size >= order.size - 0.01:
                # Full fill — track for liquidity competition
                self._liquidity_tracker.record_fill(order.market_id)
                return OrderResult(
                    status=OrderResultStatus.FILLED,
                    order_id=order.order_id,
                    fill_price=fill_price,
                    fill_size=filled_size,
                )
            else:
                # Partial fill — add remainder as pending
                remaining = order.size - filled_size
                pending = PendingOrder(
                    order_id=order.order_id,
                    market_id=order.market_id,
                    side=order.side,
                    price=order.price,
                    size=remaining,
                    original_size=order.size,
                    is_taker=order.is_taker,
                    created_at=time.time(),
                )
                state.pending_orders.append(pending)

                return OrderResult(
                    status=OrderResultStatus.PARTIALLY_FILLED,
                    order_id=order.order_id,
                    fill_price=fill_price,
                    fill_size=filled_size,
                    pending_size=remaining,
                )

        # No immediate fill — add to pending
        pending = PendingOrder(
            order_id=order.order_id,
            market_id=order.market_id,
            side=order.side,
            price=order.price,
            size=order.size,
            original_size=order.size,
            is_taker=order.is_taker,
            created_at=time.time(),
        )
        state.pending_orders.append(pending)

        return OrderResult(
            status=OrderResultStatus.PENDING,
            order_id=order.order_id,
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending simulated order."""
        for state in self._markets.values():
            before = len(state.pending_orders)
            state.pending_orders = [
                o for o in state.pending_orders if o.order_id != order_id
            ]
            if len(state.pending_orders) < before:
                return True
        return False

    async def check_fills(
        self, market_id: str, up_price: float, down_price: float
    ) -> List[FillEvent]:
        """
        Check pending orders for fills against current prices.
        
        Evaluates each pending order against orderbook depth or spread model.
        Also handles stale order cancellation and timeouts.
        """
        state = self._markets.get(market_id)
        if not state:
            return []

        events: List[FillEvent] = []
        remaining: List[PendingOrder] = []
        now = time.time()

        for order in state.pending_orders:
            market_price = up_price if order.side == "up" else down_price
            price_diff = (
                (market_price - order.price) / market_price
                if market_price > 0
                else 0
            )

            # Fill delay — orders need time to attract counterparties
            min_delay = self._get_fill_delay(order.price, market_price)
            if order.age < min_delay:
                remaining.append(order)
                continue

            # Attempt fill
            fill_result = self._simulate_fill(
                state, order.side, order.size, order.price, market_price,
                market_id, is_taker=order.is_taker,
            )

            if fill_result:
                fill_price, filled_size = fill_result

                # Fee model: taker pays fees; maker (GTC) pays nothing
                if order.is_taker:
                    filled_size = apply_taker_fee_to_shares(filled_size, fill_price)

                if filled_size < order.size - 0.01:
                    # Partial fill
                    events.append(FillEvent(
                        order_id=order.order_id,
                        market_id=market_id,
                        side=order.side,
                        fill_price=fill_price,
                        fill_size=filled_size,
                        is_taker=order.is_taker,
                        is_partial=True,
                        remaining_size=order.size - filled_size,
                        timestamp=now,
                    ))
                    order.size -= filled_size
                    remaining.append(order)
                else:
                    # Full fill
                    events.append(FillEvent(
                        order_id=order.order_id,
                        market_id=market_id,
                        side=order.side,
                        fill_price=fill_price,
                        fill_size=filled_size,
                        is_taker=order.is_taker,
                        timestamp=now,
                    ))
                    # Track for liquidity competition
                    self._liquidity_tracker.record_fill(market_id)

            elif price_diff > state.config.stale_order_threshold:
                # Stale order — market moved too far
                events.append(FillEvent(
                    order_id=order.order_id,
                    market_id=market_id,
                    side=order.side,
                    fill_price=0.0,
                    fill_size=0.0,
                    is_cancelled=True,
                    cancel_reason=f"stale: market={market_price:.2f}, diff={price_diff:.1%}",
                    timestamp=now,
                ))

            elif order.age > state.config.order_timeout:
                # Timeout
                events.append(FillEvent(
                    order_id=order.order_id,
                    market_id=market_id,
                    side=order.side,
                    fill_price=0.0,
                    fill_size=0.0,
                    is_cancelled=True,
                    cancel_reason=f"timeout: {order.age:.0f}s > {state.config.order_timeout:.0f}s",
                    timestamp=now,
                ))
            else:
                remaining.append(order)

        state.pending_orders = remaining
        return events

    async def cancel_all(self, market_id: Optional[str] = None) -> int:
        """Cancel all pending orders."""
        count = 0
        if market_id:
            state = self._markets.get(market_id)
            if state:
                count = len(state.pending_orders)
                state.pending_orders.clear()
        else:
            for state in self._markets.values():
                count += len(state.pending_orders)
                state.pending_orders.clear()
        return count

    def register_market(self, config: MarketExecutionConfig) -> None:
        """Register a new market for simulation."""
        self._markets[config.market_id] = MarketState(config=config)

    def unregister_market(self, market_id: str) -> None:
        """Unregister a market, cleaning up state."""
        state = self._markets.pop(market_id, None)
        if state:
            # Export calibration data before cleanup
            cal_stats = state.fill_tracker.get_calibration_stats()
            if cal_stats.get("spread_events", 0) > 0:
                logger.info(
                    f"[{market_id}] Fill calibration: {cal_stats['spread_events']} spread evals, "
                    f"fill_rate={cal_stats.get('spread_fill_rate', 0):.0%}, "
                    f"brier={cal_stats.get('brier_score', 0):.4f}"
                )

    def update_orderbook(
        self, market_id: str, side: str, orderbook: Any
    ) -> None:
        """Update cached orderbook snapshot for a market token."""
        state = self._markets.get(market_id)
        if not state:
            return
        if side == "up":
            state.up_orderbook = orderbook
        else:
            state.down_orderbook = orderbook

    def update_orderbook_incremental(
        self, market_id: str, side: str, changes: Dict[str, Any]
    ) -> None:
        """Apply incremental orderbook changes."""
        state = self._markets.get(market_id)
        if not state:
            return

        book = state.up_orderbook if side == "up" else state.down_orderbook
        if book and book.is_valid:
            book.apply_incremental_update(changes)
        else:
            snapshot = OrderbookSnapshot.from_raw(changes)
            if side == "up":
                state.up_orderbook = snapshot
            else:
                state.down_orderbook = snapshot

    def update_spread(self, market_id: str, side: str, spread: float) -> None:
        """Update the WS-derived spread for a market token."""
        state = self._markets.get(market_id)
        if not state:
            return
        if side == "up":
            state.up_spread = spread
        else:
            state.down_spread = spread

    def get_pending_count(self, market_id: Optional[str] = None) -> int:
        """Get number of pending orders."""
        if market_id:
            state = self._markets.get(market_id)
            return len(state.pending_orders) if state else 0
        return sum(len(s.pending_orders) for s in self._markets.values())

    def get_pending_orders(self, market_id: str) -> List[PendingOrder]:
        """Get pending orders for a market."""
        state = self._markets.get(market_id)
        return list(state.pending_orders) if state else []

    def export_calibration(self, market_id: str, filepath: str) -> int:
        """Export fill calibration data for a market."""
        state = self._markets.get(market_id)
        if state:
            return state.fill_tracker.export_to_jsonl(filepath)
        return 0

    # ------------------------------------------------------------------
    # Private fill simulation methods (extracted from MarketSimulation)
    # ------------------------------------------------------------------

    def _gen_order_id(self, market_id: str, side: str) -> str:
        """Generate a unique order ID."""
        state = self._markets.get(market_id)
        if state:
            state.next_order_id += 1
            return f"{market_id}_{side}_{state.next_order_id}"
        return f"{market_id}_{side}_{int(time.time() * 1000)}"

    def _get_orderbook(self, state: MarketState, side: str) -> Optional[OrderbookSnapshot]:
        """Get valid orderbook for a side, or None if stale/missing."""
        book = state.up_orderbook if side == "up" else state.down_orderbook
        if book and book.is_valid:
            return book
        return None

    def _get_opposite_orderbook(self, state: MarketState, side: str) -> Optional[OrderbookSnapshot]:
        """Get the complementary token's orderbook (UP↔DOWN)."""
        opp = state.down_orderbook if side == "up" else state.up_orderbook
        if opp and opp.is_valid:
            return opp
        return None

    def _derive_spread_from_opposite(self, state: MarketState, side: str) -> float:
        """
        Derive effective spread for `side` from the opposite token's orderbook.
        
        In Polymarket binary markets, the CLOB merges complementary orders:
        a BUY DOWN at price P creates a synthetic SELL UP at price (1-P).
        So the DOWN orderbook gives us information about UP's effective
        liquidity, and vice versa.
        
        Returns 0.0 if no useful data can be derived.
        """
        opp_book = self._get_opposite_orderbook(state, side)
        if not opp_book:
            return 0.0

        # The opposite book's spread maps to our side's spread:
        # If DOWN best_bid=0.38, best_ask=0.42 → spread=0.04
        # Then UP effective: best_ask≈1-0.38=0.62, best_bid≈1-0.42=0.58 → spread≈0.04
        if opp_book.spread > 0:
            return opp_book.spread

        return 0.0

    def _get_effective_spread(self, state: MarketState, side: str) -> float:
        """
        Get effective spread tolerance using real orderbook data.
        
        Priority:
        1. Same-side orderbook depth spread
        2. Cross-derived spread from opposite orderbook
        3. WebSocket bid-ask spread (same side)
        4. WebSocket bid-ask spread (opposite side as proxy)
        5. Conservative 3% fallback
        """
        # 1. Direct orderbook
        book = self._get_orderbook(state, side)
        if book and book.spread > 0:
            return max(book.spread, 0.005)

        # 2. Cross-derived from opposite orderbook
        cross_spread = self._derive_spread_from_opposite(state, side)
        if cross_spread > 0:
            return max(cross_spread, 0.005)

        # 3. WS spread (same side)
        if side == "up" and state.up_spread > 0:
            return max(state.up_spread, 0.005)
        if side == "down" and state.down_spread > 0:
            return max(state.down_spread, 0.005)

        # 4. WS spread (opposite side as proxy)
        if side == "up" and state.down_spread > 0:
            return max(state.down_spread, 0.005)
        if side == "down" and state.up_spread > 0:
            return max(state.up_spread, 0.005)

        # 5. Fallback
        return 0.03

    def _get_fill_delay(self, limit_price: float, market_price: float) -> float:
        """
        Calculate minimum delay before a pending order can fill.
        
        Models real-world observation that limit orders further from
        market price take longer to attract a counterparty.
        
        Returns:
            Minimum age in seconds.
        """
        if market_price <= 0:
            return 1.0
        price_diff = max(0.0, (market_price - limit_price) / market_price)
        return 1.0 + min(price_diff / 0.05, 1.0) * 9.0

    def _simulate_fill(
        self,
        state: MarketState,
        side: str,
        size: float,
        limit_price: float,
        market_price: float,
        market_id: str,
        is_taker: bool = False,
    ) -> Optional[Tuple[float, float]]:
        """
        Simulate an order fill using orderbook depth or spread model.
        
        Architecture:
        1. Depth-based: walks orderbook ask levels (best model when available)
        2. Cross-book assisted: uses opposite orderbook to supplement
        3. Taker: immediate fill for aggressive orders (limit >= market)
        4. Spread-based: probabilistic fills for maker orders within spread
        
        For maker orders (GTC), the fill probability models whether a
        counterparty will match our resting bid. For taker orders (FAK/FOK),
        the fill executes immediately against available asks.
        
        Returns:
            (fill_price, filled_size) or None if no fill.
        """
        book = self._get_orderbook(state, side)

        # Staleness check: if orderbook best_ask diverges >15% from WS price,
        # the depth data is stale — fall through to spread model
        if book:
            book_best_ask = book.best_ask
            if book_best_ask is not None:
                divergence = (
                    abs(book_best_ask - market_price) / market_price
                    if market_price > 0
                    else 0
                )
                if divergence > 0.15:
                    logger.debug(
                        f"[{market_id}] Stale orderbook: {side.upper()} "
                        f"book_ask={book_best_ask:.4f} vs market={market_price:.4f} "
                        f"(div={divergence:.1%}). Using spread model."
                    )
                    book = None

        if book:
            # Depth-based fill (walks asks — models both taker fills and maker
            # fills where market has moved to our price)
            result = book.simulate_buy_fill(size, limit_price)
            if result:
                avg_fill_price, filled_size = result
                noise = random.uniform(-0.0005, 0.0005)
                avg_fill_price = max(0.01, min(0.99, avg_fill_price * (1 + noise)))
                return (avg_fill_price, filled_size)

            # No direct fill from same-side book. For maker orders, also check
            # if the opposite book's bid depth provides additional liquidity.
            # In Polymarket, BUY DOWN at P creates synthetic SELL UP at (1-P).
            if not is_taker:
                opp_book = self._get_opposite_orderbook(state, side)
                if opp_book and opp_book.bids:
                    mirrored_fill = self._try_cross_book_fill(
                        opp_book, size, limit_price
                    )
                    if mirrored_fill:
                        return mirrored_fill

            return None  # Limit below all asks

        # --- Spread-based model (no primary depth data) ---
        spread_tolerance = self._get_effective_spread(state, side)
        price_diff = (
            (market_price - limit_price) / market_price
            if market_price > 0
            else 0
        )

        # Size-based slippage
        order_value = size * limit_price
        size_slippage = min(0.005, order_value * 0.0001)

        if limit_price >= market_price:
            # Aggressive (taker) — fill at market + slippage
            fill_price = min(market_price * (1 + size_slippage), 0.99)
            state.fill_tracker.record(
                side=side,
                limit_price=limit_price,
                market_price=market_price,
                size=size,
                spread_tolerance=spread_tolerance,
                fill_probability=1.0,
                filled=True,
                fill_source="taker",
                slug=market_id,
            )
            return (fill_price, size)

        elif price_diff <= spread_tolerance:
            # Maker order within spread — probabilistic fill
            proximity = (
                1.0 - (price_diff / spread_tolerance)
                if spread_tolerance > 0
                else 1.0
            )

            # Cross-book liquidity boost: if the opposite orderbook has
            # significant bid depth at the mirrored price, our maker order
            # is more likely to be matched via complementary matching.
            cross_boost = self._estimate_cross_book_boost(
                state, side, limit_price
            )

            # Base probability calibrated against observed live fill rates:
            # - At spread edge (proximity=0): ~15% (was 20%)
            # - At best bid (proximity=1): ~85% (was 90%)
            # - Cross-book liquidity adds up to +15% boost
            base_probability = 0.15 + 0.70 * proximity + cross_boost

            size_penalty = (
                1.0
                if size <= 50
                else max(0.5, 1.0 - (size - 50) * 0.002)
            )
            fill_probability = min(0.95, base_probability * size_penalty)

            # Liquidity competition penalty
            competition_factor = self._liquidity_tracker.get_competition_factor(
                market_id
            )
            fill_probability *= competition_factor

            filled = random.random() < fill_probability

            state.fill_tracker.record(
                side=side,
                limit_price=limit_price,
                market_price=market_price,
                size=size,
                spread_tolerance=spread_tolerance,
                fill_probability=fill_probability,
                filled=filled,
                fill_source="spread" + ("+cross" if cross_boost > 0 else ""),
                slug=market_id,
            )

            if filled:
                return (limit_price, size)

        return None

    def _try_cross_book_fill(
        self,
        opp_book: OrderbookSnapshot,
        size: float,
        limit_price: float,
    ) -> Optional[Tuple[float, float]]:
        """
        Try to fill a maker order using the opposite token's orderbook.
        
        In Polymarket, a BUY DOWN bid at price P creates a synthetic
        SELL UP at price (1-P). So if we want to BUY UP at limit 0.58,
        we can fill against DOWN bids at >= 0.42 (because 1-0.42 = 0.58).
        
        This models the CLOB's complementary order matching.
        """
        mirrored_limit = 1.0 - limit_price  # minimum DOWN bid price
        filled = 0.0
        total_cost = 0.0

        for bid_price, bid_size in opp_book.bids:
            if bid_price < mirrored_limit:
                break  # bids sorted desc; remaining are below threshold
            # This DOWN bid at bid_price creates a synthetic UP ask at (1-bid_price)
            synthetic_ask_price = 1.0 - bid_price
            fill_at_level = min(size - filled, bid_size)
            filled += fill_at_level
            total_cost += fill_at_level * synthetic_ask_price

            if filled >= size - 0.001:
                break

        if filled > 0:
            avg_price = total_cost / filled
            noise = random.uniform(-0.0005, 0.0005)
            avg_price = max(0.01, min(0.99, avg_price * (1 + noise)))
            return (avg_price, min(filled, size))

        return None

    def _estimate_cross_book_boost(
        self, state: MarketState, side: str, limit_price: float
    ) -> float:
        """
        Estimate additional fill probability from cross-book liquidity.
        
        If the opposite orderbook has significant bid depth at the mirrored
        price level, it means more counterparties are available through
        complementary matching, increasing our fill probability.
        
        Returns a probability boost between 0.0 and 0.15.
        """
        opp_book = self._get_opposite_orderbook(state, side)
        if not opp_book or not opp_book.bids:
            return 0.0

        mirrored_price = 1.0 - limit_price
        # Count bid depth at or above our mirrored price
        available = sum(
            size for price, size in opp_book.bids if price >= mirrored_price
        )

        # Normalize: 100+ shares of cross-book depth → full boost
        if available <= 0:
            return 0.0
        return min(0.15, available / 100.0 * 0.15)
