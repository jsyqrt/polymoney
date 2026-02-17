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
    """
    Unified order tracking used by both Strategy and Runner.
    
    This is the single source of truth for pending orders. Both the strategy
    (for ECR prediction and budget) and the runner (for fill simulation and
    timeout) operate on this same object.
    """

    order_id: str
    side: str  # 'up' or 'down'
    price: float
    shares: float
    cost: float = 0.0
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.now)
    created_market_price: float = 0.0
    # Runner-side fields for fill simulation
    timestamp: float = 0.0  # Unix timestamp for timeout tracking (set by runner)
    is_taker: bool = False  # Whether this crosses the spread as a taker


class TrendDetector:
    """
    Detects price trends using time-based momentum calculation.
    
    Uses a fixed time window (in seconds) rather than event count,
    ensuring consistent trend detection regardless of WebSocket update frequency.
    
    Momentum = (current_price - price_at_window_start) / price_at_window_start
    Confidence = min(1.0, abs(momentum) / max_momentum_threshold)
    """
    
    def __init__(
        self,
        window_seconds: float = 30.0,
        max_momentum_threshold: float = 0.10,
        trend_stop_threshold: float = 0.70,
    ):
        """
        Initialize trend detector.
        
        Args:
            window_seconds: Time window in seconds for momentum calculation
            max_momentum_threshold: Momentum value for 100% confidence
            trend_stop_threshold: Confidence level to stop losing-side orders
        """
        self.window_seconds = window_seconds
        self.max_momentum_threshold = max_momentum_threshold
        self.trend_stop_threshold = trend_stop_threshold
        
        # Price history buffers: list of (timestamp, price)
        self._up_history: List[Tuple[float, float]] = []
        self._down_history: List[Tuple[float, float]] = []
    
    def update(self, up_price: float, down_price: float, timestamp: Optional[float] = None) -> None:
        """
        Add new price point to history.
        
        Args:
            up_price: Current UP token price
            down_price: Current DOWN token price
            timestamp: Unix timestamp (uses time.time() if None)
        """
        import time as _time
        ts = timestamp if timestamp is not None else _time.time()
        
        self._up_history.append((ts, up_price))
        self._down_history.append((ts, down_price))
        
        # Prune entries older than 2x window to keep memory bounded
        cutoff = ts - self.window_seconds * 2
        self._up_history = [(t, p) for t, p in self._up_history if t >= cutoff]
        self._down_history = [(t, p) for t, p in self._down_history if t >= cutoff]
    
    def get_momentum(self, side: str) -> float:
        """
        Calculate momentum for a side over the time window.
        
        Returns:
            Momentum value (positive = price increasing, negative = decreasing).
            Returns 0.0 if insufficient price history.
        """
        history = self._up_history if side == "up" else self._down_history
        
        if len(history) < 2:
            return 0.0
        
        current_ts, current_price = history[-1]
        window_start = current_ts - self.window_seconds
        
        # Find the price closest to window_start
        old_price = None
        for ts, price in history:
            if ts <= window_start:
                old_price = price
            else:
                break
        
        # If no entry before window_start, use the oldest available
        if old_price is None:
            oldest_ts = history[0][0]
            # Need at least half the window of data
            if current_ts - oldest_ts < self.window_seconds * 0.5:
                return 0.0
            old_price = history[0][1]
        
        if old_price <= 0:
            return 0.0
        
        return (current_price - old_price) / old_price
    
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
    
    def reset(self) -> None:
        """Clear price history."""
        self._up_history.clear()
        self._down_history.clear()


