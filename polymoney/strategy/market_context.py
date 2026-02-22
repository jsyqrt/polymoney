"""
MarketContext - Lightweight market state container.

Extracted from the monolithic MarketSimulation, this class holds:
- Market metadata (slug, coin, condition_id, token IDs)
- Strategy instance (PositionArbitrageStrategy)
- Price tracking (WS warmup, tick cooldown, staleness)
- Result tracking (MarketResult)
- Spread info for adaptive pricing

Execution logic (fill simulation, order submission) is delegated to
OrderExecutor. Orderbook management is delegated to MarketDataProvider
(or OrderExecutor for SimulatedExecutor).
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from polymoney.core.logging import get_logger
from polymoney.core.models import OrderSignal, PriceData, TokenType
from polymoney.data.real_data_fetcher import PriceUpdate
from polymoney.simulation.live_runner import MarketResult, SimulationConfig
from polymoney.strategy.builtin.position_arbitrage import (
    LimitOrder,
    PositionArbitrageStrategy,
)

logger = get_logger("strategy.market_context")


class MarketContext:
    """
    Manages state for a single market within the StrategyEngine.
    
    Holds the strategy instance and market metadata, processes price
    updates to generate order signals, and tracks results. Does NOT
    manage order execution — that's the OrderExecutor's job.
    
    Lifecycle:
        1. Created when MarketDataProvider discovers a new market
        2. Receives price updates -> generates OrderSignals
        3. Receives fill events -> updates positions
        4. Finalized when settlement is detected
    """

    def __init__(
        self,
        market: Dict[str, Any],
        config: SimulationConfig,
    ):
        self.market = market
        self.config = config
        self.slug = market["slug"]
        self.coin = market.get("coin", "btc")

        # Strategy instance
        strategy_params = {
            "target_cost": config.target_cost,
            "batch_ratio": config.batch_ratio,
            "ecr_threshold": config.ecr_threshold,
            "enable_ecr_stoploss": config.enable_ecr_stoploss,
            "enable_rebalancing": config.enable_rebalancing,
            "enable_trend_detection": config.enable_trend_detection,
            "enable_urgency_pricing": config.enable_urgency_pricing,
        }
        self.strategy = PositionArbitrageStrategy(
            strategy_id=f"ctx_{self.slug}",
            name=f"Context {self.slug}",
            position_size=config.position_size,
            params=strategy_params,
        )

        # Result tracking
        self.result = MarketResult(
            slug=self.slug,
            coin=self.coin,
            condition_id=market.get("condition_id", ""),
            start_time=time.time(),
        )

        # Price tracking
        self.last_up_price: Optional[float] = None
        self.last_down_price: Optional[float] = None
        self.last_up_spread: float = 0.0
        self.last_down_spread: float = 0.0
        self.last_price_update_time: float = 0.0
        self.price_stale_threshold: float = 10.0

        # WS warmup: don't generate orders until both sides have confirmed prices
        self._ws_confirmed_up: bool = False
        self._ws_confirmed_down: bool = False
        self._ws_warmup_logged: bool = False

        # Tick cooldown: limit order generation rate (1s between ticks)
        self._last_order_tick_time: float = 0.0
        self._order_tick_interval: float = 1.0

        # Order ID generator
        self._next_order_id: int = 0

    # ------------------------------------------------------------------
    # Price handling
    # ------------------------------------------------------------------

    def process_price_update(self, price: PriceUpdate) -> List[OrderSignal]:
        """
        Process a price update and return order signals.
        
        Updates internal price tracking, handles WS warmup, and
        delegates to strategy for signal generation.
        
        Args:
            price: Price update from WS/trade
            
        Returns:
            List of OrderSignals to be submitted to OrderExecutor
        """
        up_token_id = self.market.get("up_token_id")
        down_token_id = self.market.get("down_token_id")

        # Update price and mark WS side as confirmed
        if price.token_id == up_token_id:
            self.last_up_price = price.mid_price
            self.last_up_spread = price.spread
            self._ws_confirmed_up = True
        elif price.token_id == down_token_id:
            self.last_down_price = price.mid_price
            self.last_down_spread = price.spread
            self._ws_confirmed_down = True

        self.last_price_update_time = time.time()

        # WS warmup gate
        if not (self._ws_confirmed_up and self._ws_confirmed_down):
            if not self._ws_warmup_logged:
                waiting = []
                if not self._ws_confirmed_up:
                    waiting.append("UP")
                if not self._ws_confirmed_down:
                    waiting.append("DOWN")
                logger.debug(
                    f"[{self.slug}] WS warmup: waiting for "
                    f"{', '.join(waiting)} confirmation"
                )
            return []

        if not self._ws_warmup_logged:
            self._ws_warmup_logged = True
            logger.info(
                f"[{self.slug}] WS warmup complete: "
                f"UP={self.last_up_price:.4f}, DOWN={self.last_down_price:.4f}"
            )

        # Skip if both prices are not available
        if self.last_up_price is None or self.last_down_price is None:
            return []

        # Tick cooldown
        now = time.time()
        if now - self._last_order_tick_time < self._order_tick_interval:
            return []
        self._last_order_tick_time = now

        # Build PriceData for strategy
        price_data = PriceData(
            market_id=self.slug,
            up_price=self.last_up_price,
            down_price=self.last_down_price,
            timestamp=datetime.fromtimestamp(now),
        )

        # Update spread info for adaptive pricing
        self.strategy.update_spread_info(
            self.last_up_spread, self.last_down_spread
        )

        # Get signals from strategy
        signals = self.strategy.on_price_update(price_data)
        return signals

    # ------------------------------------------------------------------
    # Fill handling (called by StrategyEngine when executor reports fills)
    # ------------------------------------------------------------------

    def apply_fill(
        self, side: str, size: float, fill_price: float, is_taker: bool = False
    ) -> None:
        """
        Apply a fill event to update positions.
        
        Called by StrategyEngine when OrderExecutor reports a fill.
        
        Args:
            side: "up" or "down"
            size: Shares filled
            fill_price: Average fill price
            is_taker: Whether this was a taker fill
        """
        cost = size * fill_price
        self.result.orders_filled += 1
        self.result.total_buy_cost += cost

        if side == "up":
            self.result.up_shares += size
            self.result.up_cost += cost
            self.strategy.up_position.add(size, fill_price)
        else:
            self.result.down_shares += size
            self.result.down_cost += cost
            self.strategy.down_position.add(size, fill_price)

    def apply_sell(
        self, side: str, size: float, sell_price: float
    ) -> None:
        """Apply a sell fill — reduce position and record realized proceeds."""
        proceeds = size * sell_price
        pos = self.strategy.up_position if side == "up" else self.strategy.down_position

        if pos.shares < size - 0.01:
            logger.warning(
                f"Sell size {size:.2f} > held shares {pos.shares:.2f} "
                f"for {self.slug} {side}, capping"
            )
            size = pos.shares
            proceeds = size * sell_price

        if size <= 0:
            return

        avg = pos.avg_price
        pos.shares -= size
        pos.cost -= size * avg

        if side == "up":
            self.result.up_shares -= size
            self.result.up_cost -= size * avg
        else:
            self.result.down_shares -= size
            self.result.down_cost -= size * avg

        self.result.sell_proceeds += proceeds

        logger.info(
            f"Sell applied: {self.slug} {side.upper()} "
            f"{size:.2f}@{sell_price:.4f} (proceeds=${proceeds:.2f}, "
            f"total_sell_proceeds=${self.result.sell_proceeds:.2f})"
        )

    def record_order_submitted(self) -> None:
        """Record that an order was submitted."""
        self.result.orders_submitted += 1

    def remove_pending_order(self, order_id: str) -> None:
        """Remove a pending order from the strategy (on fill or cancel)."""
        self.strategy.pending_orders = [
            p for p in self.strategy.pending_orders if p.order_id != order_id
        ]

    def link_order(
        self, signal: OrderSignal, order_id: str, is_taker: bool = False
    ) -> None:
        """
        Link a strategy-generated LimitOrder with an executor order ID.
        
        The strategy creates LimitOrder objects in on_price_update().
        We augment them with the executor-assigned order_id.
        """
        token_type = (
            signal.token_type
            if isinstance(signal.token_type, str)
            else signal.token_type.value
        )
        side_str = "up" if token_type in ("yes", "YES") else "down"

        for pending in reversed(self.strategy.pending_orders):
            if (
                pending.side == side_str
                and abs(pending.price - signal.target_price) < 0.001
            ):
                if self.slug in pending.order_id:
                    continue  # Already linked
                pending.order_id = order_id
                pending.timestamp = time.time()
                pending.is_taker = is_taker
                break

    def sync_pending_orders(self) -> None:
        """Clean up invalid pending orders."""
        before = len(self.strategy.pending_orders)
        self.strategy.pending_orders = [
            o
            for o in self.strategy.pending_orders
            if o.status == "pending" and o.shares > 0.001
        ]
        removed = before - len(self.strategy.pending_orders)
        if removed > 0:
            logger.info(
                f"[{self.slug}] Sync: cleaned {removed} invalid pending orders"
            )

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def finalize(self, winner: str) -> MarketResult:
        """Finalize the market with settlement."""
        self.result.winner = winner
        self.result.end_time = time.time()
        self.result.calculate_final_metrics()
        return self.result

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def prices_are_stale(self) -> bool:
        if self.last_price_update_time == 0:
            return True
        return (time.time() - self.last_price_update_time) > self.price_stale_threshold

    def get_current_prices(self) -> Tuple[Optional[float], Optional[float]]:
        if self.prices_are_stale:
            return (None, None)
        return (self.last_up_price, self.last_down_price)

    def get_status(self) -> Dict[str, Any]:
        """Get current market status for reporting."""
        up_p, down_p = self.get_current_prices()
        return {
            "coin": self.coin,
            "up_shares": self.result.up_shares,
            "down_shares": self.result.down_shares,
            "up_cost": self.result.up_cost,
            "down_cost": self.result.down_cost,
            "ecr": (
                self.result.ecr if self.result.ecr != float("inf") else 999.99
            ),
            "balance_ratio": self.result.balance_ratio,
            "total_buy_cost": self.result.total_buy_cost,
            "sell_proceeds": self.result.sell_proceeds,
            "orders_submitted": self.result.orders_submitted,
            "orders_filled": self.result.orders_filled,
            "current_up_price": up_p,
            "current_down_price": down_p,
            "prices_stale": self.prices_are_stale,
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(self.result.start_time)
            ),
            "pending_orders": len(self.strategy.pending_orders),
        }

    @property
    def is_ws_warmed_up(self) -> bool:
        return self._ws_confirmed_up and self._ws_confirmed_down
