"""
Position Arbitrage Strategy - Market Maker Arbitrage

Core principle:
- In a binary market, UP + DOWN always settles to $1
- If total cost < $1, profit is guaranteed
- Uses limit orders to buy below market price, reducing cost

Strategy logic:
1. Calculate limit prices so that up_limit + down_limit = target_cost < 100%
2. Place orders on both sides
3. Manage order lifecycle (timeout, price deviation)
4. Track positions and calculate metrics
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from polymoney.core.logging import get_logger
from polymoney.core.models import OrderSignal, Position, PriceData, TokenType, TradeSide
from polymoney.core.strategy import BaseStrategy, register_strategy

logger = get_logger("strategy.position_arbitrage")


@dataclass
class InternalPosition:
    """Internal position tracking."""

    shares: float = 0.0
    cost: float = 0.0

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares > 0 else 0.0

    def add(self, shares: float, price: float):
        self.shares += shares
        self.cost += shares * price

    def reset(self):
        self.shares = 0.0
        self.cost = 0.0


@dataclass
class LimitOrder:
    """Internal limit order tracking."""

    order_id: str
    side: str  # 'up' or 'down'
    price: float
    shares: float
    cost: float = 0.0
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.now)
    created_market_price: float = 0.0


@register_strategy("position-arbitrage")
class PositionArbitrageStrategy(BaseStrategy):
    """
    Position Arbitrage / Market Maker Strategy.

    Buys both UP and DOWN tokens below market price to achieve
    a total cost < $1, guaranteeing profit regardless of outcome.

    Parameters:
        target_cost: Target cost rate (default 0.98 = 98%)
        batch_size: Order batch size in dollars (default 5.0)
        order_timeout: Order timeout in seconds (default 60)
        phase1_end: End of phase 1 in seconds (default 300)
        phase2_end: End of phase 2 in seconds (default 600)
        low_prob_threshold: Don't buy tokens below this price (default 0.05)
        ecr_threshold: ECR stop-loss threshold (default 1.05 = 105%)
        balance_threshold: Position balance threshold for warning (default 0.70)
        enable_ecr_stoploss: Enable ECR stop-loss mechanism (default True)
        enable_rebalancing: Enable market order rebalancing (default False)
        rebalancing_cooldown: Cooldown between rebalancing orders in seconds (default 60)
    """

    def __init__(
        self,
        strategy_id: str = "position_arbitrage",
        name: str = "Position Arbitrage",
        position_size: float = 100.0,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(strategy_id, name, position_size, params)

        # Strategy parameters
        self.target_cost = self.params.get("target_cost", 0.98)
        self.batch_size = self.params.get("batch_size", 5.0)
        self.order_timeout = self.params.get("order_timeout", 60)
        self.phase1_end = self.params.get("phase1_end", 300)
        self.phase2_end = self.params.get("phase2_end", 600)
        self.low_prob_threshold = self.params.get("low_prob_threshold", 0.05)
        
        # Risk control parameters
        self.ecr_threshold = self.params.get("ecr_threshold", 1.05)  # 105%
        self.balance_threshold = self.params.get("balance_threshold", 0.70)
        self.enable_ecr_stoploss = self.params.get("enable_ecr_stoploss", True)
        self.enable_rebalancing = self.params.get("enable_rebalancing", False)
        self.rebalancing_cooldown = self.params.get("rebalancing_cooldown", 60)

        # Internal position tracking
        self.up_position = InternalPosition()
        self.down_position = InternalPosition()

        # Order tracking
        self.pending_orders: List[LimitOrder] = []
        self._order_counter = 0

        # Market state
        self.current_up_price = 0.5
        self.current_down_price = 0.5
        self.market_start_time: Optional[datetime] = None
        
        # Risk state tracking
        self._risk_state = "normal"  # "normal", "warning", "limited"
        self._ecr_violations = 0
        self._last_rebalancing_time: Optional[datetime] = None
        self._rebalancing_events: List[Dict[str, Any]] = []

    @property
    def strategy_type(self) -> str:
        return "market_maker"

    @property
    def description(self) -> str:
        return f"Position Arbitrage (target {self.target_cost:.0%})"

    @property
    def hedged_position(self) -> float:
        """Minimum of UP and DOWN positions (guaranteed profit)."""
        return min(self.up_position.shares, self.down_position.shares)

    @property
    def effective_cost_rate(self) -> float:
        """Total cost / minimum position. Must be < 1 for guaranteed profit."""
        total_cost = self.up_position.cost + self.down_position.cost
        min_shares = min(self.up_position.shares, self.down_position.shares)
        return total_cost / min_shares if min_shares > 0 else float("inf")

    @property
    def balance_ratio(self) -> float:
        """Position balance: min/max shares. Closer to 1 is better."""
        up = self.up_position.shares
        down = self.down_position.shares
        if up == 0 and down == 0:
            return 1.0
        if up == 0 or down == 0:
            return 0.0
        return min(up, down) / max(up, down)
    
    @property
    def risk_state(self) -> str:
        """
        Get current risk state.
        
        Returns:
            - "normal": ECR < 100%, healthy
            - "warning": ECR >= 100% but < threshold
            - "limited": ECR >= threshold, stop generating orders
        """
        ecr = self.effective_cost_rate
        if ecr == float("inf"):
            return "normal"  # No position yet
        
        if ecr >= self.ecr_threshold:
            return "limited"
        elif ecr >= 1.0:
            return "warning"
        return "normal"
    
    def calculate_projected_ecr(self, side: str, shares: float, price: float) -> float:
        """
        Calculate projected ECR after a hypothetical order fills.
        
        Args:
            side: "up" or "down"
            shares: Number of shares in the hypothetical order
            price: Price per share
            
        Returns:
            Projected effective cost rate (returns 0.0 if no hedged position)
        """
        # Current state
        up_shares = self.up_position.shares
        down_shares = self.down_position.shares
        total_cost = self.up_position.cost + self.down_position.cost
        
        # Add hypothetical order
        if side == "up":
            up_shares += shares
        else:
            down_shares += shares
        total_cost += shares * price
        
        min_shares = min(up_shares, down_shares)
        if min_shares <= 0:
            # No hedged position yet - ECR is not meaningful
            # Return 0 to allow building initial position
            return 0.0
        
        return total_cost / min_shares
    
    def is_position_imbalanced(self) -> bool:
        """Check if position is severely imbalanced."""
        return self.balance_ratio < self.balance_threshold
    
    def can_rebalance(self) -> bool:
        """Check if rebalancing is allowed (not in cooldown)."""
        if not self.enable_rebalancing:
            return False
        
        if self._last_rebalancing_time is None:
            return True
        
        elapsed = (datetime.now() - self._last_rebalancing_time).total_seconds()
        return elapsed >= self.rebalancing_cooldown

    def get_market_phase(self) -> int:
        """Get current market phase (1, 2, or 3)."""
        if self.market_start_time is None:
            return 1

        elapsed = (datetime.now() - self.market_start_time).total_seconds()

        if elapsed < self.phase1_end:
            return 1
        elif elapsed < self.phase2_end:
            return 2
        return 3

    def get_lagging_side(self) -> str:
        """Get the side with fewer shares."""
        if self.up_position.shares < self.down_position.shares:
            return "up"
        elif self.down_position.shares < self.up_position.shares:
            return "down"
        return "balanced"

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self.up_position.reset()
        self.down_position.reset()
        self.pending_orders.clear()
        self._order_counter = 0
        self.market_start_time = None
        # Reset risk state
        self._risk_state = "normal"
        self._ecr_violations = 0
        self._last_rebalancing_time = None
        self._rebalancing_events.clear()

    def on_market_start(self, market_id: str, market_info: Dict[str, Any]) -> None:
        """Initialize for a new market."""
        self.reset()
        self.market_start_time = datetime.now()
        logger.info(f"[{self.name}] Market started: {market_id}")

    def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
        """Process price update and generate order signals."""
        self.current_up_price = price_data.up_price
        self.current_down_price = price_data.down_price

        signals = []
        
        # Update risk state
        current_risk = self.risk_state
        if current_risk != self._risk_state:
            self._risk_state = current_risk
            logger.info(f"[{self.name}] Risk state changed to: {current_risk} (ECR: {self.effective_cost_rate:.2%})")
        
        # ECR stop-loss check
        if self.enable_ecr_stoploss and current_risk == "limited":
            ecr = self.effective_cost_rate
            self._ecr_violations += 1
            if self._ecr_violations <= 3:  # Log first few violations
                logger.warning(
                    f"[{self.name}] ECR STOP-LOSS: ECR {ecr:.2%} exceeds threshold {self.ecr_threshold:.2%}. "
                    f"Stopping new orders."
                )
            return signals  # Return empty - no new orders
        
        # Check for rebalancing opportunity
        if self.is_position_imbalanced() and self.can_rebalance():
            rebalance_signal = self._generate_rebalancing_order(price_data)
            if rebalance_signal:
                signals.append(rebalance_signal)
                self._last_rebalancing_time = datetime.now()
                # Record rebalancing event
                self._rebalancing_events.append({
                    "timestamp": datetime.now().isoformat(),
                    "side": "up" if rebalance_signal.token_type == TokenType.YES else "down",
                    "size": rebalance_signal.size,
                    "price": rebalance_signal.target_price,
                    "balance_before": self.balance_ratio,
                })
                return signals  # Return just the rebalancing order

        # Calculate limit prices
        up_limit, down_limit = self._calculate_limit_prices(
            price_data.up_price, price_data.down_price
        )

        # Check available budget
        total_cost = self.up_position.cost + self.down_position.cost
        pending_cost = sum(o.cost for o in self.pending_orders)
        available = self.position_size - total_cost - pending_cost

        if available < self.batch_size:
            return signals

        phase = self.get_market_phase()
        lagging_side = self.get_lagging_side()

        # Phase 3: Focus on lagging side, avoid low probability
        if phase == 3:
            if lagging_side == "up" and price_data.up_price >= self.low_prob_threshold:
                if self._check_ecr_before_order("up", up_limit, self.batch_size):
                    signals.append(self._create_signal("up", up_limit, self.batch_size))
            elif lagging_side == "down" and price_data.down_price >= self.low_prob_threshold:
                if self._check_ecr_before_order("down", down_limit, self.batch_size):
                    signals.append(self._create_signal("down", down_limit, self.batch_size))
        else:
            # Phase 1-2: Normal operation, prioritize lagging side
            if lagging_side == "up" or lagging_side == "balanced":
                if self._should_place_order("up", up_limit):
                    if self._check_ecr_before_order("up", up_limit, self.batch_size):
                        signals.append(self._create_signal("up", up_limit, self.batch_size))
                        available -= self.batch_size

            if available >= self.batch_size:
                if lagging_side == "down" or lagging_side == "balanced":
                    if self._should_place_order("down", down_limit):
                        if self._check_ecr_before_order("down", down_limit, self.batch_size):
                            signals.append(self._create_signal("down", down_limit, self.batch_size))

        return signals
    
    def _check_ecr_before_order(self, side: str, price: float, cost: float) -> bool:
        """Check if placing this order would exceed ECR threshold."""
        if not self.enable_ecr_stoploss:
            return True
        
        shares = cost / price
        projected_ecr = self.calculate_projected_ecr(side, shares, price)
        
        if projected_ecr >= self.ecr_threshold:
            logger.debug(
                f"[{self.name}] Order rejected: projected ECR {projected_ecr:.2%} "
                f"would exceed threshold {self.ecr_threshold:.2%}"
            )
            return False
        
        return True
    
    def _generate_rebalancing_order(self, price_data: PriceData) -> Optional[OrderSignal]:
        """Generate a market order to rebalance position."""
        if not self.enable_rebalancing:
            return None
        
        up = self.up_position.shares
        down = self.down_position.shares
        
        if up == 0 and down == 0:
            return None
        
        # Determine underweight side
        if up < down:
            underweight_side = "up"
            deficit = down - up
            market_price = price_data.up_price
        else:
            underweight_side = "down"
            deficit = up - down
            market_price = price_data.down_price
        
        # Calculate shares needed to reach target balance (0.85 ratio)
        target_ratio = 0.85
        max_shares = max(up, down)
        target_min = max_shares * target_ratio
        current_min = min(up, down)
        shares_needed = target_min - current_min
        
        if shares_needed <= 0:
            return None
        
        # Use current market price (essentially a market order)
        # Add small buffer to ensure fill
        order_price = min(market_price * 1.01, 0.99)
        
        logger.info(
            f"[{self.name}] REBALANCING: Buying {shares_needed:.1f} {underweight_side} shares "
            f"at ~${order_price:.3f} to improve balance from {self.balance_ratio:.2%}"
        )
        
        return self._create_signal(underweight_side, order_price, shares_needed * order_price)

    def _calculate_limit_prices(self, up_price: float, down_price: float) -> Tuple[float, float]:
        """Calculate limit prices based on target cost."""
        price_sum = up_price + down_price

        if price_sum <= self.target_cost:
            return up_price, down_price

        scale = self.target_cost / price_sum
        up_limit = max(0.01, up_price * scale)
        down_limit = max(0.01, down_price * scale)

        return up_limit, down_limit

    def _should_place_order(self, side: str, limit_price: float) -> bool:
        """Check if order should be placed."""
        if limit_price <= 0:
            return False

        market_price = self.current_up_price if side == "up" else self.current_down_price
        if limit_price > market_price:
            return False

        # Check position balance
        up = self.up_position.shares
        down = self.down_position.shares
        max_shares = max(up, down)
        min_shares = min(up, down)

        if max_shares > 0 and (max_shares - min_shares) / max_shares > 0.1:
            # Imbalanced: only allow lagging side
            if side == "up" and up >= down:
                return False
            if side == "down" and down >= up:
                return False

        return True

    def _create_signal(self, side: str, price: float, cost: float) -> OrderSignal:
        """Create an order signal."""
        token_type = TokenType.YES if side == "up" else TokenType.NO
        trade_side = TradeSide.BUY
        size = cost / price

        return OrderSignal(
            side=trade_side,
            token_type=token_type,
            target_price=price,
            size=size,
        )

    def on_market_end(self, market_id: str, winner: Optional[str]) -> Tuple[float, float, str]:
        """Handle market settlement."""
        up_shares = self.up_position.shares
        down_shares = self.down_position.shares
        total_cost = self.up_position.cost + self.down_position.cost

        if up_shares == 0 and down_shares == 0:
            return 0.0, 0.0, "empty"

        if winner == "up" or winner == "yes":
            final_value = up_shares
            outcome = "up"
        elif winner == "down" or winner == "no":
            final_value = down_shares
            outcome = "down"
        else:
            final_value = (up_shares + down_shares) / 2
            outcome = "unknown"

        pnl = final_value - total_cost

        logger.info(
            f"[{self.name}] Settlement: {outcome} | "
            f"UP {up_shares:.1f}, DOWN {down_shares:.1f} | "
            f"Cost ${total_cost:.2f}, Value ${final_value:.2f}, PnL ${pnl:.2f}"
        )

        return total_cost, pnl, outcome

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        up = self.up_position.shares
        down = self.down_position.shares
        total_cost = self.up_position.cost + self.down_position.cost

        eff_cost = self.effective_cost_rate
        if eff_cost == float("inf"):
            eff_cost = 999.99

        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "type": self.strategy_type,
            "position_size": self.position_size,
            "params": {
                "target_cost": self.target_cost,
                "batch_size": self.batch_size,
                "ecr_threshold": self.ecr_threshold,
                "balance_threshold": self.balance_threshold,
                "enable_ecr_stoploss": self.enable_ecr_stoploss,
                "enable_rebalancing": self.enable_rebalancing,
            },
            "up_position": {
                "shares": up,
                "cost": self.up_position.cost,
                "avg_price": self.up_position.avg_price,
            },
            "down_position": {
                "shares": down,
                "cost": self.down_position.cost,
                "avg_price": self.down_position.avg_price,
            },
            "effective_cost_rate": eff_cost,
            "balance_ratio": self.balance_ratio,
            "hedged_position": self.hedged_position,
            "market_phase": self.get_market_phase(),
            "lagging_side": self.get_lagging_side(),
            # Risk metrics
            "risk_state": self.risk_state,
            "ecr_violations": self._ecr_violations,
            "rebalancing_events": len(self._rebalancing_events),
            "is_position_imbalanced": self.is_position_imbalanced(),
        }
