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


class TrendDetector:
    """
    Detects price trends using momentum calculation.
    
    Momentum = (current_price - price_N_ago) / price_N_ago
    Confidence = min(1.0, abs(momentum) / max_momentum_threshold)
    """
    
    def __init__(
        self,
        window_size: int = 5,
        max_momentum_threshold: float = 0.10,
        trend_stop_threshold: float = 0.70,
    ):
        """
        Initialize trend detector.
        
        Args:
            window_size: Number of price updates for momentum calculation
            max_momentum_threshold: Momentum value for 100% confidence
            trend_stop_threshold: Confidence level to stop losing-side orders
        """
        self.window_size = window_size
        self.max_momentum_threshold = max_momentum_threshold
        self.trend_stop_threshold = trend_stop_threshold
        
        # Price history buffers
        self._up_prices: List[float] = []
        self._down_prices: List[float] = []
    
    def update(self, up_price: float, down_price: float) -> None:
        """Add new price point to history."""
        self._up_prices.append(up_price)
        self._down_prices.append(down_price)
        
        # Keep only window_size + 1 prices (for calculating momentum)
        max_len = self.window_size + 1
        if len(self._up_prices) > max_len:
            self._up_prices = self._up_prices[-max_len:]
        if len(self._down_prices) > max_len:
            self._down_prices = self._down_prices[-max_len:]
    
    def get_momentum(self, side: str) -> float:
        """
        Calculate momentum for a side.
        
        Returns:
            Momentum value (positive = price increasing, negative = decreasing).
            Returns 0.0 if insufficient price history.
        """
        prices = self._up_prices if side == "up" else self._down_prices
        
        if len(prices) <= self.window_size:
            return 0.0
        
        price_old = prices[-(self.window_size + 1)]
        price_new = prices[-1]
        
        if price_old <= 0:
            return 0.0
        
        return (price_new - price_old) / price_old
    
    def get_trend(self) -> Tuple[str, float]:
        """
        Get current trend direction and confidence.
        
        Returns:
            Tuple of (trend_side, confidence):
            - trend_side: "up" if UP prices rising, "down" if DOWN prices rising, "neutral"
            - confidence: 0.0 to 1.0 based on momentum strength
        """
        up_momentum = self.get_momentum("up")
        down_momentum = self.get_momentum("down")
        
        # Use UP side momentum as primary indicator
        # (if UP price rising, market expects UP to win)
        momentum = up_momentum
        
        if abs(momentum) < 0.001:
            return "neutral", 0.0
        
        trend_side = "up" if momentum > 0 else "down"
        confidence = min(1.0, abs(momentum) / self.max_momentum_threshold)
        
        return trend_side, confidence
    
    def should_block_order(self, side: str) -> bool:
        """
        Check if order should be blocked due to strong trend.
        
        Args:
            side: Order side ('up' or 'down')
            
        Returns:
            True if order should be blocked (going against strong trend)
        """
        trend_side, confidence = self.get_trend()
        
        if confidence < self.trend_stop_threshold:
            return False
        
        # Block if order is opposite to trend
        # e.g., if trend is "up" (UP prices rising), block "down" orders
        return side != trend_side
    
    def reset(self) -> None:
        """Clear price history."""
        self._up_prices.clear()
        self._down_prices.clear()