@register_strategy("position-arbitrage")
class PositionArbitrageStrategy(BaseStrategy):
    """
    Position Arbitrage / Market Maker Strategy.

    Buys both UP and DOWN tokens below market price via limit orders so that
    the total cost per hedged pair < $1, guaranteeing profit at settlement.

    Key Features:
        - ECR Prediction: Pre-calculates post-order ECR and rejects orders that worsen it
        - Trend-following Pricing: Asymmetric limit prices in skewed markets
        - Urgency Pricing: Tightens limits as time or imbalance urgency grows
        - Proportional Sizing: Allocates order cost by price ratio for balanced shares
        - Market Rebalancing: Market orders for severe position imbalance

    Core Parameters:
        target_cost: UP_limit + DOWN_limit target (default 0.96)
        batch_ratio: Order size = position_size × batch_ratio (default 0.001)
        batch_size: Explicit override for batch_ratio auto-calculation
        min_order_shares: Polymarket minimum order size in shares (default 5)
        order_timeout: Order timeout in seconds (default 60)
        phase1_end / phase2_end: Phase boundaries in seconds (default 300 / 600)
        low_prob_threshold: Skip tokens below this price (default 0.05)
        max_orders_per_tick: Orders generated per price update (default 2)

    Risk Control Parameters:
        ecr_threshold: ECR threshold for minority-side-only mode (default 1.05)
        balance_threshold: Balance ratio warning threshold (default 0.70)
        max_skew_threshold: Suspend new orders above this skew (default 0.85)
        trend_patience_threshold: Asymmetric pricing activation (default 0.65)
        severe_imbalance_threshold: Market order rebalancing trigger (default 0.50)

    See config/strategy_defaults.yaml for full parameter reference.
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
        self.target_cost = self.params.get("target_cost", 0.96)
        
        # Adaptive target_cost: dynamically adjust discount based on orderbook spread.
        # High liquidity (tight spread) → smaller discount → higher fill rate.
        # Low liquidity (wide spread) → larger discount → safer fills.
        # Bounds: [adaptive_target_min, adaptive_target_max]
        self.enable_adaptive_target = self.params.get("enable_adaptive_target", True)
        self.adaptive_target_min = self.params.get("adaptive_target_min", 0.94)
        self.adaptive_target_max = self.params.get("adaptive_target_max", 0.98)
        self._last_up_spread: float = 0.0
        self._last_down_spread: float = 0.0
        
        # Batch size: auto-derived from position_size if not explicitly set.
        # batch_ratio (default 0.001 = 1/1000) determines granularity.
        # Smaller batch = better averaging, more precise ECR convergence.
        self.batch_ratio = self.params.get("batch_ratio", 0.001)
        explicit_batch = self.params.get("batch_size")
        if explicit_batch is not None:
            self.batch_size = explicit_batch
        else:
            self.batch_size = max(position_size * self.batch_ratio, 0.10)
        
        # Minimum order size in shares.  Polymarket CLOB enforces a per-market
        # minimum (typically 5-15 shares).  Default 5 matches the common floor.
        # The strategy converts this to a dollar minimum at order time.
        self.min_order_shares = self.params.get("min_order_shares", 5)
        
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
        self.momentum_window = self.params.get("momentum_window", 5)  # Legacy (event-count), kept for config compat
        self.momentum_window_seconds = self.params.get("momentum_window_seconds", 30.0)  # Time-based window
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
        
        # Order pacing parameters
        self.max_orders_per_tick = self.params.get("max_orders_per_tick", 2)
        
        # Market skew filter: reject entering markets where one side dominates
        # If max(up_price, down_price) > max_skew_threshold, skip new orders
        # This prevents one-sided position building in extremely skewed markets
        self.max_skew_threshold = self.params.get("max_skew_threshold", 0.85)
        self._skew_rejection_logged = False  # avoid log spam
        
        # Trend patience mode: when market is moderately skewed (one side > this threshold
        # but below max_skew_threshold), only buy the CHEAP side. The expensive side orders
        # are skipped entirely — we accumulate the cheap token and wait for a pullback to
        # buy the expensive side at a better price. This replaces the old approach of
        # refusing to trade in skewed markets entirely.
        self.trend_patience_threshold = self.params.get("trend_patience_threshold", 0.65)
        self._trend_patience_logged = False  # avoid log spam

        # Internal position tracking
        self.up_position = InternalPosition()
        self.down_position = InternalPosition()

        # Order tracking
        self.pending_orders: List[LimitOrder] = []
        self._order_counter = 0

        # Market state
        self.current_up_price: Optional[float] = None  # None = no data yet
        self.current_down_price: Optional[float] = None  # None = no data yet
        self.market_start_time: Optional[datetime] = None
        self.market_settlement_time: Optional[float] = None  # Unix timestamp of market settlement
        
        # Risk state tracking
        self._risk_state = "normal"  # "normal", "warning", "limited"
        self._current_phase = 0  # Track phase transitions for logging
        self._ecr_violations = 0
        self._ecr_recovery_side: Optional[str] = None  # "up"/"down" when in Tier 1/2 recovery
        self._last_rebalancing_time: Optional[datetime] = None
        self._last_rebalancing_rejection_time: Optional[datetime] = None
        self._rebalancing_events: List[Dict[str, Any]] = []
        
        # Trend detection (time-based)
        self._trend_detector = TrendDetector(
            window_seconds=self.momentum_window_seconds,
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
        
        Includes pending orders in calculation for accurate ECR prediction.
        
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

        # Include both filled positions AND pending orders
        pending_up_shares = sum(o.shares for o in self.pending_orders if o.side == "up")
        pending_down_shares = sum(o.shares for o in self.pending_orders if o.side == "down")
        pending_cost = sum(o.cost for o in self.pending_orders)
        
        up = self.up_position.shares + pending_up_shares
        down = self.down_position.shares + pending_down_shares
        cost = self.up_position.cost + self.down_position.cost + pending_cost

        # Predict state after this order
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

    def is_position_imbalanced(self) -> bool:
        """Check if position is severely imbalanced."""
        return self.balance_ratio < self.balance_threshold
    
    def can_rebalance(self) -> bool:
        """Check if rebalancing is allowed (not in cooldown after execution or rejection)."""
        if not self.enable_rebalancing:
            return False
        
        now = datetime.now()
        
        # Check cooldown after successful execution
        if self._last_rebalancing_time is not None:
            elapsed = (now - self._last_rebalancing_time).total_seconds()
            if elapsed < self.rebalancing_cooldown:
                return False
        
        # Check cooldown after rejection (prevent log spam)
        if self._last_rebalancing_rejection_time is not None:
            elapsed = (now - self._last_rebalancing_rejection_time).total_seconds()
            if elapsed < self.rebalancing_cooldown:
                return False
        
        return True

    def _get_phase1_ecr_limit(self) -> float:
        """
        Get progressive ECR tolerance for Phase 1 with skew awareness.
        
        Base behavior: decays from 1.5 (start) to 1.1 (end of Phase 1).
        
        Skew adjustment: in highly skewed markets, one-sided fills are more
        likely, so the ECR cap is tightened proportionally to skew intensity.
        At 75% skew (max_entry_skew), the cap is reduced by 20% (e.g. 1.5→1.3
        at start, 1.1→1.02 at end).  This prevents entering Phase 2 with a
        dangerously high ECR that's hard to recover from.
        
        Returns:
            Maximum allowed ECR for Phase 1 orders.
        """
        if self.market_start_time is None:
            return 1.5
        
        elapsed = (datetime.now() - self.market_start_time).total_seconds()
        progress = min(1.0, elapsed / self.phase1_end)  # 0 → 1 over Phase 1
        base_limit = 1.5 - 0.4 * progress  # 1.5 → 1.1
        
        # Skew adjustment: tighten ECR cap in skewed markets
        if self.current_up_price is not None and self.current_down_price is not None:
            max_price = max(self.current_up_price, self.current_down_price)
            # Skew intensity: 0 at 50% (balanced), 1 at max_entry_skew (75%)
            skew_start = 0.50
            skew_end = self.params.get("max_entry_skew", 0.75)
            if max_price > skew_start and skew_end > skew_start:
                skew_intensity = min(1.0, (max_price - skew_start) / (skew_end - skew_start))
                # Reduce the cap by up to 20% of (cap - 1.0)
                # At skew_intensity=1: 1.5 → 1.3 at start, 1.1 → 1.02 at end
                reduction = skew_intensity * 0.40 * (base_limit - 1.0)
                base_limit -= reduction
        
        return max(1.02, base_limit)  # Never go below 1.02 (2% margin)

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

    def update_spread_info(self, up_spread: float, down_spread: float) -> None:
        """
        Update spread information from orderbook data.
        
        Called by the simulation runner when orderbook data is available.
        Used by adaptive target_cost to adjust discount dynamically.
        
        Args:
            up_spread: Current bid-ask spread for UP token (absolute, e.g. 0.02)
            down_spread: Current bid-ask spread for DOWN token (absolute, e.g. 0.02)
        """
        if up_spread > 0:
            self._last_up_spread = up_spread
        if down_spread > 0:
            self._last_down_spread = down_spread

    def _get_adaptive_target_cost(self) -> float:
        """
        Calculate adaptive target_cost based on current orderbook spread.
        
        The intuition: spread represents the "cost of immediacy" in the market.
        Tight spread (1-2%) = liquid market, orders fill easily → small discount (0.98).
        Wide spread (5%+) = illiquid market, need wider limits → large discount (0.94).
        
        Linear interpolation between min and max based on average spread:
          spread ≤ 1% → target_max (0.98)
          spread ≥ 5% → target_min (0.94)
          
        Returns:
            Adaptive target_cost (or static target_cost if adaptive is disabled).
        """
        if not self.enable_adaptive_target:
            return self.target_cost
        
        avg_spread = 0.0
        count = 0
        if self._last_up_spread > 0:
            avg_spread += self._last_up_spread
            count += 1
        if self._last_down_spread > 0:
            avg_spread += self._last_down_spread
            count += 1
        
        if count == 0:
            # No spread data available — use static target_cost
            return self.target_cost
        
        avg_spread /= count
        
        # Map spread to target_cost:
        #   spread ≤ 1% (tight) → adaptive_target_max (e.g. 0.98)
        #   spread ≥ 5% (wide)  → adaptive_target_min (e.g. 0.94)
        spread_low = 0.01   # Tight spread threshold
        spread_high = 0.05  # Wide spread threshold
        
        if avg_spread <= spread_low:
            adaptive = self.adaptive_target_max
        elif avg_spread >= spread_high:
            adaptive = self.adaptive_target_min
        else:
            # Linear interpolation
            ratio = (avg_spread - spread_low) / (spread_high - spread_low)
            adaptive = self.adaptive_target_max - ratio * (self.adaptive_target_max - self.adaptive_target_min)
        
        return adaptive

    def _calculate_time_urgency(self) -> float:
        """
        Calculate time-based urgency factor.
        
        Uses actual market settlement time when available for accurate urgency.
        Falls back to market_duration estimate from strategy start time.
        
        Returns:
            Urgency value from 0.0 (start) to 1.0 (end of market).
        """
        if self.market_start_time is None:
            return 0.0
        
        now = datetime.now()
        
        # Use actual settlement time when available (more accurate)
        if self.market_settlement_time is not None:
            now_ts = now.timestamp()
            start_ts = self.market_start_time.timestamp()
            total_duration = self.market_settlement_time - start_ts
            if total_duration <= 0:
                return 1.0
            elapsed = now_ts - start_ts
            return min(1.0, elapsed / total_duration)
        
        # Fallback: estimate from market_duration
        elapsed = (now - self.market_start_time).total_seconds()
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

    def reset(self) -> None:
        """Reset strategy state."""
        super().reset()
        self.up_position.reset()
        self.down_position.reset()
        self.pending_orders.clear()
        self._order_counter = 0
        self.market_start_time = None
        self.market_settlement_time = None
        # Reset risk state
        self._risk_state = "normal"
        self._current_phase = 0
        self._ecr_violations = 0
        self._ecr_recovery_side = None
        self._last_rebalancing_time = None
        self._last_rebalancing_rejection_time = None
        self._rebalancing_events.clear()
        self._skew_rejection_logged = False
        self._trend_patience_logged = False
        # Reset adaptive target spread data
        self._last_up_spread = 0.0
        self._last_down_spread = 0.0
        # Reset trend detector
        self._trend_detector.reset()

    def on_market_start(self, market_id: str, market_info: Dict[str, Any]) -> None:
        """Initialize for a new market."""
        self.reset()
        self.market_start_time = datetime.now()
        # Use settlement_time from market_info if available for accurate urgency calculation
        self.market_settlement_time = market_info.get("settlement_time")
        
        # Dynamic min_order_shares: use market-specific value from API if available.
        # Polymarket enforces per-market minimums (typically 5-15 shares).
        # Falls back to the configured default (5 shares).
        api_min_order = market_info.get("min_order_size")
        if api_min_order is not None and api_min_order > 0:
            old_min = self.min_order_shares
            self.min_order_shares = api_min_order
            logger.info(
                f"[{self.name}] Market started: {market_id} "
                f"(min_order_shares: {old_min}→{api_min_order} from API)"
            )
        else:
            logger.info(f"[{self.name}] Market started: {market_id}")

    def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
        """Process price update and generate order signals."""
        # Validate price data - reject invalid prices
        if price_data.up_price <= 0 or price_data.down_price <= 0:
            logger.debug(f"[{self.name}] Invalid prices: up={price_data.up_price}, down={price_data.down_price}")
            return []
        
        # Validate price sum is approximately 1.0 (allow some tolerance)
        price_sum = price_data.up_price + price_data.down_price
        if price_sum < 0.9 or price_sum > 1.1:
            logger.debug(f"[{self.name}] Invalid price sum: {price_sum:.2f} (up={price_data.up_price:.2f}, down={price_data.down_price:.2f})")
            return []
        
        self.current_up_price = price_data.up_price
        self.current_down_price = price_data.down_price

        signals = []
        
        # Update trend detector with new prices (time-based)
        if self.enable_trend_detection:
            self._trend_detector.update(
                price_data.up_price,
                price_data.down_price,
                timestamp=price_data.timestamp.timestamp(),
            )
        
        # Update risk state
        current_risk = self.risk_state
        if current_risk != self._risk_state:
            self._risk_state = current_risk
            logger.info(f"[{self.name}] Risk state changed to: {current_risk} (ECR: {self.effective_cost_rate:.2%})")
        
        # Track phase transitions
        current_phase = self.get_market_phase()
        if current_phase != self._current_phase:
            self._current_phase = current_phase
            ecr = self.effective_cost_rate
            ecr_str = f"{ecr:.2%}" if ecr != float("inf") else "-"
            fills = self.up_position.shares + self.down_position.shares
            if current_phase == 3:
                logger.info(
                    f"[{self.name}] === PHASE 3 (settlement protection) === "
                    f"ECR={ecr_str}, fills={fills:.0f}. "
                    f"{'Profitable — locking position.' if ecr < 1.0 else 'Unprofitable — recovery-only orders.'}"
                )
            else:
                logger.info(f"[{self.name}] Phase transition → Phase {current_phase} (ECR={ecr_str}, fills={fills:.0f})")
        
        # === Simple balance rule ===
        # From first principles: the ONLY thing that matters is balanced shares
        # at a combined cost < $1 per pair.  All the complex tier/recovery logic
        # was papering over a fill-model bug (2% spread tolerance vs 4% limit
        # discount).  Now that fills work, one rule suffices:
        #
        #   If ECR > target_cost AND shares are imbalanced → minority side only
        #   Otherwise → both sides
        #
        # No tiers, no phases, no recovery reserves.  Just: buy what you need.
        phase = self.get_market_phase()
        total_cost = self.up_position.cost + self.down_position.cost
        # Activate balance rule early: after just 2 order pairs (4 fills).
        # Previous value max(batch*20, size*0.05) = $5.00 required 50+ fills
        # at $0.10 batch, creating a huge blind spot where ECR could spiral
        # (ETH hit ECR=2.71 with only $0.90 invested, balance rule never fired).
        min_cost_for_ecr = self.batch_size * 4
        
        self._ecr_recovery_side = None  # default: allow both sides
        
        if self.enable_ecr_stoploss and total_cost >= min_cost_for_ecr:
            ecr = self.effective_cost_rate
            up_shares = self.up_position.shares
            down_shares = self.down_position.shares
            
            # Use ecr_threshold (configurable, default 1.05) as the activation
            # point — it already represents "ECR level worth worrying about".
            # target_cost (0.96) is just our desired buy price, not a risk gate.
            # Also require meaningful share imbalance (5%) to avoid triggering
            # on floating-point noise when shares are nearly equal.
            max_shares = max(up_shares, down_shares)
            meaningful_imbalance = max_shares > 0 and abs(up_shares - down_shares) / max_shares > 0.05
            
            if ecr != float("inf") and ecr > self.ecr_threshold and meaningful_imbalance:
                minority_side = "up" if up_shares < down_shares else "down"
                self._ecr_recovery_side = minority_side
                if self._ecr_violations <= 3:
                    logger.info(
                        f"[{self.name}] BALANCE: ECR={ecr:.2%} > threshold={self.ecr_threshold:.0%}. "
                        f"Minority side only ({minority_side.upper()})."
                    )
                self._ecr_violations += 1
        
        # Check for rebalancing opportunity (use severe threshold for market orders)
        # Only rebalance when BOTH sides have positions - don't rebalance during initial building
        up_shares = self.up_position.shares
        down_shares = self.down_position.shares
        has_both_sides = up_shares > 0 and down_shares > 0
        severe_imbalance = self.balance_ratio < self.severe_imbalance_threshold
        
        if has_both_sides and severe_imbalance and self.can_rebalance():
            rebalance_signal = self._generate_rebalancing_order(price_data)
            if rebalance_signal:
                signals.append(rebalance_signal)
                self._last_rebalancing_time = datetime.now()
                
                # Track the rebalancing order in strategy's pending list so that
                # budget and ECR prediction account for it.  Previously omitted
                # because market orders always filled immediately; now that we use
                # aggressive limit orders, the fill may be delayed.
                rebal_side = "up" if rebalance_signal.token_type == TokenType.YES else "down"
                rebal_cost = rebalance_signal.size * rebalance_signal.target_price
                self._order_counter += 1
                self.pending_orders.append(LimitOrder(
                    order_id=f"rebal_{rebal_side}_{self._order_counter}",
                    side=rebal_side,
                    price=rebalance_signal.target_price,
                    shares=rebalance_signal.size,
                    cost=rebal_cost,
                    created_market_price=(price_data.up_price if rebal_side == "up"
                                         else price_data.down_price),
                ))
                
                # Record rebalancing event
                self._rebalancing_events.append({
                    "timestamp": datetime.now().isoformat(),
                    "side": rebal_side,
                    "size": rebalance_signal.size,
                    "price": rebalance_signal.target_price,
                    "balance_before": self.balance_ratio,
                })
                return signals  # Return just the rebalancing order

        # === Market skew guard ===
        # Prevent new limit orders in extremely skewed markets where one side dominates.
        # In such markets, only the cheap side limit orders fill, creating dangerous
        # one-sided exposure. Rebalancing (above) still runs for existing positions.
        # Uses hysteresis: activate at max_skew_threshold, deactivate at threshold - 5%
        max_price = max(price_data.up_price, price_data.down_price)
        skew_deactivate = self.max_skew_threshold - 0.05  # 5% hysteresis band
        if max_price > self.max_skew_threshold or (self._skew_rejection_logged and max_price > skew_deactivate):
            if not self._skew_rejection_logged:
                dominant_side = "UP" if price_data.up_price > price_data.down_price else "DOWN"
                logger.info(
                    f"[{self.name}] SKEW GUARD: Market too skewed ({dominant_side}={max_price:.1%}), "
                    f"threshold={self.max_skew_threshold:.1%}. Suspending new limit orders."
                )
                self._skew_rejection_logged = True
            return signals
        elif self._skew_rejection_logged:
            # Market returned below hysteresis band — resume trading
            logger.info(
                f"[{self.name}] SKEW GUARD: Market skew reduced ({max_price:.1%} < {skew_deactivate:.1%}). "
                f"Resuming limit orders."
            )
            self._skew_rejection_logged = False
        
        # Calculate limit prices
        up_limit, down_limit = self._calculate_limit_prices(
            price_data.up_price, price_data.down_price
        )

        # Check available budget — full position_size, no reserves needed
        total_cost = self.up_position.cost + self.down_position.cost
        pending_cost = sum(o.cost for o in self.pending_orders)
        available = self.position_size - total_cost - pending_cost

        if available < self.batch_size:
            logger.debug(
                f"[{self.name}] No budget: total_cost=${total_cost:.2f}, "
                f"pending_cost=${pending_cost:.2f} ({len(self.pending_orders)} orders), "
                f"available=${available:.2f} < batch=${self.batch_size:.2f}"
            )
            return signals

        phase = self.get_market_phase()
        
        # === Trend patience mode logging ===
        # When market is moderately skewed, _calculate_limit_prices uses asymmetric
        # offset: tight limit on trending side (fills easily), wide limit on declining
        # side (waits for pullback). Both sides still get orders — the pricing handles
        # which fills first.
        # Uses hysteresis: activate at threshold, deactivate at threshold - 3%
        trend_deactivate = self.trend_patience_threshold - 0.03  # 3% hysteresis band
        in_trend_zone = max_price > self.trend_patience_threshold or \
                        (self._trend_patience_logged and max_price > trend_deactivate)
        if in_trend_zone:
            if not self._trend_patience_logged:
                dominant_side = "UP" if price_data.up_price > price_data.down_price else "DOWN"
                logger.info(
                    f"[{self.name}] TREND FOLLOWING: {dominant_side} dominant ({max_price:.1%}). "
                    f"Tight limit on {dominant_side} (buy before it gets pricier), "
                    f"wide limit on other side (wait for pullback)."
                )
                self._trend_patience_logged = True
        elif self._trend_patience_logged:
            logger.info(
                f"[{self.name}] TREND FOLLOWING: Market rebalanced ({max_price:.1%} < "
                f"{trend_deactivate:.1%}). Resuming symmetric pricing."
            )
            self._trend_patience_logged = False
        
        # === LOOP-BASED ORDER CREATION ===
        # Create multiple orders per update, include pending in position calculation
        # Ported from examples/position_arbitrage.py
        
        # Count pending orders per side
        pending_up_count = sum(1 for o in self.pending_orders if o.side == "up")
        pending_down_count = sum(1 for o in self.pending_orders if o.side == "down")
        
        # Calculate total shares including pending orders
        up_shares_total = self.up_position.shares + sum(
            o.shares for o in self.pending_orders if o.side == "up"
        )
        down_shares_total = self.down_position.shares + sum(
            o.shares for o in self.pending_orders if o.side == "down"
        )
        
        # Limits
        # max_pending_per_side controls how many unfilled orders can exist per side.
        # Reduced from 3→2 to limit maximum fill asymmetry: with 2 per side,
        # at most 2 extra fills can accumulate on one side before the other
        # catches up, keeping ECR drift within ~2%.
        # Keep low to prevent "initial burst" problem: when REST orderbook data
        # doesn't match real WS prices, a burst of orders fills one-sided.
        # With 2, at most 4 orders pending initially — if some fill one-sided,
        # the damage is limited and recovery is fast.
        max_pending_per_side = 2
        max_orders_per_tick = self.max_orders_per_tick
        orders_created = 0
        
        # ECR recovery constraint: when _ecr_recovery_side is set (Tier 1 or 2),
        # only allow orders on the minority side. Block majority-side orders entirely.
        ecr_recovery_side = self._ecr_recovery_side  # "up", "down", or None
        
        # === Proportional order sizing ===
        # In a skewed market (e.g., UP=0.70 DOWN=0.30), equal dollar orders
        # produce unbalanced shares.  Split the pair budget by price ratio
        # so both sides get equal shares.
        price_sum = up_limit + down_limit
        if price_sum > 0:
            up_cost_ratio = up_limit / price_sum
            down_cost_ratio = down_limit / price_sum
        else:
            up_cost_ratio = down_cost_ratio = 0.5
        
        pair_budget = self.batch_size * 2  # Total per order pair
        
        # Minimum order cost enforces the Polymarket minimum order size.
        # CRITICAL: compute per-side using each side's OWN limit price.
        # Using min(up, down) was a bug — the expensive side needs MORE dollars
        # to reach min_order_shares.  E.g. with UP=0.54, DOWN=0.40, min_shares=5:
        #   UP needs 5×0.54=$2.70, DOWN needs 5×0.40=$2.00.
        #   Using min_price=$0.40 gave both sides $2.00 floor, producing only
        #   3.7 UP shares → rejected by Polymarket (minimum: 5).
        min_order_cost_up = max(self.min_order_shares * up_limit, self.batch_size * 0.5)
        min_order_cost_down = max(self.min_order_shares * down_limit, self.batch_size * 0.5)
        # Loop guard: continue while we can afford at least the cheaper side
        min_order_cost = min(min_order_cost_up, min_order_cost_down)
        while available >= min_order_cost and orders_created < max_orders_per_tick:
            # Calculate current imbalance
            gap_shares = abs(up_shares_total - down_shares_total)
            max_shares = max(up_shares_total, down_shares_total)
            is_imbalanced = max_shares > 0 and gap_shares / max_shares > 0.1
            
            # Determine primary (lagging) and secondary (leading) sides
            if up_shares_total <= down_shares_total:
                primary_side, secondary_side = "up", "down"
                primary_limit, secondary_limit = up_limit, down_limit
                primary_count, secondary_count = pending_up_count, pending_down_count
            else:
                primary_side, secondary_side = "down", "up"
                primary_limit, secondary_limit = down_limit, up_limit
                primary_count, secondary_count = pending_down_count, pending_up_count
            
            created = False
            
            # Phase 3: Don't buy low probability side
            if phase == 3:
                if primary_limit < self.low_prob_threshold:
                    break  # Stop if primary side is low probability
            
            # Primary side order (lagging side - always try)
            # In trend following mode, asymmetric pricing in _calculate_limit_prices
            # handles which side fills first — no need to skip any side here.
            # ECR recovery: skip if this side is blocked by recovery constraint
            primary_blocked = ecr_recovery_side is not None and primary_side != ecr_recovery_side
            primary_cost_ratio = up_cost_ratio if primary_side == "up" else down_cost_ratio
            primary_min_cost = min_order_cost_up if primary_side == "up" else min_order_cost_down
            primary_order_budget = max(pair_budget * primary_cost_ratio, primary_min_cost)
            if not primary_blocked and primary_count < max_pending_per_side and available >= primary_order_budget:
                # Proportional order sizing: scale cost by price ratio for balanced shares.
                order_cost = min(available, primary_order_budget)
                
                if self._should_place_order(primary_side, primary_limit, order_cost):
                    signals.append(self._create_signal(primary_side, primary_limit, order_cost))
                    shares = order_cost / primary_limit
                    # Add to pending_orders for tracking
                    self._order_counter += 1
                    self.pending_orders.append(LimitOrder(
                        order_id=f"{primary_side}_{self._order_counter}",
                        side=primary_side,
                        price=primary_limit,
                        shares=shares,
                        cost=order_cost,
                        created_market_price=price_data.up_price if primary_side == "up" else price_data.down_price,
                    ))
                    available -= order_cost
                    if primary_side == "up":
                        pending_up_count += 1
                        up_shares_total += shares
                    else:
                        pending_down_count += 1
                        down_shares_total += shares
                    created = True
                    orders_created += 1
            
            # Secondary side order (leading side)
            # Both sides get orders — asymmetric pricing handles fill priority.
            # ECR recovery: skip if this side is blocked by recovery constraint
            secondary_blocked = ecr_recovery_side is not None and secondary_side != ecr_recovery_side
            secondary_cost_ratio = up_cost_ratio if secondary_side == "up" else down_cost_ratio
            secondary_min_cost = min_order_cost_up if secondary_side == "up" else min_order_cost_down
            secondary_order_budget = max(pair_budget * secondary_cost_ratio, secondary_min_cost)
            if not secondary_blocked and secondary_count < max_pending_per_side and available >= secondary_order_budget:
                # Phase 3: Don't buy low probability side
                if phase == 3 and secondary_limit < self.low_prob_threshold:
                    pass  # Skip
                else:
                    order_cost = min(available, secondary_order_budget)
                    if self._should_place_order(secondary_side, secondary_limit, order_cost):
                        signals.append(self._create_signal(secondary_side, secondary_limit, order_cost))
                        shares = order_cost / secondary_limit
                        # Add to pending_orders for tracking
                        self._order_counter += 1
                        self.pending_orders.append(LimitOrder(
                            order_id=f"{secondary_side}_{self._order_counter}",
                            side=secondary_side,
                            price=secondary_limit,
                            shares=shares,
                            cost=order_cost,
                            created_market_price=price_data.up_price if secondary_side == "up" else price_data.down_price,
                        ))
                        available -= order_cost
                        if secondary_side == "up":
                            pending_up_count += 1
                            up_shares_total += shares
                        else:
                            pending_down_count += 1
                            down_shares_total += shares
                        created = True
                        orders_created += 1
            
            # Exit if no orders created this iteration
            if not created:
                break
        
        if signals:
            logger.debug(
                f"[{self.name}] Created {len(signals)} orders: " +
                ", ".join(f"{s.token_type}@{s.target_price:.1%}" for s in signals)
            )

        return signals
    
    def _generate_rebalancing_order(self, price_data: PriceData) -> Optional[OrderSignal]:
        """
        Generate an aggressive limit order to rebalance severely imbalanced position.
        
        Uses a limit price just below market (0.2% discount) so the order sits on
        the book as a MAKER — paying 0% fee instead of 1.56% taker fee.  At 0.2%
        below market, the order fills on the first micro-dip (typically within 1-3
        seconds in 15-min crypto markets).
        
        If the order doesn't fill immediately it enters the pending queue and will
        be checked every price update, or cancelled after order_timeout.
        
        Uses market_order_size_cap to limit order size.
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
        
        # Enforce minimum order size (Polymarket minimum)
        if shares_needed < self.min_order_shares:
            shares_needed = float(self.min_order_shares)
        
        # Apply order size cap (fraction of total position cost)
        total_cost = self.up_position.cost + self.down_position.cost
        max_order_cost = total_cost * self.market_order_size_cap
        
        # Aggressive limit price: 0.2% below market.
        # This keeps us as MAKER (0% fee + 20% daily rebate) while still being
        # close enough to market that the order fills on micro-dips.
        # Previous approach: market_price * 1.01 (taker, 1.56% fee at 50¢).
        order_price = max(market_price * 0.998, 0.01)
        
        # Calculate order cost and cap if necessary
        order_cost = shares_needed * order_price
        if order_cost > max_order_cost and max_order_cost > 0:
            capped_shares = max_order_cost / order_price
            logger.info(
                f"[{self.name}] REBALANCING: Capping from {shares_needed:.1f} to {capped_shares:.1f} shares "
                f"(${order_cost:.2f} → ${max_order_cost:.2f}, cap={self.market_order_size_cap:.0%})"
            )
            shares_needed = capped_shares
            order_cost = max_order_cost
        
        # ECR check: reject rebalancing if it would push ECR past threshold
        projected_ecr = self.calculate_projected_ecr(underweight_side, shares_needed, order_price)
        if projected_ecr > 0 and projected_ecr >= self.ecr_threshold:
            logger.warning(
                f"[{self.name}] REBALANCING REJECTED: would push ECR to {projected_ecr:.2%} "
                f"(threshold {self.ecr_threshold:.2%}). "
                f"Skipping {shares_needed:.1f} {underweight_side} shares."
            )
            self._last_rebalancing_rejection_time = datetime.now()
            return None
        
        logger.info(
            f"[{self.name}] REBALANCING: {underweight_side.upper()} {shares_needed:.1f} shares "
            f"limit ${order_price:.4f} (market ${market_price:.4f}, -0.2%) "
            f"balance {self.balance_ratio:.0%} → ~{target_ratio:.0%} "
            f"(projected ECR: {projected_ecr:.2%})"
        )
        
        return self._create_signal(underweight_side, order_price, order_cost)

    def _calculate_limit_prices(self, up_price: float, down_price: float) -> Tuple[float, float]:
        """
        Calculate limit prices based on target cost with trend-following adjustment.
        
        Core idea: total of limit prices must equal target_cost. The question is
        how to ALLOCATE the discount (price_sum - target_cost) between the two sides.
        
        Normal mode (balanced market): proportional allocation (same % discount).
        Trend patience mode (skewed market): asymmetric allocation:
          - Dominant (trending) side: small discount → tight limit, fills on micro-dips
          - Minority (declining) side: large discount → wide limit, fills on pullback
        
        The key insight: in a trending market, the trending side is getting MORE
        expensive — buy it NOW before it costs even more. The declining side is
        getting CHEAPER — be patient and buy it later at a better price.
        
        Uses adaptive target_cost when enabled: tight spreads → higher target (fills
        more easily), wide spreads → lower target (larger safety margin).
        """
        price_sum = up_price + down_price
        
        # Use adaptive target_cost based on current spread
        effective_target = self._get_adaptive_target_cost()

        if price_sum <= effective_target:
            return up_price, down_price

        total_offset = price_sum - effective_target  # Total discount needed (e.g., 0.02)
        max_price = max(up_price, down_price)
        
        # === Trend patience: asymmetric offset allocation ===
        # When market is moderately skewed, allocate offset asymmetrically:
        # - Dominant side (trending, expensive): gets LESS offset → tighter limit
        # - Minority side (declining, cheap): gets MORE offset → wider limit
        #
        # Example: UP=70%, DOWN=30%, target=0.98, total_offset=0.02
        #   Proportional: UP_offset=0.014, DOWN_offset=0.006 (both 2% from market)
        #   Asymmetric:   UP_offset=0.005, DOWN_offset=0.015
        #     → UP limit = 0.695 (0.7% below market, fills easily)
        #     → DOWN limit = 0.285 (5% below market, waits for pullback)
        #     → Total = 0.695 + 0.285 = 0.98 ✓
        if max_price > self.trend_patience_threshold:
            # Determine dominant (trending) and minority (declining) sides
            if up_price >= down_price:
                dominant_price, minority_price = up_price, down_price
                dominant_is_up = True
            else:
                dominant_price, minority_price = down_price, up_price
                dominant_is_up = False
            
            # How skewed is the market? 0 at threshold, 1 at skew guard
            skew_intensity = (max_price - self.trend_patience_threshold) / \
                             (self.max_skew_threshold - self.trend_patience_threshold)
            skew_intensity = min(1.0, max(0.0, skew_intensity))
            
            # Dominant side gets less offset (tighter limit):
            #   At skew_intensity=0 (65%): 50% share (normal proportional)
            #   At skew_intensity=1 (85%): 20% share (very tight)
            dominant_share = 0.50 - 0.30 * skew_intensity  # 0.50 → 0.20
            
            dominant_offset = total_offset * dominant_share
            minority_offset = total_offset * (1.0 - dominant_share)
            
            if dominant_is_up:
                up_limit = up_price - dominant_offset
                down_limit = down_price - minority_offset
            else:
                down_limit = down_price - dominant_offset
                up_limit = up_price - minority_offset
            
            logger.debug(
                f"[{self.name}] Trend patience pricing (skew={skew_intensity:.0%}): "
                f"UP {up_price:.2f}→{up_limit:.3f} ({(up_price-up_limit)/up_price:.1%} off), "
                f"DOWN {down_price:.2f}→{down_limit:.3f} ({(down_price-down_limit)/down_price:.1%} off)"
            )
        else:
            # Normal: proportional scaling (same % discount on both sides)
            base_scale = effective_target / price_sum
            up_limit = up_price * base_scale
            down_limit = down_price * base_scale
            
            # === Mild trend adjustment for balanced markets ===
            if self.enable_trend_detection:
                trend_side, trend_confidence = self._trend_detector.get_trend()
                
                if trend_confidence > 0.3:
                    trend_shift_factor = min(0.5, trend_confidence * 0.6)
                    up_offset = up_price - up_limit
                    down_offset = down_price - down_limit
                    
                    if trend_side == "up":
                        shift_amount = min(up_offset, down_offset) * trend_shift_factor
                        up_limit += shift_amount
                        down_limit -= shift_amount * 0.5
                    elif trend_side == "down":
                        shift_amount = min(up_offset, down_offset) * trend_shift_factor
                        down_limit += shift_amount
                        up_limit -= shift_amount * 0.5
        
        # === Urgency adjustment (on top of trend adjustment) ===
        if self.enable_urgency_pricing:
            urgency = self._calculate_combined_urgency()
            
            if urgency > 0:
                # Calculate offset from market price (using current limits)
                up_offset = up_price - up_limit
                down_offset = down_price - down_limit
                
                # Reduce offset based on urgency (up to urgency_price_factor reduction)
                offset_reduction = urgency * self.urgency_price_factor
                
                up_limit = up_limit + up_offset * offset_reduction
                down_limit = down_limit + down_offset * offset_reduction
                
                # Lagging side boost: move even closer to market price
                lagging_side = self.get_lagging_side()
                if urgency > 0.7 and lagging_side != "balanced":
                    if lagging_side == "up":
                        # Boost up_limit toward market price
                        up_limit = min(up_price * 0.995, up_limit + (up_price - up_limit) * 0.5)
                    else:
                        # Boost down_limit toward market price
                        down_limit = min(down_price * 0.995, down_limit + (down_price - down_limit) * 0.5)
        
        # Ensure limits stay positive and maintain arbitrage condition
        up_limit = max(0.01, up_limit)
        down_limit = max(0.01, down_limit)
        
        # Safety: ensure total limit < 1 for arbitrage profit potential
        if up_limit + down_limit >= 1.0:
            # Scale down proportionally
            scale = 0.99 / (up_limit + down_limit)
            up_limit *= scale
            down_limit *= scale
        
        return up_limit, down_limit

    # ------------------------------------------------------------------
    # Order gating — split into focused sub-methods for readability
    # ------------------------------------------------------------------

    def _should_place_order(self, side: str, limit_price: float, order_cost: Optional[float] = None) -> bool:
        """
        Top-level gate: should the strategy emit this order?

        Delegates to focused sub-checks in priority order.  Returns as soon
        as any sub-check produces a definitive answer.
        """
        if order_cost is None:
            order_cost = self.batch_size

        # --- 1. Basic validation ------------------------------------------------
        ok, market_price = self._check_order_basics(side, limit_price, order_cost)
        if not ok:
            return False

        up = self.up_position.shares
        down = self.down_position.shares

        # --- 2. Position imbalance hard guard (>3× ratio) -----------------------
        if not self._check_imbalance_guard(side, up, down):
            return False

        phase = self.get_market_phase()
        current_ecr = self.effective_cost_rate
        predicted_ecr = self._predict_effective_cost_rate(side, limit_price, order_cost)

        # --- 3. Initial position (no fills yet) ---------------------------------
        if up == 0 and down == 0:
            return self._check_initial_position(side, limit_price, order_cost, phase, predicted_ecr)

        # --- 4. Recovery mode for severely imbalanced positions ------------------
        recovery_result = self._check_recovery_mode(side, up, down, current_ecr, predicted_ecr)
        if recovery_result is not None:
            return recovery_result

        # --- 5. Recovery-side exemption (from balance rule in on_price_update) ---
        recovery_exempt = self._check_recovery_exemption(
            side, phase, current_ecr, predicted_ecr, market_price
        )
        if recovery_exempt is not None:
            return recovery_exempt

        # --- 6. ECR protection per phase ----------------------------------------
        if not self._check_ecr_protection(side, up, down, phase, limit_price, order_cost,
                                          current_ecr, predicted_ecr):
            return False

        # --- 7. Phase 3 settlement protection -----------------------------------
        if phase == 3:
            return self._check_phase3(side, market_price, current_ecr, predicted_ecr)

        # --- 8. Phase 1-2 imbalance preference ----------------------------------
        return self._check_imbalance_preference(side, up, down)

    # ---- Sub-checks (private) ------------------------------------------------

    def _check_order_basics(self, side: str, limit_price: float, order_cost: float
                            ) -> Tuple[bool, Optional[float]]:
        """Validate basic order parameters.  Returns (ok, market_price)."""
        if limit_price <= 0 or order_cost <= 0:
            return False, None
        market_price = self.current_up_price if side == "up" else self.current_down_price
        if market_price is None or market_price <= 0:
            return False, None
        if limit_price > market_price:
            return False, None
        return True, market_price

    def _check_imbalance_guard(self, side: str, up: float, down: float) -> bool:
        """Block overweight-side orders when imbalance exceeds 3×."""
        if up > 0 and down > 0:
            max_s, min_s = max(up, down), min(up, down)
            ratio = max_s / min_s if min_s > 0 else float("inf")
            overweight = "up" if up > down else "down"
            if ratio > 3.0 and side == overweight:
                logger.debug(
                    f"[{self.name}] Imbalance guard: blocking {side.upper()} "
                    f"(ratio={ratio:.1f}×, UP={up:.1f}, DOWN={down:.1f})"
                )
                return False
        return True

    def _check_initial_position(self, side: str, limit_price: float,
                                order_cost: float, phase: int,
                                predicted_ecr: float) -> bool:
        """Gate for the very first orders when no position exists."""
        if self.current_up_price is None or self.current_down_price is None:
            return False

        effective_target = self._get_adaptive_target_cost()
        price_sum = self.current_up_price + self.current_down_price
        if price_sum > 0:
            scale = effective_target / price_sum if price_sum > effective_target else 1.0
            up_limit = self.current_up_price * scale
            down_limit = self.current_down_price * scale

            up_shares = order_cost / up_limit if up_limit > 0 else 0
            down_shares = order_cost / down_limit if down_limit > 0 else 0
            hedged = min(up_shares, down_shares)

            if hedged > 0:
                balanced_ecr = (order_cost * 2) / hedged
                threshold = self._get_phase1_ecr_limit()
                if balanced_ecr > threshold:
                    logger.debug(
                        f"[{self.name}] Initial rejected {side.upper()}@{limit_price:.1%}"
                        f"(${order_cost:.2f}): balanced ECR {balanced_ecr:.2%} > {threshold:.0%}"
                    )
                    return False

        single_threshold = self._get_phase1_ecr_limit() if phase == 1 else 1.0
        if predicted_ecr != float("inf") and predicted_ecr >= single_threshold:
            logger.debug(
                f"[{self.name}] Initial rejected {side.upper()}@{limit_price:.1%}"
                f"(${order_cost:.2f}): predicted ECR {predicted_ecr:.1%} >= {single_threshold:.0%}"
            )
            return False
        return True

    def _check_recovery_mode(self, side: str, up: float, down: float,
                             current_ecr: float, predicted_ecr: float
                             ) -> Optional[bool]:
        """
        Handle severely imbalanced positions (balance < 50%).

        Returns True/False when a definitive decision is made, or None to
        continue to the next sub-check.
        """
        balance = self.balance_ratio
        if balance >= 0.50 or (up == 0 and down == 0):
            # Not in recovery territory — edge-case: infinite ECR with balance >= 0.50
            if current_ecr == float("inf"):
                return False if predicted_ecr == float("inf") else True
            return None  # Continue to next check

        minority = "up" if up < down else "down"

        if current_ecr == float("inf") and predicted_ecr == float("inf"):
            logger.debug(
                f"[{self.name}] Recovery rejected {side.upper()}: "
                f"would stay single-sided (ECR ∞)"
            )
            return False

        if side == minority:
            logger.debug(
                f"[{self.name}] Recovery order: {side.upper()} "
                f"(balance={balance:.2f}, ECR {current_ecr:.2f}→{predicted_ecr:.2f})"
            )
            return True

        logger.debug(
            f"[{self.name}] Recovery blocked majority {side.upper()} "
            f"(balance={balance:.2f})"
        )
        return False

    def _check_recovery_exemption(self, side: str, phase: int,
                                  current_ecr: float, predicted_ecr: float,
                                  market_price: float) -> Optional[bool]:
        """
        Handle orders pre-approved by the balance rule (_ecr_recovery_side).

        Returns True/False when a definitive decision is made, or None to
        continue to the next sub-check.
        """
        if self._ecr_recovery_side is None or side != self._ecr_recovery_side:
            return None  # Not a recovery-exempted order

        if phase == 3:
            if current_ecr < 1.0:
                logger.debug(f"[{self.name}] Phase 3 recovery rejected — already profitable")
                return False
            if predicted_ecr >= current_ecr:
                logger.debug(f"[{self.name}] Phase 3 recovery rejected — wouldn't improve ECR")
                return False
            return True

        if predicted_ecr <= current_ecr + 0.02:
            return True  # Improves or maintains ECR

        logger.debug(
            f"[{self.name}] Recovery exemption rejected {side.upper()}: "
            f"ECR {current_ecr:.1%}→{predicted_ecr:.1%}"
        )
        return False

    def _check_ecr_protection(self, side: str, up: float, down: float,
                              phase: int, limit_price: float, order_cost: float,
                              current_ecr: float, predicted_ecr: float) -> bool:
        """ECR-based order rejection for Phase 1 vs Phase 2-3."""
        if phase == 1:
            cap = self._get_phase1_ecr_limit()
            if predicted_ecr >= cap:
                logger.debug(
                    f"[{self.name}] Phase 1 ECR cap: {side.upper()}@{limit_price:.1%} "
                    f"rejected (predicted {predicted_ecr:.1%} >= {cap:.0%})"
                )
                return False
        else:
            ecr_tolerance = 0.02
            improves = (side == "up" and up < down) or (side == "down" and down < up)

            if improves:
                if predicted_ecr > current_ecr + ecr_tolerance:
                    logger.debug(
                        f"[{self.name}] Balance order rejected {side.upper()}: "
                        f"ECR {current_ecr:.1%}→{predicted_ecr:.1%}"
                    )
                    return False
            else:
                if predicted_ecr > current_ecr + ecr_tolerance:
                    logger.debug(
                        f"[{self.name}] Order rejected {side.upper()}: "
                        f"ECR worsens {current_ecr:.1%}→{predicted_ecr:.1%}"
                    )
                    return False
                if predicted_ecr >= 1.0:
                    logger.debug(
                        f"[{self.name}] Order rejected {side.upper()}: "
                        f"ECR would exceed 100% ({predicted_ecr:.1%})"
                    )
                    return False
        return True

    def _check_phase3(self, side: str, market_price: float,
                      current_ecr: float, predicted_ecr: float) -> bool:
        """
        Phase 3 settlement protection with conditional profit improvement.
        
        When ECR < 1.0 (profitable):
          - Allow orders on the lagging side that would IMPROVE ECR further
            (e.g. ECR 0.98 → 0.96), increasing guaranteed profit margin.
          - Block orders that would worsen or not improve ECR.
        When ECR >= 1.0 (unprofitable):
          - Only allow ECR-improving orders on the lagging side (recovery mode).
        """
        if market_price < self.low_prob_threshold:
            logger.debug(f"[{self.name}] Phase 3: low prob {side.upper()} ({market_price:.1%})")
            return False

        lagging = self.get_lagging_side()

        if current_ecr < 1.0:
            # Profitable — allow ECR-IMPROVING orders on lagging side only
            if predicted_ecr < current_ecr and side == lagging:
                logger.debug(
                    f"[{self.name}] Phase 3: improving profitable ECR "
                    f"{current_ecr:.2%}→{predicted_ecr:.2%} ({side.upper()})"
                )
                return True
            logger.debug(
                f"[{self.name}] Phase 3: profitable ECR={current_ecr:.2%}, "
                f"{'would worsen' if predicted_ecr >= current_ecr else 'not lagging side'}"
            )
            return False

        if predicted_ecr >= current_ecr:
            logger.debug(
                f"[{self.name}] Phase 3: {side.upper()} wouldn't improve ECR "
                f"({current_ecr:.2%}→{predicted_ecr:.2%})"
            )
            return False

        if lagging != "balanced" and side != lagging:
            logger.debug(f"[{self.name}] Phase 3: leading {side.upper()} paused")
            return False

        return True

    def _check_imbalance_preference(self, side: str, up: float, down: float) -> bool:
        """Phase 1-2: prefer lagging side, allow trending side through."""
        max_s, min_s = max(up, down), min(up, down)
        if max_s > 0 and (max_s - min_s) / max_s > 0.1:
            is_lagging = (side == "up" and up < down) or (side == "down" and down < up)
            if is_lagging:
                return True

            if self.enable_trend_detection:
                trend_side, confidence = self._trend_detector.get_trend()
                if confidence > 0.3 and side == trend_side:
                    return True

            return False

        return True  # Balanced: allow both sides

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
                "effective_target_cost": self._get_adaptive_target_cost(),
                "batch_size": self.batch_size,
                "batch_ratio": self.batch_ratio,
                "ecr_threshold": self.ecr_threshold,
                "balance_threshold": self.balance_threshold,
                "enable_ecr_stoploss": self.enable_ecr_stoploss,
                "enable_rebalancing": self.enable_rebalancing,
                "enable_trend_detection": self.enable_trend_detection,
                "enable_urgency_pricing": self.enable_urgency_pricing,
                "enable_adaptive_target": self.enable_adaptive_target,
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
