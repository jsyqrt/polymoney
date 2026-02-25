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

import time

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
        self.urgency_price_factor = self.params.get("urgency_price_factor", 0.25)
        self.market_duration = self.params.get("market_duration", 900)  # 15 minutes
        
        # Enhanced rebalancing parameters
        self.severe_imbalance_threshold = self.params.get("severe_imbalance_threshold", 0.50)
        self.market_order_size_cap = self.params.get("market_order_size_cap", 0.10)
        
        # Order pacing parameters
        self.max_orders_per_tick = self.params.get("max_orders_per_tick", 2)
        
        # Market skew filter: reject entering markets where one side dominates
        # If max(up_price, down_price) > max_skew_threshold, skip new orders
        # This prevents one-sided position building in extremely skewed markets
        self.max_skew_threshold = self.params.get("max_skew_threshold", 0.90)
        self._skew_rejection_logged = False  # avoid log spam
        
        # Phase 3 directional tilt: allow buying the probable winner even
        # if it creates imbalance.  Near settlement, expected value of buying
        # the high-probability side at below-market price is positive.
        self.enable_phase3_tilt = self.params.get("enable_phase3_tilt", True)
        self.phase3_tilt_min_prob = self.params.get("phase3_tilt_min_prob", 0.65)
        self._phase3_tilt_logged = False
        
        # Trend patience mode: when market is moderately skewed (one side > this threshold
        # but below max_skew_threshold), only buy the CHEAP side. The expensive side orders
        # are skipped entirely — we accumulate the cheap token and wait for a pullback to
        # buy the expensive side at a better price. This replaces the old approach of
        # refusing to trade in skewed markets entirely.
        self.trend_patience_threshold = self.params.get("trend_patience_threshold", 0.60)
        self._trend_patience_logged = False  # avoid log spam
        
        # Directional recovery: when ECR > 1.0 and shares are balanced (normal
        # recovery can't help), bet on the probable winner.  Each share bought
        # below market price has positive EV = prob_win - limit_price.  Only
        # activates with confirmed trend + high probability.
        self.enable_directional_recovery = self.params.get("enable_directional_recovery", True)
        self.directional_recovery_min_prob = self.params.get("directional_recovery_min_prob", 0.62)
        self.directional_recovery_min_trend = self.params.get("directional_recovery_min_trend", 0.40)
        self.directional_recovery_max_ratio = self.params.get("directional_recovery_max_ratio", 1.5)

        # Profit-taking sell parameters.
        # DISABLED by default: in hedged binary markets, the winning side
        # settles at $1. Selling it at any price < $1 destroys value vs
        # simply holding to settlement. Only enable if you explicitly want
        # early cash-out and accept the settlement loss.
        self.enable_profit_sell = self.params.get("enable_profit_sell", False)
        self.sell_profit_threshold = self.params.get("sell_profit_threshold", 0.85)
        self.sell_min_phase = self.params.get("sell_min_phase", 2)
        self.sell_discount = self.params.get("sell_discount", 0.005)
        self._last_sell_time: float = 0.0
        self._sell_cooldown: float = self.params.get("sell_cooldown", 10.0)

        # Pre-settlement exit: sell ALL positions before market closes so we
        # never need to redeem.  Starts `exit_lead_seconds` before settlement.
        self.enable_exit_sell = self.params.get("enable_exit_sell", True)
        self.exit_lead_seconds = self.params.get("exit_lead_seconds", 180.0)
        self.exit_sell_interval = self.params.get("exit_sell_interval", 3.0)
        self.exit_hold_winner_seconds = self.params.get("exit_hold_winner_seconds", 60.0)
        self._exit_mode = False
        self._exit_logged = False
        self._last_exit_sell_time: float = 0.0

        # Hard stop-loss: abandon market when realized ECR is irrecoverable.
        # When realized ECR > threshold with severe imbalance past the midpoint
        # of market duration, stop all activity to limit losses.
        self.abandon_ecr_threshold = self.params.get("abandon_ecr_threshold", 1.02)
        self.abandon_balance_threshold = self.params.get("abandon_balance_threshold", 0.30)
        self.abandon_time_threshold = self.params.get("abandon_time_threshold", 0.50)
        self._abandon_mode = False
        self._abandon_logged = False

        # Early abandon for extreme one-sided positions: when ECR is extremely
        # high (>3) with near-zero balance (<0.1), the position is essentially
        # a directional bet. Continuing to place orders wastes capital.
        # Triggers earlier (20% of market time) than normal abandon (50%).
        self.early_abandon_ecr_threshold = self.params.get("early_abandon_ecr_threshold", 3.0)
        self.early_abandon_balance_threshold = self.params.get("early_abandon_balance_threshold", 0.10)
        self.early_abandon_time_threshold = self.params.get("early_abandon_time_threshold", 0.20)

        # Late-game directional signal from Binance price delta.
        # When enabled and Binance delta exceeds threshold in the final
        # portion of market duration, bias limit prices toward predicted
        # winning side for better fill odds.
        self.enable_directional_signal = self.params.get("enable_directional_signal", True)
        self.directional_min_delta = self.params.get("directional_min_delta", 0.003)
        self.directional_late_game_ratio = self.params.get("directional_late_game_ratio", 0.30)
        # Injected by runner from BinancePriceFeed
        self.binance_delta: Optional[float] = None
        self.binance_price: Optional[float] = None
        self._directional_signal_logged = False

        # Sequential ordering: only place secondary (leading) side when the
        # primary (lagging) side has enough pending or filled shares.
        # This prevents one-sided position building from asymmetric fills.
        self.enable_sequential_ordering = self.params.get("enable_sequential_ordering", True)
        # Maximum allowed imbalance ratio for secondary-side orders.
        # If (leading_shares - lagging_shares) / leading_shares > this, block secondary.
        # Tightened from 0.30 → 0.20: in live trading, asymmetric fills are the
        # primary cause of ECR > 1.0.  A 20% gap limit catches the problem earlier.
        self.sequential_max_gap_ratio = self.params.get("sequential_max_gap_ratio", 0.20)

        # Minimum maker discount: ensure limit prices are at least this %
        # below market to avoid crossing the spread and filling as taker.
        self.min_maker_discount = self.params.get("min_maker_discount", 0.015)
        
        # Fill urgency mode: use aggressive limit pricing to improve fill rate.
        # When enabled, limits are set closer to market price (smaller discount),
        # sacrificing profit margin for faster fills. Important for live trading
        # where maker orders may not fill if market moves away.
        self.fill_urgency_mode = self.params.get("fill_urgency_mode", False)
        # Urgency discount multiplier: 1.0 = normal, 0.5 = half the discount (more aggressive)
        self.urgency_discount_factor = self.params.get("urgency_discount_factor", 0.5)

        # Per-side exposure cap: maximum shares on any single side before
        # the other side must catch up. Expressed as ratio of position_size.
        self.max_single_side_exposure = self.params.get(
            "max_single_side_exposure", 0.0
        )  # 0 = disabled; set to e.g. 50 to cap at 50 shares

        # Fill-rate adaptive pricing: track rolling fill counts per side and
        # skew limit prices so the hard-to-fill side gets tighter limits.
        # This is the core mechanism against asymmetric fills.
        self.enable_fill_rate_skew = self.params.get("enable_fill_rate_skew", True)
        # EMA decay for fill rate tracking (lower = more memory)
        self._fill_rate_ema_alpha: float = self.params.get("fill_rate_ema_alpha", 0.15)
        # Maximum limit price adjustment from fill-rate skew (fraction of offset)
        self.fill_rate_skew_max: float = self.params.get("fill_rate_skew_max", 0.40)
        self._up_fill_ema: float = 0.5   # starts balanced
        self._down_fill_ema: float = 0.5
        self._up_cancel_ema: float = 0.5
        self._down_cancel_ema: float = 0.5

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
        self._directional_recovery_side: Optional[str] = None  # "up"/"down" for directional bet
        self._directional_recovery_logged = False
        self._last_rebalancing_time: Optional[datetime] = None
        self._last_rebalancing_rejection_time: Optional[datetime] = None
        self._rebalancing_events: List[Dict[str, Any]] = []
        
        # Trend detection (time-based)
        self._trend_detector = TrendDetector(
            window_seconds=self.momentum_window_seconds,
            max_momentum_threshold=self.max_momentum_threshold,
            trend_stop_threshold=self.trend_stop_threshold,
        )

    def record_fill_event(self, side: str, is_cancelled: bool) -> None:
        """Update fill-rate EMA based on an order outcome.

        Called by the runner whenever an order fills or gets cancelled.
        Maintains a per-side exponential moving average that tracks how
        easily each side is filling, used to skew limit prices.
        """
        if not self.enable_fill_rate_skew:
            return
        alpha = self._fill_rate_ema_alpha
        if side == "up":
            if is_cancelled:
                self._up_cancel_ema = alpha * 1.0 + (1 - alpha) * self._up_cancel_ema
                self._up_fill_ema = alpha * 0.0 + (1 - alpha) * self._up_fill_ema
            else:
                self._up_fill_ema = alpha * 1.0 + (1 - alpha) * self._up_fill_ema
                self._up_cancel_ema = alpha * 0.0 + (1 - alpha) * self._up_cancel_ema
        else:
            if is_cancelled:
                self._down_cancel_ema = alpha * 1.0 + (1 - alpha) * self._down_cancel_ema
                self._down_fill_ema = alpha * 0.0 + (1 - alpha) * self._down_fill_ema
            else:
                self._down_fill_ema = alpha * 1.0 + (1 - alpha) * self._down_fill_ema
                self._down_cancel_ema = alpha * 0.0 + (1 - alpha) * self._down_cancel_ema

    def _get_fill_rate_skew(self) -> float:
        """Return a skew factor in [-1, +1] based on per-side fill rates.

        Positive = UP is filling more easily than DOWN → push DOWN tighter.
        Negative = DOWN is filling more easily than UP → push UP tighter.
        Zero     = balanced fill rates or feature disabled.

        The magnitude is capped at ``fill_rate_skew_max``.
        """
        if not self.enable_fill_rate_skew:
            return 0.0
        up_rate = self._up_fill_ema
        down_rate = self._down_fill_ema
        total = up_rate + down_rate
        if total < 0.01:
            return 0.0
        # Normalised difference: +1 when only UP fills, -1 when only DOWN fills
        raw_skew = (up_rate - down_rate) / total
        return max(-self.fill_rate_skew_max, min(self.fill_rate_skew_max, raw_skew))

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

    @property
    def realized_ecr(self) -> float:
        """ECR based solely on filled positions — no pending order assumptions."""
        total_cost = self.up_position.cost + self.down_position.cost
        min_shares = min(self.up_position.shares, self.down_position.shares)
        return total_cost / min_shares if min_shares > 0 else float("inf")

    def _predict_effective_cost_rate(self, side: str, price: float, order_cost: Optional[float] = None) -> float:
        """
        Predict effective cost rate after a hypothetical order fills.
        
        Uses only realized (filled) positions for share counts. Pending orders
        contribute to cost exposure but NOT to hedged share count, because
        live fill rates are much lower than 100% — counting pending shares
        as filled makes ECR appear artificially healthy.
        
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

        # Only count realized fills — pending orders have uncertain fill rates
        up = self.up_position.shares
        down = self.down_position.shares
        cost = self.up_position.cost + self.down_position.cost
        # Pending orders are committed capital exposure
        pending_cost = sum(o.cost for o in self.pending_orders)
        cost += pending_cost

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

    def _should_abandon_market(self) -> bool:
        """
        Hard stop-loss: detect irrecoverable positions early.
        
        Three tiers:
        1. Pure one-sided: only one side has fills, balance=0.
           Abandon after 15% of duration — no point waiting, the counterpart
           cap (1.25) will allow fills up to that ECR, so if it hasn't filled
           by now the market has moved too far.
        2. Early abandon: ECR > 3.0, balance < 0.10, past 20% of duration
           (extreme one-sided positions that are essentially directional bets)
        3. Normal abandon: ECR > 1.02, balance < 0.30, past 50% of duration
        """
        if self._abandon_mode:
            return True

        up_sh = self.up_position.shares
        down_sh = self.down_position.shares
        has_position = up_sh > 0 or down_sh > 0

        if not has_position:
            return False

        time_urgency = self._calculate_time_urgency()
        bal = self.balance_ratio

        # Pure one-sided: one side filled, the other is zero.
        # The relaxed counterpart cap (1.10) gives the other side a chance to
        # fill, but if it still hasn't after 25% of duration, the market has
        # moved too far and continuing is a losing bet.
        if has_position and (up_sh == 0 or down_sh == 0) and time_urgency >= 0.25:
            return True

        ecr = self.realized_ecr
        if ecr == float("inf"):
            return False

        # Early abandon: extreme one-sided position, stop wasting capital
        if (ecr > self.early_abandon_ecr_threshold
                and bal < self.early_abandon_balance_threshold
                and time_urgency >= self.early_abandon_time_threshold):
            return True

        # Normal abandon
        if (ecr > self.abandon_ecr_threshold
                and bal < self.abandon_balance_threshold
                and time_urgency >= self.abandon_time_threshold):
            return True

        return False

    def _generate_abandon_sell_signals(self, price_data: PriceData) -> List[OrderSignal]:
        """Sell positions when abandon mode triggers.

        For one-sided positions, selling at market immediately is better than
        holding to settlement (coin flip).  For example, DOWN-only at $0.30
        market: selling recovers $3.06 guaranteed vs 50% × $5.10 = $2.55 EV
        from holding.  The earlier we sell, the higher the recovery since the
        losing side's price decays toward 0 as settlement approaches.
        """
        now_ts = time.time()
        if now_ts - self._last_sell_time < self._sell_cooldown:
            return []

        signals: List[OrderSignal] = []
        discount = 0.005

        for side, pos, market_price in [
            ("up", self.up_position, price_data.up_price),
            ("down", self.down_position, price_data.down_price),
        ]:
            if pos.shares < self.min_order_shares:
                continue
            sell_price = max(market_price * (1 - discount), 0.01)
            if sell_price < 0.02:
                continue
            sell_shares = min(pos.shares, pos.shares * 0.5)
            sell_shares = max(sell_shares, self.min_order_shares)
            sell_shares = min(sell_shares, pos.shares)
            token_type = TokenType.YES if side == "up" else TokenType.NO
            signals.append(OrderSignal(
                side=TradeSide.SELL,
                token_type=token_type,
                target_price=sell_price,
                size=sell_shares,
            ))
            logger.info(
                f"[{self.name}] ABANDON-SELL: {side.upper()} "
                f"{sell_shares:.1f}/{pos.shares:.1f}sh @{sell_price:.3f} "
                f"(market={market_price:.3f})"
            )

        if signals:
            self._last_sell_time = now_ts
        return signals

    def _get_directional_signal(self) -> Optional[Tuple[str, float]]:
        """Compute a directional signal from Binance price delta.

        Returns ``("up", confidence)`` or ``("down", confidence)`` when
        a strong enough signal exists in the late-game portion of the
        market. Returns ``None`` otherwise.

        Conditions:
        - Binance delta is available (feed running + epoch recorded)
        - Market is in late-game (time_remaining < directional_late_game_ratio)
        - |delta| exceeds ``directional_min_delta``

        Confidence scales from 0 to 1 based on delta magnitude.
        """
        if not self.enable_directional_signal:
            return None
        if self.binance_delta is None:
            return None

        time_urgency = self._calculate_time_urgency()
        # time_urgency is fraction of market elapsed; late-game = high urgency
        if time_urgency < (1.0 - self.directional_late_game_ratio):
            return None

        delta = self.binance_delta
        if abs(delta) < self.directional_min_delta:
            return None

        # Confidence ramps from 0 at min_delta to 1 at 1% delta
        confidence = min(1.0, abs(delta) / 0.01)
        direction = "up" if delta > 0 else "down"
        return (direction, confidence)

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
        
        Phase 1 is the position-building phase.  We need to be lenient enough
        to allow building positions when only one side has filled (ECR is
        temporarily high), but strict enough to prevent locking in losing
        positions.
        
        Base behavior: decays from 1.20 (start) to 1.02 (end of Phase 1).
        This is much tighter than the previous 1.5→1.1 range.  The old range
        allowed initial fills with combined cost > $1, which was the primary
        source of losses.
        
        Skew adjustment: in highly skewed markets, one-sided fills are more
        likely, so the ECR cap is tightened further.
        
        Returns:
            Maximum allowed ECR for Phase 1 orders.
        """
        if self.market_start_time is None:
            return 1.10
        
        elapsed = (datetime.now() - self.market_start_time).total_seconds()
        progress = min(1.0, elapsed / self.phase1_end)  # 0 → 1 over Phase 1
        base_limit = 1.20 - 0.18 * progress  # 1.20 → 1.02
        
        # Skew adjustment: tighten ECR cap in skewed markets
        if self.current_up_price is not None and self.current_down_price is not None:
            max_price = max(self.current_up_price, self.current_down_price)
            skew_start = 0.50
            skew_end = self.params.get("max_entry_skew", 0.75)
            if max_price > skew_start and skew_end > skew_start:
                skew_intensity = min(1.0, (max_price - skew_start) / (skew_end - skew_start))
                reduction = skew_intensity * 0.40 * (base_limit - 1.0)
                base_limit -= reduction
        
        return max(1.01, base_limit)

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
        self._exit_mode = False
        self._exit_logged = False
        self._abandon_mode = False
        self._abandon_logged = False
        self.binance_delta = None
        self.binance_price = None
        self._directional_signal_logged = False
        self._last_exit_sell_time = 0.0
        self._phase3_tilt_logged = False
        self._directional_recovery_side = None
        self._directional_recovery_logged = False
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
        
        # Minimum spread requirement: if the market is too efficient (price sum
        # very close to 1.0), there's no room for our target discount.
        # e.g. if target_cost = 0.96, we need price_sum - target_cost = 0.04 of
        # room.  If price_sum = 0.99, we can only get 0.03 discount — not enough.
        effective_target = self._get_adaptive_target_cost()
        min_required_discount = 1.0 - effective_target  # e.g. 0.04 for target=0.96
        available_discount = price_sum - effective_target
        if available_discount < min_required_discount * 0.5:
            return []
        
        self.current_up_price = price_data.up_price
        self.current_down_price = price_data.down_price

        signals = []

        # === Pre-settlement exit: sell everything before market closes ===
        if self._is_exit_phase():
            return self._on_exit_phase_tick(price_data)

        # === Hard stop-loss: abandon market if position is irrecoverable ===
        if self._should_abandon_market():
            if not self._abandon_mode:
                self._abandon_mode = True
                ecr = self.realized_ecr
                ecr_str = f"{ecr:.2%}" if ecr != float("inf") else "inf"
                bal = self.balance_ratio
                is_early = (ecr > self.early_abandon_ecr_threshold
                           and bal < self.early_abandon_balance_threshold)
                tier = "EARLY" if is_early else "NORMAL"
                logger.warning(
                    f"[{self.name}] ABANDON({tier}): realized ECR={ecr_str}, "
                    f"balance={bal:.2f}, "
                    f"up={self.up_position.shares:.1f}@${self.up_position.avg_price:.3f}, "
                    f"down={self.down_position.shares:.1f}@${self.down_position.avg_price:.3f}"
                )
            # Sell one-sided positions immediately to recover capital.
            # Holding a one-sided position to settlement is a coin flip:
            # 50% chance worthless ($0), 50% chance full value ($1).
            # Selling at current market (e.g. $0.30) recovers a guaranteed
            # ~60% of cost vs the 50% expected value of holding.
            return self._generate_abandon_sell_signals(price_data)

        # Update trend detector with new prices (time-based)
        if self.enable_trend_detection:
            self._trend_detector.update(
                price_data.up_price,
                price_data.down_price,
                timestamp=price_data.timestamp.timestamp(),
            )
        
        current_risk = self.risk_state
        if current_risk != self._risk_state:
            self._risk_state = current_risk
            logger.info(f"[{self.name}] Risk: {current_risk} ECR={self.effective_cost_rate:.2%}")
        
        current_phase = self.get_market_phase()
        if current_phase != self._current_phase:
            self._current_phase = current_phase
            ecr = self.effective_cost_rate
            ecr_str = f"{ecr:.2%}" if ecr != float("inf") else "-"
            fills = self.up_position.shares + self.down_position.shares
            logger.info(
                f"[{self.name}] Phase {current_phase} | ECR={ecr_str} fills={fills:.0f}"
                + (" [profitable]" if current_phase == 3 and ecr < 1.0
                   else " [recovery]" if current_phase == 3
                   else "")
            )
        
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
        self._directional_recovery_side = None  # reset each tick
        
        if self.enable_ecr_stoploss and total_cost >= min_cost_for_ecr:
            ecr = self.effective_cost_rate
            up_shares = self.up_position.shares
            down_shares = self.down_position.shares
            
            max_shares = max(up_shares, down_shares)
            meaningful_imbalance = max_shares > 0 and abs(up_shares - down_shares) / max_shares > 0.05
            
            if ecr != float("inf") and ecr > self.ecr_threshold and meaningful_imbalance:
                minority_side = "up" if up_shares < down_shares else "down"
                self._ecr_recovery_side = minority_side
                if self._ecr_violations == 0:
                    logger.info(
                        f"[{self.name}] BALANCE: ECR={ecr:.2%}>{self.ecr_threshold:.0%}, "
                        f"minority only ({minority_side.upper()})"
                    )
                self._ecr_violations += 1
            
            # Directional recovery: ECR > 1.0 but shares are balanced —
            # rebalancing won't help.  Bet on probable winner if trend is clear.
            elif (ecr != float("inf") and ecr > 1.0 and not meaningful_imbalance
                  and self.enable_directional_recovery and phase >= 2):
                self._directional_recovery_side = self._evaluate_directional_recovery()
        
        # Check for rebalancing opportunity (use severe threshold for market orders)
        # Only rebalance when BOTH sides have positions - don't rebalance during initial building
        up_shares = self.up_position.shares
        down_shares = self.down_position.shares
        has_both_sides = up_shares > 0 and down_shares > 0
        severe_imbalance = self.balance_ratio < self.severe_imbalance_threshold
        
        # DELAY SELL-REBALANCE: Data shows all 3-fill markets have sells and
        # average -$0.53/market, while 4-fill no-sell markets average +$0.039.
        # The sell-rebalance after 3 fills locks in ECR > 1.0 and blocks the
        # 4th fill (strict cap applies once balanced).  Wait until 40% of
        # market duration to give the 4th fill a chance.  After 40%, the
        # 4th fill is unlikely and sell-rebalance caps further loss.
        time_urgency = self._calculate_time_urgency()
        rebal_allowed = time_urgency >= 0.40
        
        if has_both_sides and severe_imbalance and rebal_allowed and self.can_rebalance():
            rebalance_signal = self._generate_rebalancing_order(price_data)

            # Fallback: if buying underweight side was rejected (too expensive),
            # try selling excess of the overweight side instead.
            if rebalance_signal is None:
                rebalance_signal = self._generate_sell_to_rebalance(price_data)

            if rebalance_signal:
                signals.append(rebalance_signal)
                self._last_rebalancing_time = datetime.now()
                
                is_sell_rebal = rebalance_signal.side == TradeSide.SELL
                if is_sell_rebal:
                    rebal_side = "up" if rebalance_signal.token_type == TokenType.YES else "down"
                else:
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
                    "type": "sell_excess" if is_sell_rebal else "buy_underweight",
                    "size": rebalance_signal.size,
                    "price": rebalance_signal.target_price,
                    "balance_before": self.balance_ratio,
                })
                return signals  # Return just the rebalancing order

        # ECR > 1 with any imbalance: sell excess to reduce cost even if
        # balance_ratio isn't below severe_imbalance_threshold.
        # Same delay applies — give the 4th fill time to arrive.
        ecr = self.effective_cost_rate
        if (has_both_sides and ecr != float("inf") and ecr > 1.0
                and not severe_imbalance and rebal_allowed and self.can_rebalance()):
            sell_rebal = self._generate_sell_to_rebalance(price_data)
            if sell_rebal:
                signals.append(sell_rebal)
                self._last_rebalancing_time = datetime.now()
                rebal_side = "up" if sell_rebal.token_type == TokenType.YES else "down"
                self._rebalancing_events.append({
                    "timestamp": datetime.now().isoformat(),
                    "side": rebal_side,
                    "type": "sell_excess_ecr",
                    "size": sell_rebal.size,
                    "price": sell_rebal.target_price,
                    "balance_before": self.balance_ratio,
                    "ecr": ecr,
                })
                return signals

        # === Market skew guard ===
        # Prevent new limit orders in extremely skewed markets where one side dominates.
        # In such markets, only the cheap side limit orders fill, creating dangerous
        # one-sided exposure. Rebalancing (above) still runs for existing positions.
        # Uses hysteresis: activate at max_skew_threshold, deactivate at threshold - 5%
        #
        # Bypasses (allow trading despite skew):
        #   1. Phase 3 directional tilt (existing)
        #   2. Phase 2+ with confirmed strong trend (≥70% confidence matching dominant side)
        #      — other risk controls (ECR, imbalance guard) still protect against bad fills
        max_price = max(price_data.up_price, price_data.down_price)
        skew_deactivate = self.max_skew_threshold - 0.05  # 5% hysteresis band
        phase3_tilt_active = (phase == 3 and self.enable_phase3_tilt
                              and max_price >= self.phase3_tilt_min_prob)
        trend_bypass_active = False
        if phase >= 2 and self.enable_trend_detection:
            dominant = "up" if price_data.up_price > price_data.down_price else "down"
            t_side, t_conf = self._trend_detector.get_trend()
            if t_side == dominant and t_conf >= 0.70:
                trend_bypass_active = True
        skew_bypass = phase3_tilt_active or trend_bypass_active
        if not skew_bypass:
            if max_price > self.max_skew_threshold or (self._skew_rejection_logged and max_price > skew_deactivate):
                if not self._skew_rejection_logged:
                    dominant_side = "UP" if price_data.up_price > price_data.down_price else "DOWN"
                    logger.info(f"[{self.name}] SKEW GUARD: {dominant_side}={max_price:.0%}, pausing orders")
                    self._skew_rejection_logged = True
                return signals
            elif self._skew_rejection_logged:
                logger.info(f"[{self.name}] SKEW GUARD off ({max_price:.0%})")
                self._skew_rejection_logged = False
        elif self._skew_rejection_logged:
            bypass_reason = "P3 tilt" if phase3_tilt_active else f"trend {t_conf:.0%}"
            logger.info(f"[{self.name}] SKEW GUARD bypassed: {bypass_reason} ({max_price:.0%})")
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
                logger.info(f"[{self.name}] TREND: {dominant_side}={max_price:.0%}, asymmetric pricing")
                self._trend_patience_logged = True
        elif self._trend_patience_logged:
            logger.info(f"[{self.name}] TREND off ({max_price:.0%}), symmetric pricing")
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
        # Using 2 per side at all times to maximise fill opportunities — with
        # only 2 fills per market the ECR never averages below 1.0.
        max_pending_per_side = 2
        max_orders_per_tick = self.max_orders_per_tick
        orders_created = 0
        
        # ECR recovery constraint: when _ecr_recovery_side is set (Tier 1 or 2),
        # only allow orders on the minority side. Block majority-side orders entirely.
        ecr_recovery_side = self._ecr_recovery_side  # "up", "down", or None
        
        # === Proportional order sizing ===
        # In a skewed market (e.g., UP=0.70 DOWN=0.30), equal dollar orders
        # produce unbalanced shares.  We want EQUAL SHARES on both sides,
        # which means spending more dollars on the expensive side.
        # Each side gets: min_order_shares * its_limit_price dollars.
        price_sum = up_limit + down_limit
        if price_sum > 0:
            up_cost_ratio = up_limit / price_sum
            down_cost_ratio = down_limit / price_sum
        else:
            up_cost_ratio = down_cost_ratio = 0.5
        
        # Minimum order cost enforces the Polymarket minimum order size.
        # Each side independently needs enough dollars to produce min_order_shares.
        # The expensive side naturally needs more dollars.
        min_order_cost_up = self.min_order_shares * up_limit
        min_order_cost_down = self.min_order_shares * down_limit
        
        # Pair budget: enough for min_order_shares on BOTH sides.
        # If batch_size * 2 < sum of minimums, scale up to the minimums.
        min_pair_cost = min_order_cost_up + min_order_cost_down
        pair_budget = max(self.batch_size * 2, min_pair_cost)
        
        # Loop guard: need enough for at least one side's minimum
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
            primary_blocked = ecr_recovery_side is not None and primary_side != ecr_recovery_side
            primary_cost_ratio = up_cost_ratio if primary_side == "up" else down_cost_ratio
            primary_min_cost = min_order_cost_up if primary_side == "up" else min_order_cost_down
            primary_order_budget = max(pair_budget * primary_cost_ratio, primary_min_cost)

            # Per-side exposure cap for primary too
            if self.max_single_side_exposure > 0 and not primary_blocked:
                pri_shares = up_shares_total if primary_side == "up" else down_shares_total
                if pri_shares >= self.max_single_side_exposure:
                    primary_blocked = True

            if not primary_blocked and primary_count < max_pending_per_side and available >= primary_min_cost:
                order_cost = max(min(available, primary_order_budget), primary_min_cost)
                
                if self._should_place_order(primary_side, primary_limit, order_cost):
                    signal = self._create_signal(primary_side, primary_limit, order_cost)
                    if signal is not None:
                        actual_cost = signal.size * signal.target_price
                        signals.append(signal)
                        self._order_counter += 1
                        self.pending_orders.append(LimitOrder(
                            order_id=f"{primary_side}_{self._order_counter}",
                            side=primary_side,
                            price=primary_limit,
                            shares=signal.size,
                            cost=actual_cost,
                            created_market_price=price_data.up_price if primary_side == "up" else price_data.down_price,
                        ))
                        available -= actual_cost
                        if primary_side == "up":
                            pending_up_count += 1
                            up_shares_total += signal.size
                        else:
                            pending_down_count += 1
                            down_shares_total += signal.size
                        created = True
                        orders_created += 1
            
            # Secondary side order (leading side)
            secondary_blocked = ecr_recovery_side is not None and secondary_side != ecr_recovery_side
            secondary_cost_ratio = up_cost_ratio if secondary_side == "up" else down_cost_ratio
            secondary_min_cost = min_order_cost_up if secondary_side == "up" else min_order_cost_down
            secondary_order_budget = max(pair_budget * secondary_cost_ratio, secondary_min_cost)

            # Sequential ordering gate: block secondary (leading) side when
            # the primary (lagging) side is too far behind.  This prevents
            # one-sided position building from asymmetric fills.
            #
            # The gap ratio is tightened dynamically when fill-rate asymmetry
            # is detected: if one side consistently fails to fill, the gate
            # closes earlier to prevent further imbalance accumulation.
            if self.enable_sequential_ordering and not secondary_blocked:
                effective_gap_ratio = self.sequential_max_gap_ratio
                fill_skew = abs(self._get_fill_rate_skew())
                if fill_skew > 0.10:
                    effective_gap_ratio *= max(0.25, 1.0 - fill_skew)
                leading = max(up_shares_total, down_shares_total)
                lagging = min(up_shares_total, down_shares_total)
                if leading > 0 and lagging < leading * (1 - effective_gap_ratio):
                    secondary_blocked = True

            # Per-side exposure cap
            if self.max_single_side_exposure > 0 and not secondary_blocked:
                sec_shares = up_shares_total if secondary_side == "up" else down_shares_total
                if sec_shares >= self.max_single_side_exposure:
                    secondary_blocked = True

            if not secondary_blocked and secondary_count < max_pending_per_side and available >= secondary_min_cost:
                if phase == 3 and secondary_limit < self.low_prob_threshold:
                    pass  # Skip low probability side in Phase 3
                else:
                    order_cost = max(min(available, secondary_order_budget), secondary_min_cost)
                    if self._should_place_order(secondary_side, secondary_limit, order_cost):
                        signal = self._create_signal(secondary_side, secondary_limit, order_cost)
                        if signal is not None:
                            actual_cost = signal.size * signal.target_price
                            signals.append(signal)
                            self._order_counter += 1
                            self.pending_orders.append(LimitOrder(
                                order_id=f"{secondary_side}_{self._order_counter}",
                                side=secondary_side,
                                price=secondary_limit,
                                shares=signal.size,
                                cost=actual_cost,
                                created_market_price=price_data.up_price if secondary_side == "up" else price_data.down_price,
                            ))
                            available -= actual_cost
                            if secondary_side == "up":
                                pending_up_count += 1
                                up_shares_total += signal.size
                            else:
                                pending_down_count += 1
                                down_shares_total += signal.size
                            created = True
                            orders_created += 1
            
            # Exit if no orders created this iteration
            if not created:
                break

        # === Profit-taking sell logic ===
        if self.enable_profit_sell and phase >= self.sell_min_phase:
            now_ts = price_data.timestamp.timestamp() if hasattr(price_data.timestamp, 'timestamp') else time.time()
            if now_ts - self._last_sell_time >= self._sell_cooldown:
                sell_signals = self._generate_sell_signals(price_data)
                if sell_signals:
                    signals.extend(sell_signals)
                    self._last_sell_time = now_ts

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
        
        # Skip rebalancing when the underweight side is very expensive.
        # Buying at 0.90+ means max $0.10 profit at settlement — the capital
        # is better left as cash or used in the next market cycle.
        rebal_max_price = self.params.get("rebal_max_price", 0.85)
        if market_price > rebal_max_price:
            logger.debug(
                f"[{self.name}] Rebal skip: {underweight_side.upper()} "
                f"price={market_price:.3f} > max {rebal_max_price} (poor value)"
            )
            return None

        # Aggressive limit price: 0.2% below market.
        # This keeps us as MAKER (0% fee + 20% daily rebate) while still being
        # close enough to market that the order fills on micro-dips.
        order_price = max(market_price * 0.998, 0.01)
        
        # Calculate order cost and cap if necessary
        order_cost = shares_needed * order_price
        if order_cost > max_order_cost and max_order_cost > 0:
            capped_shares = max_order_cost / order_price
            shares_needed = capped_shares
            order_cost = max_order_cost
        
        projected_ecr = self.calculate_projected_ecr(underweight_side, shares_needed, order_price)
        current_ecr = self.effective_cost_rate
        if projected_ecr > 0 and projected_ecr >= self.ecr_threshold:
            if current_ecr > 0 and projected_ecr < current_ecr:
                pass  # Allow: improving ECR even if still above threshold
            else:
                logger.debug(
                    f"[{self.name}] Rebal rejected: ECR {current_ecr:.2%}→{projected_ecr:.2%}"
                )
                self._last_rebalancing_rejection_time = datetime.now()
                return None
        
        logger.info(
            f"[{self.name}] REBAL: {underweight_side.upper()} {shares_needed:.0f}sh "
            f"@{order_price:.3f} bal={self.balance_ratio:.0%}→~{target_ratio:.0%} "
            f"ECR={current_ecr:.2%}→{projected_ecr:.2%}"
        )
        
        return self._create_signal(underweight_side, order_price, order_cost)

    def _generate_sell_to_rebalance(self, price_data: PriceData) -> Optional[OrderSignal]:
        """Sell excess shares of the overweight side to restore balance.

        Complements _generate_rebalancing_order (which buys the underweight
        side).  Selling the overweight side is preferable when:
        - The underweight side is too expensive to buy (price > rebal_max_price)
        - ECR > 1 and we need to reduce total cost to improve profitability
        - The overweight side's unhedged excess is a directional gamble

        Favorable-condition rules:
        - Losing overweight (price <= 0.50): sell at any positive price —
          these shares trend toward $0 at settlement.
        - Winning overweight (price > 0.50): only sell if price > avg_cost,
          locking in profit on the excess shares.
        """
        if not self.enable_rebalancing:
            return None

        up = self.up_position.shares
        down = self.down_position.shares
        if up == 0 and down == 0:
            return None

        if up > down:
            overweight_side = "up"
            overweight_pos = self.up_position
            market_price = price_data.up_price
            excess = up - down
        else:
            overweight_side = "down"
            overweight_pos = self.down_position
            market_price = price_data.down_price
            excess = down - up

        if excess < self.min_order_shares:
            return None

        sell_discount = 0.002  # 0.2% below market → maker order
        sell_price = max(market_price * (1 - sell_discount), 0.01)

        if sell_price < 0.02:
            return None

        # Favorable-condition gate
        if market_price > 0.50:
            # Winning overweight: only sell excess if profitable
            if sell_price <= overweight_pos.avg_price:
                logger.debug(
                    f"[{self.name}] Sell-rebal skip: {overweight_side.upper()} "
                    f"sell={sell_price:.3f} <= cost={overweight_pos.avg_price:.3f}"
                )
                return None
        # Losing overweight (price <= 0.50): always willing to sell — better than $0

        # Sell at most 50% of excess per cycle to avoid over-correction
        sell_shares = min(excess * 0.50, excess)
        sell_shares = max(sell_shares, float(self.min_order_shares))
        sell_shares = min(sell_shares, excess)

        # Keep at least min_order_shares on the overweight side after selling
        if overweight_pos.shares - sell_shares < self.min_order_shares:
            sell_shares = overweight_pos.shares - self.min_order_shares
            if sell_shares < self.min_order_shares:
                return None

        token_type = TokenType.YES if overweight_side == "up" else TokenType.NO
        signal = OrderSignal(
            side=TradeSide.SELL,
            token_type=token_type,
            target_price=sell_price,
            size=sell_shares,
        )

        hedged = min(up, down)
        new_excess = excess - sell_shares
        new_balance = hedged / (hedged + new_excess) if (hedged + new_excess) > 0 else 1.0

        logger.info(
            f"[{self.name}] SELL-REBAL: {overweight_side.upper()} "
            f"{sell_shares:.1f}/{excess:.1f}excess @{sell_price:.3f} "
            f"(market={market_price:.3f}, cost={overweight_pos.avg_price:.3f}) "
            f"bal={self.balance_ratio:.0%}→~{new_balance:.0%} "
            f"ECR={self.effective_cost_rate:.2%}"
        )
        return signal

    def _generate_sell_signals(self, price_data: PriceData) -> List[OrderSignal]:
        """Generate sell signals to lock in profit before settlement.

        When a side's market price is high (>= sell_profit_threshold), selling
        a portion of that side's shares returns cash immediately, avoiding
        redeem risk and settlement delay.

        CRITICAL safety rules:
        - Never sell more than 50% of hedged shares in one batch
        - Always retain enough winning shares so remaining position stays hedged
        - Must leave at least min_order_shares on the side being sold
        """
        signals: List[OrderSignal] = []

        for side, pos, market_price in [
            ("up", self.up_position, price_data.up_price),
            ("down", self.down_position, price_data.down_price),
        ]:
            if pos.shares < self.min_order_shares * 2:
                continue
            if market_price < self.sell_profit_threshold:
                continue

            other_pos = (
                self.down_position if side == "up" else self.up_position
            )
            hedged = min(pos.shares, other_pos.shares)
            if hedged < self.min_order_shares:
                continue

            # Sell at most 50% of the hedged portion per cycle.
            # After selling, the position remains partially hedged.
            max_sell_ratio = self.params.get("sell_max_ratio", 0.50)
            sellable = hedged * max_sell_ratio

            # Ensure we retain enough shares to stay above min_order_shares
            retain = max(self.min_order_shares, pos.shares * 0.20)
            sellable = min(sellable, pos.shares - retain)
            if sellable < self.min_order_shares:
                continue

            sell_price = max(market_price * (1 - self.sell_discount), 0.01)

            if sell_price <= pos.avg_price:
                continue

            token_type = TokenType.YES if side == "up" else TokenType.NO
            signal = OrderSignal(
                side=TradeSide.SELL,
                token_type=token_type,
                target_price=sell_price,
                size=sellable,
            )
            signals.append(signal)
            logger.info(
                f"[{self.name}] SELL signal: {side.upper()} "
                f"{sellable:.1f}/{pos.shares:.1f}sh @{sell_price:.3f} "
                f"(market={market_price:.3f}, avg_cost={pos.avg_price:.3f}, "
                f"retain={pos.shares - sellable:.1f})"
            )

        return signals

    # ------------------------------------------------------------------
    # Pre-settlement exit phase
    # ------------------------------------------------------------------

    def _is_exit_phase(self) -> bool:
        """Check whether we should be in pre-settlement exit mode.

        Returns True when:
        1. The market is within ``exit_lead_seconds`` of settlement (normal), OR
        2. The hedged position is clearly profitable (ECR < target_cost) with
           good balance (> 70%) and meaningful size — trigger early to lock in
           profit before a market reversal can destroy the hedge.

        Once activated, exit mode stays on for the rest of the market.
        """
        if not self.enable_exit_sell:
            return False
        if self._exit_mode:
            return True
        if self.market_start_time is None:
            return False

        now_ts = datetime.now().timestamp()

        if self.market_settlement_time is not None:
            remaining = self.market_settlement_time - now_ts
        else:
            elapsed = (datetime.now() - self.market_start_time).total_seconds()
            remaining = self.market_duration - elapsed

        if remaining <= self.exit_lead_seconds:
            self._exit_mode = True
            return True

        # Early take-profit trigger: when the hedged position is clearly
        # profitable, sell both sides now instead of risking market reversal.
        # Only trigger after Phase 1 (need enough fills to be meaningful).
        # Threshold is ECR < 1.0 (any profitable hedge), not target_cost,
        # because even ECR=0.97 (2.73% profit) is worth locking in vs the
        # risk of a market reversal wiping out the entire position.
        ecr = self.effective_cost_rate
        hedged = self.hedged_position
        take_profit_ecr = self.params.get("take_profit_ecr", 0.995)
        if (ecr != float("inf")
                and ecr < take_profit_ecr
                and self.balance_ratio >= 0.70
                and hedged >= self.position_size * 0.20):
            self._exit_mode = True
            logger.info(
                f"[{self.name}] EARLY TAKE-PROFIT EXIT: "
                f"ECR={ecr:.4f} < target={self.target_cost:.4f}, "
                f"balance={self.balance_ratio:.0%}, hedged={hedged:.1f}sh"
            )
            return True

        return False

    def _on_exit_phase_tick(self, price_data: PriceData) -> List[OrderSignal]:
        """Generate signals during the exit phase.

        During exit we:
        1. Cancel all pending BUY orders (via special CANCEL signals)
        2. Sell remaining positions in batches, both sides
        3. Prioritise the winning (high-price) side — it has more value
        """
        if not self._exit_logged:
            remaining = "?"
            if self.market_settlement_time is not None:
                remaining = f"{self.market_settlement_time - datetime.now().timestamp():.0f}s"
            elif self.market_start_time is not None:
                elapsed = (datetime.now() - self.market_start_time).total_seconds()
                remaining = f"{self.market_duration - elapsed:.0f}s"
            up_sh = self.up_position.shares
            down_sh = self.down_position.shares
            logger.info(
                f"[{self.name}] EXIT PHASE: {remaining} to settlement | "
                f"UP={up_sh:.1f}sh DOWN={down_sh:.1f}sh | selling all"
            )
            self._exit_logged = True

        now_ts = time.time()
        if now_ts - self._last_exit_sell_time < self.exit_sell_interval:
            return []

        signals = self._generate_exit_signals(price_data)
        if signals:
            self._last_exit_sell_time = now_ts
        return signals

    def _generate_exit_signals(self, price_data: PriceData) -> List[OrderSignal]:
        """Generate sell signals to liquidate all positions.

        Key principle: when the hedged position is profitable (ECR < 1.0),
        sell BOTH sides proportionally to preserve the hedge and lock in
        profit.  The old approach of checking per-side profitability would
        only sell the winning side, destroying the hedge and leaving the
        losing side to become worthless at settlement.

        When ECR >= 1.0 (unprofitable), fall back to per-side profitability
        checks — only sell what doesn't lock in a loss.
        """
        signals: List[OrderSignal] = []

        if self.market_settlement_time is not None:
            remaining = max(1.0, self.market_settlement_time - datetime.now().timestamp())
        elif self.market_start_time is not None:
            elapsed = (datetime.now() - self.market_start_time).total_seconds()
            remaining = max(1.0, self.market_duration - elapsed)
        else:
            remaining = self.exit_lead_seconds

        urgency = 1.0 - min(1.0, remaining / self.exit_lead_seconds)
        discount = 0.005 + urgency * 0.025

        sell_fraction = min(0.60, 0.30 + urgency * 0.30)

        ecr = self.effective_cost_rate
        hedge_profitable = ecr != float("inf") and ecr < 1.0
        combined_price = price_data.up_price + price_data.down_price
        pair_profitable = combined_price > ecr if ecr != float("inf") else False

        late_phase = remaining <= self.exit_hold_winner_seconds

        # For balanced positions, holding to settlement gives a deterministic
        # outcome: PnL = min(up, down) × $1 - total_cost.  Exit selling the
        # winning side at market × (1-discount) < $1.00 always produces a
        # worse result.  Skip exit sell and let redemption handle it.
        balance = self.balance_ratio
        if balance > 0.5 and ecr != float("inf") and ecr < 1.15:
            return signals

        # When the hedge is profitable, sell both sides proportionally.
        # This preserves the hedge instead of only selling the winning side.
        if hedge_profitable and pair_profitable and not late_phase:
            hedged = min(self.up_position.shares, self.down_position.shares)
            if hedged >= self.min_order_shares:
                sell_shares = max(self.min_order_shares, hedged * sell_fraction)
                sell_shares = min(sell_shares, hedged)

                for side, pos, market_price in [
                    ("up", self.up_position, price_data.up_price),
                    ("down", self.down_position, price_data.down_price),
                ]:
                    if pos.shares < self.min_order_shares:
                        continue
                    sell_price = max(market_price * (1 - discount), 0.01)
                    if sell_price < 0.02:
                        continue
                    size = min(sell_shares, pos.shares)
                    token_type = TokenType.YES if side == "up" else TokenType.NO
                    signals.append(OrderSignal(
                        side=TradeSide.SELL,
                        token_type=token_type,
                        target_price=sell_price,
                        size=size,
                    ))
                    logger.info(
                        f"[{self.name}] EXIT PAIR-SELL: {side.upper()} "
                        f"{size:.1f}/{pos.shares:.1f}sh @{sell_price:.3f} "
                        f"(market={market_price:.3f}, ECR={ecr:.4f}, "
                        f"combined={combined_price:.3f}, remaining={remaining:.0f}s)"
                    )
                return signals

        # Fallback: per-side exit for unhedged or unprofitable positions
        sides = [
            ("up", self.up_position, price_data.up_price),
            ("down", self.down_position, price_data.down_price),
        ]
        sides.sort(key=lambda x: x[2])

        for side, pos, market_price in sides:
            if pos.shares < self.min_order_shares:
                continue

            sell_price = max(market_price * (1 - discount), 0.01)

            if sell_price < 0.02:
                continue

            if late_phase:
                if market_price > 0.50:
                    logger.debug(
                        f"[{self.name}] EXIT HOLD: {side.upper()} "
                        f"price={market_price:.3f} > 0.50, "
                        f"holding for $1 settlement ({remaining:.0f}s left)"
                    )
                    continue
            else:
                if not hedge_profitable and sell_price <= pos.avg_price:
                    logger.debug(
                        f"[{self.name}] EXIT SKIP: {side.upper()} "
                        f"sell={sell_price:.3f} <= cost={pos.avg_price:.3f}, "
                        f"waiting ({remaining:.0f}s left)"
                    )
                    continue

            sell_size = max(self.min_order_shares, pos.shares * sell_fraction)
            sell_size = min(sell_size, pos.shares)

            token_type = TokenType.YES if side == "up" else TokenType.NO
            signals.append(OrderSignal(
                side=TradeSide.SELL,
                token_type=token_type,
                target_price=sell_price,
                size=sell_size,
            ))
            force_tag = " [HEDGE-FORCE]" if hedge_profitable and sell_price <= pos.avg_price else ""
            logger.info(
                f"[{self.name}] EXIT SELL{force_tag}: {side.upper()} "
                f"{sell_size:.1f}/{pos.shares:.1f}sh @{sell_price:.3f} "
                f"(market={market_price:.3f}, cost={pos.avg_price:.3f}, "
                f"ECR={ecr:.4f}, discount={discount:.1%}, remaining={remaining:.0f}s)"
            )

        return signals

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
        
        skip_urgency = False
        
        # === Trend-adaptive pricing ===
        # In skewed markets, price asymmetrically based on trend confidence:
        #
        # Low confidence (< 30%): proportional (same % discount both sides)
        # Medium confidence (30-70%): asymmetric allocation (current logic)
        # High confidence (70%+, Phase 2+): maker-aggressive on trending side
        #   → Trending side: 0.5% below market (fills on bid-ask bounce)
        #   → Other side: absorbs ALL remaining discount (rarely fills)
        #   → Pair sum = effective_target (unchanged)
        #
        # This prevents the asymmetric fill problem: in an uptrend, DOWN
        # fills easily at its discounted price while UP never fills, creating
        # dangerous one-sided exposure to the losing side.
        if max_price > self.trend_patience_threshold:
            if up_price >= down_price:
                dominant_price, minority_price = up_price, down_price
                dominant_is_up = True
            else:
                dominant_price, minority_price = down_price, up_price
                dominant_is_up = False
            
            skew_intensity = (max_price - self.trend_patience_threshold) / \
                             (self.max_skew_threshold - self.trend_patience_threshold)
            skew_intensity = min(1.0, max(0.0, skew_intensity))
            
            # Check if trend is strong enough AND confirmed for maker-aggressive
            phase = self.get_market_phase()
            trend_side, trend_confidence = (
                self._trend_detector.get_trend()
                if self.enable_trend_detection else ("neutral", 0.0)
            )
            dominant_side = "up" if dominant_is_up else "down"
            trend_confirmed = (
                trend_confidence >= 0.70
                and trend_side == dominant_side
                and phase >= 2
            )
            
            if trend_confirmed:
                skip_urgency = True
                # High-confidence trend: maker-aggressive on trending side.
                # 0.5% below market fills on normal bid-ask bounce within
                # seconds, even in a strong trend.  All remaining discount
                # goes to the declining side, making it very conservative.
                maker_discount = 0.005
                dominant_limit = dominant_price * (1 - maker_discount)
                minority_limit = effective_target - dominant_limit
                # Floor: minority must stay above low_prob_threshold
                if minority_limit < self.low_prob_threshold:
                    minority_limit = self.low_prob_threshold
                    dominant_limit = effective_target - minority_limit
            else:
                # Medium-confidence: asymmetric allocation (original logic)
                dominant_share = 0.50 - 0.30 * skew_intensity
                dominant_offset = total_offset * dominant_share
                minority_offset = total_offset * (1.0 - dominant_share)
                dominant_limit = dominant_price - dominant_offset
                minority_limit = minority_price - minority_offset
            
            if dominant_is_up:
                up_limit = dominant_limit
                down_limit = minority_limit
            else:
                down_limit = dominant_limit
                up_limit = minority_limit
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
        # Skip urgency when maker-aggressive pricing is active: urgency pushes
        # both limits toward market, then the hard ceiling scales them down
        # proportionally — redistributing discount away from the trending side,
        # which defeats the purpose of maker-aggressive pricing.
        if self.enable_urgency_pricing and not skip_urgency:
            urgency = self._calculate_combined_urgency()
            
            if urgency > 0:
                # Calculate offset from market price (using current limits)
                up_offset = up_price - up_limit
                down_offset = down_price - down_limit
                
                # Reduce offset based on urgency (up to urgency_price_factor reduction)
                offset_reduction = urgency * self.urgency_price_factor
                
                up_limit = up_limit + up_offset * offset_reduction
                down_limit = down_limit + down_offset * offset_reduction
                
                # Lagging side boost: move closer to market price (but capped
                # at 1% below market to preserve arbitrage margin)
                lagging_side = self.get_lagging_side()
                if urgency > 0.7 and lagging_side != "balanced":
                    if lagging_side == "up":
                        up_limit = min(up_price * 0.99, up_limit + (up_price - up_limit) * 0.3)
                    else:
                        down_limit = min(down_price * 0.99, down_limit + (down_price - down_limit) * 0.3)
        
        # === Fill-rate adaptive skew ===
        # Redistribute discount between sides based on observed fill rates.
        # If UP fills easily but DOWN keeps timing out, move DOWN's limit
        # closer to market (tighter) and UP's limit further (wider).
        # This keeps the pair sum unchanged while making the hard-to-fill
        # side more likely to execute.
        fill_skew = self._get_fill_rate_skew()
        if abs(fill_skew) > 0.05:
            up_offset = up_price - up_limit
            down_offset = down_price - down_limit
            total_offset = up_offset + down_offset
            if total_offset > 0.001:
                # fill_skew > 0 means UP fills more → tighten DOWN
                shift = total_offset * fill_skew * 0.5
                up_limit -= shift      # wider (more discount) for easy side
                down_limit += shift    # tighter (less discount) for hard side

        # === Late-game Binance directional bias ===
        # When Binance price shows a strong directional move in the final
        # portion of the market, shift the predicted winning side closer
        # to market (higher fill probability) at the expense of the losing
        # side.  Pair sum remains <= effective_target (enforced by ceiling).
        dir_signal = self._get_directional_signal()
        if dir_signal is not None:
            signal_dir, signal_conf = dir_signal
            up_offset = up_price - up_limit
            down_offset = down_price - down_limit
            total_off = up_offset + down_offset
            if total_off > 0.001:
                shift = total_off * signal_conf * 0.30
                if signal_dir == "up":
                    up_limit += shift
                    down_limit -= shift
                else:
                    down_limit += shift
                    up_limit -= shift
                if not self._directional_signal_logged:
                    self._directional_signal_logged = True
                    logger.info(
                        f"[{self.name}] Directional signal: {signal_dir} "
                        f"conf={signal_conf:.2f} delta={self.binance_delta:.4f}"
                    )

        # Safety floor: never go below low_prob_threshold (e.g. 0.05).
        # Orders below this price are not worth placing — tokens at <5% are
        # nearly worthless and carry extreme settlement risk.
        up_limit = max(self.low_prob_threshold, up_limit)
        down_limit = max(self.low_prob_threshold, down_limit)
        
        # HARD CEILING: urgency/trend adjustments must never erode the pair
        # discount beyond effective_target.  This is the #1 cause of ECR > 1.0:
        # urgency pricing would push limits close to market, making pair cost
        # approach 1.0 and guaranteeing losses.
        pair_cost = up_limit + down_limit
        if pair_cost > effective_target:
            scale = effective_target / pair_cost
            up_limit *= scale
            down_limit *= scale
        
        # === ECR-aware counterpart cap ===
        # Balance-aware: use relaxed cap when position is imbalanced to
        # encourage the 4th fill that completes the hedge.  Switch to strict
        # cap once balanced (≥85%) to prevent further ECR inflation.
        #
        # Data shows: 4-fill no-sell markets avg +$0.039 (profitable).
        #             3-fill markets avg -$0.529 (all lose, all have sells).
        # The strict cap after 3 fills blocks the 4th fill, triggering
        # sell-rebalance which locks in ECR > 1.0.  The relaxed cap at 1.10
        # allows the 4th fill while limiting worst-case ECR.
        #
        # Exception: directional recovery side is exempt (uses EV-based pricing).
        max_imbalanced_ecr = 1.10
        dr_side = self._directional_recovery_side
        has_up = self.up_position.shares > 0
        has_down = self.down_position.shares > 0
        bal = self.balance_ratio
        cap = effective_target if bal >= 0.85 else max_imbalanced_ecr
        if has_up and dr_side != "down":
            up_avg = self.up_position.avg_price
            max_down = cap - up_avg
            if max_down > 0.01 and down_limit > max_down:
                down_limit = max_down
        if has_down and dr_side != "up":
            down_avg = self.down_position.avg_price
            max_up = cap - down_avg
            if max_up > 0.01 and up_limit > max_up:
                up_limit = max_up
        
        # === Directional recovery: aggressive pricing ===
        # When betting on the probable winner, the hedging-based limit price
        # (3-5% below market) is too conservative — it requires a pullback
        # to fill, which contradicts the confirmed uptrend.  Instead, use
        # maker-aggressive pricing: 0.5% below market.  This sits just below
        # the ask and fills on normal bid-ask bounce within seconds.
        # Still a maker order (0% fee), but much more likely to fill.
        if dr_side == "up":
            up_limit = max(up_price * 0.995, self.low_prob_threshold)
        elif dr_side == "down":
            down_limit = max(down_price * 0.995, self.low_prob_threshold)

        # Enforce minimum maker discount: limit must be at least min_maker_discount
        # below market price to avoid crossing the spread and filling as taker.
        # Directional recovery is exempt (it intentionally sits close to market).
        if self.min_maker_discount > 0:
            if dr_side != "up":
                maker_ceiling_up = up_price * (1 - self.min_maker_discount)
                if up_limit > maker_ceiling_up:
                    up_limit = maker_ceiling_up
            if dr_side != "down":
                maker_ceiling_down = down_price * (1 - self.min_maker_discount)
                if down_limit > maker_ceiling_down:
                    down_limit = maker_ceiling_down
        
        # === Fill urgency mode: aggressive pricing for faster fills ===
        # When enabled, move limit prices closer to market to improve fill rate.
        # This is important for live trading where maker orders may not fill
        # if the market moves away from the limit price.
        # 
        # Strategy: reduce the discount (distance from market) by the urgency factor.
        # Example: if limit is 5% below market and urgency_factor=0.5,
        # new limit is only 2.5% below market.
        if self.fill_urgency_mode:
            up_discount = up_price - up_limit
            down_discount = down_price - down_limit
            
            # Reduce discount by urgency factor (0.5 = half the distance to market)
            up_limit = up_price - up_discount * self.urgency_discount_factor
            down_limit = down_price - down_discount * self.urgency_discount_factor
            
            # Ensure we still have some discount (don't cross to taker)
            min_discount = 0.005  # 0.5% minimum discount
            up_limit = min(up_limit, up_price * (1 - min_discount))
            down_limit = min(down_limit, down_price * (1 - min_discount))

        # FINAL hard ceiling: post-urgency adjustments (fill_urgency_mode,
        # directional recovery) can push pair cost above effective_target.
        # Re-enforce so ECR stays below 1.0 even after sell-to-rebalance.
        final_pair = up_limit + down_limit
        if final_pair > effective_target:
            final_scale = effective_target / final_pair
            up_limit *= final_scale
            down_limit *= final_scale

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

        # --- 0. Abandon mode: no orders at all -----------------------------------
        if self._abandon_mode:
            return False

        # --- 0b. Realized ECR hard gate: if filled position is already losing,
        #     only allow orders on the lagging side (recovery) ----------------
        recr = self.realized_ecr
        if recr != float("inf") and recr > self.ecr_threshold:
            lagging = self.get_lagging_side()
            if side != lagging and lagging != "balanced":
                return False

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

        # --- 5b. Directional recovery (ECR > 1.0, balanced, strong trend) --------
        directional_result = self._check_directional_recovery(
            side, up, down, phase, limit_price, order_cost, current_ecr, market_price
        )
        if directional_result is not None:
            return directional_result

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
        """Block overweight-side orders when imbalance exceeds the cap."""
        if up > 0 and down > 0:
            max_s, min_s = max(up, down), min(up, down)
            ratio = max_s / min_s if min_s > 0 else float("inf")
            overweight = "up" if up > down else "down"
            cap = self.directional_recovery_max_ratio if self._directional_recovery_side == side else 2.0
            if ratio > cap and side == overweight:
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
                    return False

        single_threshold = self._get_phase1_ecr_limit() if phase == 1 else 1.0
        if predicted_ecr != float("inf") and predicted_ecr >= single_threshold:
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
            return False

        if side == minority:
            return True

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
                return False
            if predicted_ecr >= current_ecr:
                return False
            return True

        if predicted_ecr < current_ecr:
            return True

        return False

    def _evaluate_directional_recovery(self) -> Optional[str]:
        """
        Determine if directional recovery should activate and which side to bet on.
        
        Called when ECR > 1.0 with balanced shares (rebalancing won't help).
        Returns the side to buy ("up"/"down") or None if conditions aren't met.
        
        Conditions:
        - Probable winner's price >= directional_recovery_min_prob (default 0.62)
        - Trend confidence >= directional_recovery_min_trend (default 0.40)
        - Trend direction matches the probable winner (not a reversal)
        """
        if not self.enable_trend_detection:
            return None
        if self.current_up_price is None or self.current_down_price is None:
            return None
        
        probable_winner = "up" if self.current_up_price >= self.current_down_price else "down"
        win_prob = max(self.current_up_price, self.current_down_price)
        
        if win_prob < self.directional_recovery_min_prob:
            return None
        
        trend_side, trend_confidence = self._trend_detector.get_trend()
        
        # Trend must confirm the probable winner AND be strong enough
        if trend_side != probable_winner or trend_confidence < self.directional_recovery_min_trend:
            return None
        
        if not self._directional_recovery_logged:
            logger.info(
                f"[{self.name}] DIRECTIONAL: ECR>{self.effective_cost_rate:.2%}, "
                f"balanced, betting {probable_winner.upper()} "
                f"(prob={win_prob:.0%}, trend={trend_confidence:.0%})"
            )
            self._directional_recovery_logged = True
        
        return probable_winner
    
    def _check_directional_recovery(self, side: str, up: float, down: float,
                                    phase: int, limit_price: float, order_cost: float,
                                    current_ecr: float, market_price: float
                                    ) -> Optional[bool]:
        """
        Gate for directional recovery orders.
        
        When ECR > 1.0 and shares are balanced, buying the probable winner at
        below-market price has positive per-share EV even without hedging.
        
        Guards:
        - Only the probable winner side (set by _evaluate_directional_recovery)
        - Positive per-share EV: limit_price < market_price (probability)
        - Position cap: don't exceed directional_recovery_max_ratio of minority shares
        
        Returns True/False for directional orders, None to continue normal flow.
        """
        if self._directional_recovery_side is None:
            return None  # Not in directional recovery mode
        
        if side != self._directional_recovery_side:
            return False  # Block the losing side
        
        # Positive EV check: we pay limit_price, expected return is market_price
        # (which approximates the win probability in binary markets).
        # EV per share = market_price - limit_price > 0
        if limit_price >= market_price:
            return False
        
        # Position cap: don't go too heavy directional (include pending orders)
        min_shares = min(up, down) if up > 0 and down > 0 else 0
        max_shares_allowed = min_shares * self.directional_recovery_max_ratio if min_shares > 0 else 0
        pending_side = sum(o.shares for o in self.pending_orders if o.side == side)
        current_side_total = (up if side == "up" else down) + pending_side
        new_shares = order_cost / limit_price if limit_price > 0 else 0
        
        if max_shares_allowed > 0 and current_side_total + new_shares > max_shares_allowed:
            return False
        
        return True

    def _check_ecr_protection(self, side: str, up: float, down: float,
                              phase: int, limit_price: float, order_cost: float,
                              current_ecr: float, predicted_ecr: float) -> bool:
        """ECR-based order rejection for Phase 1 vs Phase 2-3."""
        if phase == 1:
            cap = self._get_phase1_ecr_limit()
            if predicted_ecr >= cap:
                return False
        else:
            improves_balance = (side == "up" and up < down) or (side == "down" and down < up)

            if predicted_ecr >= 1.0:
                if improves_balance and current_ecr > 1.0 and predicted_ecr < current_ecr:
                    pass  # Allow: recovering from bad ECR
                else:
                    return False

            ecr_tolerance = 0.01
            if predicted_ecr > current_ecr + ecr_tolerance:
                return False

        return True

    def _check_phase3(self, side: str, market_price: float,
                      current_ecr: float, predicted_ecr: float) -> bool:
        """
        Phase 3 settlement protection with directional tilt.
        
        Two modes:
        
        1. Hedging mode (default): protect existing position.
           - Allow ECR-improving orders on lagging side only.
        
        2. Directional tilt (enable_phase3_tilt=True): buy the probable winner
           even if it creates imbalance. Near settlement the expected value of
           buying the high-probability side below market price is positive:
             EV = prob_win × $1 - cost_per_share
           So if UP is at 0.82 and we buy at 0.80, EV = 0.82 × $1 - $0.80 = +$0.02
           This works even WITHOUT hedging from the other side.
        """
        if market_price < self.low_prob_threshold:
            logger.debug(f"[{self.name}] Phase 3: low prob {side.upper()} ({market_price:.1%})")
            return False

        lagging = self.get_lagging_side()

        # --- Directional tilt: buy the probable winner ---
        if self.enable_phase3_tilt and self.current_up_price and self.current_down_price:
            probable_winner = "up" if self.current_up_price >= self.current_down_price else "down"
            win_prob = max(self.current_up_price, self.current_down_price)

            if side == probable_winner and win_prob >= self.phase3_tilt_min_prob:
                # Positive EV check: price we pay must be < probability of winning
                limit_price = market_price  # approximate; actual limit set by caller
                ev = win_prob - limit_price
                if ev > 0:
                    if not self._phase3_tilt_logged:
                        logger.info(
                            f"[{self.name}] P3 TILT: {side.upper()} prob={win_prob:.0%} EV=+{ev:.3f}"
                        )
                        self._phase3_tilt_logged = True
                    return True
        # --- Standard hedging logic ---
        if current_ecr < 1.0:
            if predicted_ecr < current_ecr and side == lagging:
                return True
            return False

        if predicted_ecr >= current_ecr:
            return False

        if lagging != "balanced" and side != lagging:
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

    def _create_signal(self, side: str, price: float, cost: float) -> Optional[OrderSignal]:
        """Create an order signal, enforcing minimum share count."""
        token_type = TokenType.YES if side == "up" else TokenType.NO
        trade_side = TradeSide.BUY
        size = cost / price

        # Enforce Polymarket minimum order size (shares).
        # If the calculated shares are below the minimum, scale up the cost
        # to produce exactly min_order_shares. The caller must have enough budget.
        if size < self.min_order_shares:
            required_cost = self.min_order_shares * price
            if required_cost > cost * 3:
                return None
            size = float(self.min_order_shares)
            cost = size * price

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
            f"UP={up_shares:.0f} DOWN={down_shares:.0f} | "
            f"Cost=${total_cost:.2f} Val=${final_value:.2f} PnL=${pnl:+.2f}"
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