@register_strategy("position-arbitrage")
class PositionArbitrageStrategy(BaseStrategy):
    """
    Position Arbitrage / Market Maker Strategy (Optimized).

    Buys both UP and DOWN tokens below market price to achieve
    a total cost < $1, guaranteeing profit regardless of outcome.
    
    Optimization Features:
        - ECR Prediction: Rejects orders that would worsen ECR beyond tolerance
        - Trend Detection: Detects one-sided trends and blocks losing-side orders
        - Urgency Pricing: Dynamically adjusts limit prices based on time and imbalance
        - Dynamic Order Sizing: Scales order size up to 5x for lagging side
        - Enhanced Rebalancing: Market orders for severe imbalance (balance < 50%)

    Core Parameters:
        target_cost: Target cost rate (default 0.98 = 98%)
        batch_size: Order batch size in dollars (default 5.0)
        order_timeout: Order timeout in seconds (default 60)
        phase1_end: End of phase 1 in seconds (default 300)
        phase2_end: End of phase 2 in seconds (default 600)
        low_prob_threshold: Don't buy tokens below this price (default 0.05)
        
    Risk Control Parameters:
        ecr_threshold: ECR stop-loss threshold (default 1.05 = 105%)
        balance_threshold: Position balance threshold for warning (default 0.70)
        enable_ecr_stoploss: Enable ECR stop-loss mechanism (default True)
        enable_rebalancing: Enable market order rebalancing (default True)
        rebalancing_cooldown: Cooldown between rebalancing orders (default 30s)
        severe_imbalance_threshold: Balance ratio to trigger market orders (default 0.50)
        market_order_size_cap: Max market order as fraction of position (default 0.10)
        
    Trend Detection Parameters:
        enable_trend_detection: Enable trend detection filter (default True)
        momentum_window: Number of ticks for momentum calculation (default 5)
        trend_stop_threshold: Confidence level to stop losing-side orders (default 0.70)
        max_momentum_threshold: Momentum value for 100% confidence (default 0.10)
        
    Urgency Pricing Parameters:
        enable_urgency_pricing: Enable urgency-based limit pricing (default True)
        urgency_weight: Weight for imbalance vs time urgency (default 1.5)
        urgency_price_factor: Max offset reduction factor (default 0.5)
        market_duration: Total market duration in seconds (default 900)
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
        self.enable_rebalancing = self.params.get("enable_rebalancing", True)  # Changed default to True
        self.rebalancing_cooldown = self.params.get("rebalancing_cooldown", 30)  # Reduced from 60s to 30s
        
        # Trend detection parameters
        self.enable_trend_detection = self.params.get("enable_trend_detection", True)
        self.momentum_window = self.params.get("momentum_window", 5)
        self.trend_stop_threshold = self.params.get("trend_stop_threshold", 0.70)
        self.max_momentum_threshold = self.params.get("max_momentum_threshold", 0.10)
        
        # Urgency pricing parameters
        self.enable_urgency_pricing = self.params.get("enable_urgency_pricing", True)
        self.urgency_weight = self.params.get("urgency_weight", 1.5)
        self.urgency_price_factor = self.params.get("urgency_price_factor", 0.5)
        self.market_duration = self.params.get("market_duration", 900)  # 15 minutes
        
        # Enhanced rebalancing parameters
        self.severe_imbalance_threshold = self.params.get("severe_imbalance_threshold", 0.50)
        self.market_order_size_cap = self.params.get("market_order_size_cap", 0.10)

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
        
        # Trend detection
        self._trend_detector = TrendDetector(
            window_size=self.momentum_window,
            max_momentum_threshold=self.max_momentum_threshold,
            trend_stop_threshold=self.trend_stop_threshold,
        )

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

    def _predict_effective_cost_rate(self, side: str, price: float, order_cost: Optional[float] = None) -> float:
        """
        Predict effective cost rate after placing an order.
        
        This is the core ECR protection mechanism from the optimized strategy.
        
        Args:
            side: Order direction ('up' or 'down')
            price: Expected fill price
            order_cost: Order amount in dollars (uses batch_size if None)
            
        Returns:
            Predicted effective cost rate after the order fills.
            Returns float('inf') if no hedged position would exist.
        """
        if order_cost is None:
            order_cost = self.batch_size

        up = self.up_position.shares
        down = self.down_position.shares
        cost = self.up_position.cost + self.down_position.cost

        # Predict state after order
        new_shares = order_cost / price
        if side == "up":
            new_up = up + new_shares
            new_down = down
        else:
            new_up = up
            new_down = down + new_shares
        new_cost = cost + order_cost

        # Calculate new effective cost rate
        new_min = min(new_up, new_down)
        if new_min <= 0:
            return float("inf")
        return new_cost / new_min

    def _predict_worst_case_pnl(self, side: str, price: float, order_cost: float) -> float:
        """
        Predict worst-case PnL after placing an order.
        
        Worst case is min(UP wins PnL, DOWN wins PnL).
        Only >= 0 guarantees no loss regardless of outcome.
        
        Args:
            side: Order direction ('up' or 'down')
            price: Expected fill price
            order_cost: Order amount in dollars
            
        Returns:
            Predicted worst-case PnL after the order fills.
        """
        up = self.up_position.shares
        down = self.down_position.shares
        total_cost = self.up_position.cost + self.down_position.cost

        new_shares = order_cost / price
        if side == "up":
            new_up = up + new_shares
            new_down = down
        else:
            new_up = up
            new_down = down + new_shares
        new_cost = total_cost + order_cost

        if new_up == 0 and new_down == 0:
            return 0.0

        up_win_pnl = new_up - new_cost
        down_win_pnl = new_down - new_cost
        return min(up_win_pnl, down_win_pnl)

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

    def _calculate_time_urgency(self) -> float:
        """
        Calculate time-based urgency factor.
        
        Returns:
            Urgency value from 0.0 (start) to 1.0 (end of market).
        """
        if self.market_start_time is None:
            return 0.0
        
        elapsed = (datetime.now() - self.market_start_time).total_seconds()
        return min(1.0, elapsed / self.market_duration)
    
    def _calculate_imbalance_urgency(self) -> float:
        """
        Calculate imbalance-based urgency factor.
        
        Returns:
            Urgency value from 0.0 (balanced) to 1.0 (fully imbalanced).
        """
        return 1.0 - self.balance_ratio
    
    def _calculate_combined_urgency(self) -> float:
        """
        Calculate combined urgency score from time and imbalance.
        
        Returns:
            Combined urgency from 0.0 to 1.0.
        """
        time_urgency = self._calculate_time_urgency()
        imbalance_urgency = self._calculate_imbalance_urgency()
        
        # Imbalance matters more (weight it higher)
        combined = max(time_urgency, imbalance_urgency * self.urgency_weight)
        return min(1.0, combined)

    def _calculate_dynamic_order_cost(self, side: str, limit_price: float, available: float) -> float:
        """
        Calculate dynamic order cost for position rebalancing.
        
        For lagging side when imbalanced: scale up to 5x batch_size to accelerate rebalancing.
        For leading side or balanced: use standard batch_size.
        
        Args:
            side: Order direction ('up' or 'down')
            limit_price: Limit price for the order
            available: Available budget
            
        Returns:
            Order cost in dollars
        """
        up = self.up_position.shares
        down = self.down_position.shares
        
        # Check if position is imbalanced
        gap_shares = abs(up - down)
        max_shares = max(up, down)
        
        is_imbalanced = max_shares > 0 and gap_shares / max_shares > 0.1
        is_lagging = (side == "up" and up < down) or (side == "down" and down < up)
        
        if is_imbalanced and is_lagging and gap_shares > 0:
            # Calculate cost to fill the gap
            desired_cost = gap_shares * limit_price
            # Scale factor: min(5.0, desired_cost / batch_size)
            scale_factor = min(5.0, desired_cost / self.batch_size)
            order_cost = self.batch_size * scale_factor
            # Cap at available budget
            order_cost = min(available, order_cost)
            # Ensure at least batch_size
            order_cost = max(self.batch_size, order_cost)
            return order_cost
        
        # Standard batch_size for balanced or leading side
        return min(available, self.batch_size)

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
        # Reset trend detector
        self._trend_detector.reset()

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
        
        # Update trend detector with new prices
        if self.enable_trend_detection:
            self._trend_detector.update(price_data.up_price, price_data.down_price)
        
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
        
        # Check for rebalancing opportunity (use severe threshold for market orders)
        severe_imbalance = self.balance_ratio < self.severe_imbalance_threshold
        if severe_imbalance and self.can_rebalance():
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
        # _should_place_order now handles phase 3 logic internally
        if phase == 3:
            if lagging_side == "up":
                order_cost = self._calculate_dynamic_order_cost("up", up_limit, available)
                if self._should_place_order("up", up_limit, order_cost):
                    signals.append(self._create_signal("up", up_limit, order_cost))
            elif lagging_side == "down":
                order_cost = self._calculate_dynamic_order_cost("down", down_limit, available)
                if self._should_place_order("down", down_limit, order_cost):
                    signals.append(self._create_signal("down", down_limit, order_cost))
        else:
            # Phase 1-2: Normal operation, prioritize lagging side
            # Use dynamic order sizing for faster rebalancing
            if lagging_side == "up" or lagging_side == "balanced":
                order_cost = self._calculate_dynamic_order_cost("up", up_limit, available)
                if self._should_place_order("up", up_limit, order_cost):
                    signals.append(self._create_signal("up", up_limit, order_cost))
                    available -= order_cost

            if available >= self.batch_size:
                if lagging_side == "down" or lagging_side == "balanced":
                    order_cost = self._calculate_dynamic_order_cost("down", down_limit, available)
                    if self._should_place_order("down", down_limit, order_cost):
                        signals.append(self._create_signal("down", down_limit, order_cost))

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
        """
        Generate a market order to rebalance severely imbalanced position.
        
        Uses market_order_size_cap to limit order size and prevent excessive slippage.
        """
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
        
        # Apply market order size cap (fraction of max position value)
        total_cost = self.up_position.cost + self.down_position.cost
        max_order_cost = total_cost * self.market_order_size_cap
        
        # Use current market price (essentially a market order)
        # Add small buffer to ensure fill
        order_price = min(market_price * 1.01, 0.99)
        
        # Calculate order cost and cap if necessary
        order_cost = shares_needed * order_price
        if order_cost > max_order_cost and max_order_cost > 0:
            capped_shares = max_order_cost / order_price
            logger.info(
                f"[{self.name}] REBALANCING: Capping order from {shares_needed:.1f} to {capped_shares:.1f} shares "
                f"(${order_cost:.2f} → ${max_order_cost:.2f}) due to size cap {self.market_order_size_cap:.0%}"
            )
            shares_needed = capped_shares
            order_cost = max_order_cost
        
        logger.info(
            f"[{self.name}] REBALANCING: Buying {shares_needed:.1f} {underweight_side} shares "
            f"at ~${order_price:.3f} to improve balance from {self.balance_ratio:.2%}"
        )
        
        return self._create_signal(underweight_side, order_price, order_cost)

    def _calculate_limit_prices(self, up_price: float, down_price: float) -> Tuple[float, float]:
        """
        Calculate limit prices based on target cost with urgency adjustment.
        
        Urgency-based pricing: As urgency increases, limit prices move closer
        to market prices to increase fill probability.
        """
        price_sum = up_price + down_price

        if price_sum <= self.target_cost:
            return up_price, down_price

        # Calculate base offset
        base_scale = self.target_cost / price_sum
        base_up_limit = up_price * base_scale
        base_down_limit = down_price * base_scale
        
        # Apply urgency adjustment if enabled
        if self.enable_urgency_pricing:
            urgency = self._calculate_combined_urgency()
            
            if urgency > 0:
                # Calculate offset from market price
                up_offset = up_price - base_up_limit
                down_offset = down_price - base_down_limit
                
                # Reduce offset based on urgency (up to urgency_price_factor reduction)
                offset_reduction = urgency * self.urgency_price_factor
                
                up_limit = base_up_limit + up_offset * offset_reduction
                down_limit = base_down_limit + down_offset * offset_reduction
                
                # Lagging side boost: move even closer to market price
                lagging_side = self.get_lagging_side()
                if urgency > 0.7 and lagging_side != "balanced":
                    if lagging_side == "up":
                        # Boost up_limit toward market price
                        up_limit = min(up_price * 0.995, up_limit + (up_price - up_limit) * 0.5)
                    else:
                        # Boost down_limit toward market price
                        down_limit = min(down_price * 0.995, down_limit + (down_price - down_limit) * 0.5)
                
                return max(0.01, up_limit), max(0.01, down_limit)
        
        return max(0.01, base_up_limit), max(0.01, base_down_limit)

    def _should_place_order(self, side: str, limit_price: float, order_cost: Optional[float] = None) -> bool:
        """
        Check if order should be placed with comprehensive ECR protection.
        
        Core rules:
        - Must ensure ECR improves or stays within tolerance after order
        - Phase 3: Focus on lagging side, avoid low probability tokens
        
        Args:
            side: Order direction ('up' or 'down')
            limit_price: Limit price for the order
            order_cost: Order cost (uses batch_size if None)
            
        Returns:
            True if order should be placed, False otherwise.
        """
        if order_cost is None:
            order_cost = self.batch_size
            
        # Basic validation
        if limit_price <= 0 or order_cost <= 0:
            return False
        market_price = self.current_up_price if side == "up" else self.current_down_price
        if market_price <= 0:
            return False

        # Core condition: limit price must be <= market price
        if limit_price > market_price:
            return False

        # === Trend Detection Filter ===
        # Block orders going against a strong trend to avoid accumulating losing-side positions
        if self.enable_trend_detection:
            if self._trend_detector.should_block_order(side):
                trend_side, confidence = self._trend_detector.get_trend()
                logger.debug(
                    f"[{self.name}] Order blocked by trend: {side.upper()} order blocked "
                    f"(trend={trend_side}, confidence={confidence:.0%})"
                )
                return False

        up = self.up_position.shares
        down = self.down_position.shares
        phase = self.get_market_phase()
        lagging_side = self.get_lagging_side()

        # === Core ECR Protection ===
        current_ecr = self.effective_cost_rate
        predicted_ecr = self._predict_effective_cost_rate(side, limit_price, order_cost)

        # Initial state (up=0, down=0): allow building position
        if up == 0 and down == 0:
            # Initial order may have inf ECR (single-sided), that's normal
            # Only reject if predicted ECR is finite and >= 100%
            if predicted_ecr != float("inf") and predicted_ecr >= 1.0:
                logger.debug(
                    f"[{self.name}] Order rejected {side.upper()}@{limit_price:.1%}(${order_cost:.2f}): "
                    f"initial order would cause ECR >= 100% (predicted {predicted_ecr:.1%})"
                )
                return False
            return True

        # Special case: current ECR is infinite (single-sided position)
        if current_ecr == float("inf"):
            # If order would keep ECR at infinity (continuing single-sided), reject
            if predicted_ecr == float("inf"):
                logger.debug(
                    f"[{self.name}] Order rejected {side.upper()}@{limit_price:.1%}(${order_cost:.2f}): "
                    f"would continue single-sided position (ECR stays infinite)"
                )
                return False
            # Allow slightly high ECR (up to 105%) when transitioning from single-sided
            # because getting to balanced state is priority
            if predicted_ecr >= self.ecr_threshold:
                logger.debug(
                    f"[{self.name}] Order rejected {side.upper()}@{limit_price:.1%}(${order_cost:.2f}): "
                    f"ECR would exceed {self.ecr_threshold:.0%} (predicted {predicted_ecr:.1%})"
                )
                return False
        else:
            # Current ECR is finite - check if order would worsen it
            # Allow ECR to worsen by at most 1% (tolerance)
            ecr_tolerance = 0.01
            if predicted_ecr > current_ecr + ecr_tolerance:
                logger.debug(
                    f"[{self.name}] Order rejected {side.upper()}@{limit_price:.1%}(${order_cost:.2f}): "
                    f"ECR would worsen beyond tolerance {current_ecr:.1%} → {predicted_ecr:.1%}"
                )
                return False

            # Reject if ECR would exceed 100% (high risk)
            if predicted_ecr >= 1.0:
                logger.debug(
                    f"[{self.name}] Order rejected {side.upper()}@{limit_price:.1%}(${order_cost:.2f}): "
                    f"ECR would exceed 100% (predicted {predicted_ecr:.1%})"
                )
                return False

        # === Phase 3 special logic ===
        if phase == 3:
            # Don't buy low probability side (price < threshold)
            if market_price < self.low_prob_threshold:
                logger.debug(
                    f"[{self.name}] Phase 3: rejecting low probability {side.upper()} (price {market_price:.1%})"
                )
                return False

            # Focus on lagging side only
            if lagging_side != "balanced" and side != lagging_side:
                logger.debug(
                    f"[{self.name}] Phase 3: stopping leading side {side.upper()}, focusing on {lagging_side.upper()}"
                )
                return False

            return True

        # === Phase 1-2 logic ===
        max_shares = max(up, down)
        min_shares = min(up, down)

        # When imbalanced (>10%), only allow lagging side
        if max_shares > 0 and (max_shares - min_shares) / max_shares > 0.1:
            if side == "up" and up < down:
                return True  # UP is lagging
            elif side == "down" and down < up:
                return True  # DOWN is lagging
            else:
                return False  # Leading side paused

        # Balanced: allow both sides
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
        """Get current strategy status with all metrics."""
        up = self.up_position.shares
        down = self.down_position.shares
        total_cost = self.up_position.cost + self.down_position.cost

        eff_cost = self.effective_cost_rate
        if eff_cost == float("inf"):
            eff_cost = 999.99

        # Get trend info
        trend_side, trend_confidence = self._trend_detector.get_trend()
        
        # Calculate urgency
        time_urgency = self._calculate_time_urgency()
        imbalance_urgency = self._calculate_imbalance_urgency()
        combined_urgency = self._calculate_combined_urgency()

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
                "enable_trend_detection": self.enable_trend_detection,
                "enable_urgency_pricing": self.enable_urgency_pricing,
                "severe_imbalance_threshold": self.severe_imbalance_threshold,
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
            # Trend detection metrics
            "trend": {
                "side": trend_side,
                "confidence": trend_confidence,
                "up_momentum": self._trend_detector.get_momentum("up"),
                "down_momentum": self._trend_detector.get_momentum("down"),
            },
            # Urgency metrics
            "urgency": {
                "time": time_urgency,
                "imbalance": imbalance_urgency,
                "combined": combined_urgency,
            },
        }
