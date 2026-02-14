"""
Live Simulation Runner - Real-time multi-market paper trading.

This module implements:
- Continuous market discovery across BTC, ETH, SOL
- Strategy execution with simulated order fills
- Metrics collection and state persistence
- Configurable runtime duration
"""

import asyncio
import json
import os
import re
import signal
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from polymoney.core.logging import get_logger
from polymoney.core.models import OrderSignal, PriceData, TokenType, TradeSide
from polymoney.data.real_data_fetcher import (
    RealDataFetcher,
    MarketEvent,
    MarketEventData,
    PriceUpdate,
)
from polymoney.data.websocket_client import WebSocketManager
from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy

logger = get_logger("simulation.live_runner")


# ---------------------------------------------------------------------------
# Polymarket fee model
# ---------------------------------------------------------------------------
# Most Polymarket markets are fee-free.  15-min crypto markets charge a
# TAKER-only fee that peaks at 1.56% effective rate when the share price
# is near $0.50 and declines to ~0% at the extremes ($0.01 and $0.99).
# MAKERS (limit orders sitting on the book) pay NO fee and receive a
# daily 20% rebate from the collected taker fees.
#
# Since our strategy places limit orders (maker), normal fills are fee-free.
# Only rebalancing "market" orders (which cross the spread as taker) incur
# fees.  For buy taker orders, the fee is collected in shares — fewer shares
# are received.
#
# Official fee table:
#   https://docs.polymarket.com/polymarket-learn/trading/maker-rebates-program
#
# The approximation below uses (4*p*(1-p))^1.8 * MAX_FEE, which fits the
# official table within ~0.1% at all price points.
# ---------------------------------------------------------------------------

_TAKER_FEE_MAX = 0.0156   # 1.56% max effective rate at p=0.50
_TAKER_FEE_EXPONENT = 1.8  # Steepness of the decline toward extremes


def calculate_taker_fee_rate(price: float) -> float:
    """
    Calculate the effective taker fee rate for a Polymarket 15-min crypto market.

    Args:
        price: Share price (0.0 to 1.0).

    Returns:
        Effective fee rate as a fraction (e.g. 0.0156 = 1.56%).
        Multiply by trade value to get absolute fee in USDC.
    """
    if price <= 0.01 or price >= 0.99:
        return 0.0
    parabolic = 4.0 * price * (1.0 - price)  # Peaks at 1.0 when price=0.50
    return _TAKER_FEE_MAX * (parabolic ** _TAKER_FEE_EXPONENT)


def apply_taker_fee_to_shares(shares: float, fill_price: float) -> float:
    """
    Apply taker fee to a buy order, reducing shares received.

    On Polymarket, buy taker fees are collected in shares:
    you pay the full USDC amount but receive fewer shares.

    Args:
        shares: Shares before fee deduction.
        fill_price: Average fill price per share.

    Returns:
        Shares after fee deduction.
    """
    fee_rate = calculate_taker_fee_rate(fill_price)
    return shares * (1.0 - fee_rate)


@dataclass
class OrderbookSnapshot:
    """
    Cached orderbook depth for a single token.
    
    Stores bid/ask levels sorted by price, with methods
    to simulate realistic order fills using available liquidity.
    """
    
    # Levels as (price, size) tuples
    bids: List[Tuple[float, float]] = field(default_factory=list)  # Sorted desc by price
    asks: List[Tuple[float, float]] = field(default_factory=list)  # Sorted asc by price
    timestamp: float = 0.0
    
    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None
    
    @property
    def spread(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return 0.0
    
    @property
    def is_valid(self) -> bool:
        """Check if orderbook has both sides and is not too stale (60s)."""
        if not self.bids or not self.asks:
            return False
        return (time.time() - self.timestamp) < 60.0
    
    def total_bid_liquidity(self, down_to_price: float = 0.0) -> float:
        """Total bid-side liquidity (size) at or above down_to_price."""
        return sum(size for price, size in self.bids if price >= down_to_price)
    
    def total_ask_liquidity(self, up_to_price: float = 1.0) -> float:
        """Total ask-side liquidity (size) at or below up_to_price."""
        return sum(size for price, size in self.asks if price <= up_to_price)
    
    def simulate_buy_fill(
        self, size: float, limit_price: float, deplete: bool = True
    ) -> Optional[Tuple[float, float]]:
        """
        Simulate buying `size` shares at up to `limit_price` against the ask side.
        
        Walks ask levels from best (lowest) upward, accumulating fills
        until the order is filled or asks exceed the limit price.
        
        When deplete=True, filled liquidity is REMOVED from the orderbook,
        preventing the same liquidity from being consumed by subsequent fills.
        This is critical because the REST orderbook is a static snapshot —
        without depletion, the strategy can "eat" the same liquidity 50+ times
        in rapid succession, leading to massive one-sided positions.
        
        Args:
            size: Desired number of shares to buy
            limit_price: Maximum acceptable price per share
            deplete: If True, consumed liquidity is removed from the orderbook
        
        Returns:
            (avg_fill_price, filled_size) or None if no fill possible.
        """
        if not self.asks:
            return None
        
        filled = 0.0
        total_cost = 0.0
        levels_consumed: List[Tuple[int, float]] = []  # (index, amount_consumed)
        
        for i, (ask_price, ask_size) in enumerate(self.asks):
            if ask_price > limit_price:
                break
            
            fill_at_level = min(size - filled, ask_size)
            filled += fill_at_level
            total_cost += fill_at_level * ask_price
            levels_consumed.append((i, fill_at_level))
            
            if filled >= size - 0.001:  # Float tolerance
                break
        
        if filled <= 0:
            return None
        
        # Deplete consumed liquidity from the orderbook
        if deplete and levels_consumed:
            new_asks = list(self.asks)
            # Process in reverse to preserve indices
            for idx, consumed in reversed(levels_consumed):
                price, remaining = new_asks[idx]
                remaining -= consumed
                if remaining <= 0.01:  # Effectively empty
                    new_asks.pop(idx)
                else:
                    new_asks[idx] = (price, remaining)
            self.asks = new_asks
        
        avg_price = total_cost / filled
        return (avg_price, min(filled, size))

    @classmethod
    def from_raw(cls, raw_book: Dict[str, Any]) -> "OrderbookSnapshot":
        """
        Parse a raw orderbook dict from the CLOB API or WebSocket.
        
        Handles both dict-style levels ({"price": "0.50", "size": "100"})
        and list-style levels ([0.50, 100]).
        """
        bids: List[Tuple[float, float]] = []
        asks: List[Tuple[float, float]] = []
        
        for raw_level in raw_book.get("bids", []):
            price, size = cls._parse_level(raw_level)
            if price is not None and size is not None and size > 0:
                bids.append((price, size))
        
        for raw_level in raw_book.get("asks", []):
            price, size = cls._parse_level(raw_level)
            if price is not None and size is not None and size > 0:
                asks.append((price, size))
        
        # Sort: bids descending by price, asks ascending by price
        bids.sort(key=lambda x: x[0], reverse=True)
        asks.sort(key=lambda x: x[0])
        
        return cls(bids=bids, asks=asks, timestamp=time.time())
    
    @staticmethod
    def _parse_level(level) -> Tuple[Optional[float], Optional[float]]:
        """Parse a single orderbook level into (price, size)."""
        try:
            if isinstance(level, dict):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                return (price, size)
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                return (float(level[0]), float(level[1]))
        except (ValueError, TypeError):
            pass
        return (None, None)


@dataclass
class SimulationConfig:
    """Configuration for live simulation."""

    # Markets to monitor
    coins: List[str] = field(default_factory=lambda: ["btc", "eth", "sol"])

    # Runtime duration (0 = infinite)
    duration_seconds: float = 0

    # Output paths
    output_dir: Path = field(default_factory=lambda: Path("simulation_results"))

    # Strategy parameters
    target_cost: float = 0.96
    batch_ratio: float = 0.001  # Order size = position_size × batch_ratio
    position_size: float = 100.0  # Max position per market
    ecr_threshold: float = 1.05
    enable_ecr_stoploss: bool = True
    enable_rebalancing: bool = True
    enable_trend_detection: bool = True
    enable_urgency_pricing: bool = True

    # Simulation parameters
    market_scan_interval: float = 60.0   # 1 minute - HTTP scan for new markets
    metrics_output_interval: float = 1.0  # 1 second

    # Order management parameters
    order_timeout: float = 30.0       # Cancel pending orders after 30 seconds
    stale_order_threshold: float = 0.20  # Cancel orders when market moves 20% away (binary markets swing 10%+/s)

    # Market validity parameters
    min_trading_time: float = 300.0  # 5 minutes minimum before settlement
    min_price_threshold: float = 0.05  # Skip markets where one side < 5%
    max_entry_skew: float = 0.75     # Skip markets where max(up, down) > 75% at entry

    # Global fund management parameters
    max_concurrent_markets: int = 6       # Max markets trading simultaneously
    max_total_exposure: float = 500.0     # Max total cost across all markets

    @classmethod
    def parse_duration(cls, duration_str: str) -> float:
        """
        Parse duration string to seconds.

        Formats: "10h" (hours), "3d" (days), "0" (infinite), "3600" (seconds)
        """
        if not duration_str or duration_str == "0":
            return 0

        duration_str = duration_str.strip().lower()

        # Match patterns like "10h", "3d", "30m"
        match = re.match(r'^(\d+(?:\.\d+)?)\s*([hdms]?)$', duration_str)
        if match:
            value = float(match.group(1))
            unit = match.group(2) or 's'

            multipliers = {
                's': 1,
                'm': 60,
                'h': 3600,
                'd': 86400,
            }
            return value * multipliers.get(unit, 1)

        # Try parsing as raw seconds
        try:
            return float(duration_str)
        except ValueError:
            raise ValueError(f"Invalid duration format: {duration_str}")


@dataclass
class MarketResult:
    """Result of a single market simulation."""

    slug: str
    coin: str
    condition_id: str
    winner: Optional[str] = None

    # Positions
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0

    # Orders
    orders_submitted: int = 0
    orders_filled: int = 0

    # Final metrics
    pnl: float = 0.0
    roi: float = 0.0
    ecr: float = 0.0
    balance_ratio: float = 0.0

    # Timestamps
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.up_cost + self.down_cost

    @property
    def is_complete(self) -> bool:
        return self.winner is not None

    def calculate_final_metrics(self) -> None:
        """Calculate final PnL, ROI, ECR after settlement."""
        if not self.winner:
            return

        total_cost = self.total_cost
        if total_cost == 0:
            return

        # Settlement value
        if self.winner == "up":
            settlement_value = self.up_shares
        else:
            settlement_value = self.down_shares

        self.pnl = settlement_value - total_cost
        self.roi = self.pnl / total_cost if total_cost > 0 else 0

        # ECR and balance
        min_shares = min(self.up_shares, self.down_shares)
        max_shares = max(self.up_shares, self.down_shares)

        self.ecr = total_cost / min_shares if min_shares > 0 else float('inf')
        self.balance_ratio = min_shares / max_shares if max_shares > 0 else 0


@dataclass
class SimulationStats:
    """Aggregate statistics for the simulation."""

    start_time: float = 0.0
    last_update: float = 0.0

    # Counts
    markets_processed: int = 0
    markets_won: int = 0
    markets_lost: int = 0

    # Aggregate metrics
    total_pnl: float = 0.0
    total_cost: float = 0.0
    total_roi: float = 0.0

    # Running calculations for Sharpe
    returns: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.markets_processed == 0:
            return 0.0
        return self.markets_won / self.markets_processed

    @property
    def sharpe_ratio(self) -> float:
        """Calculate Sharpe ratio from returns."""
        if len(self.returns) < 2:
            return 0.0

        import statistics
        mean_return = statistics.mean(self.returns)
        std_return = statistics.stdev(self.returns)

        if std_return == 0:
            return 0.0

        # Annualize assuming 15-minute periods
        # ~35000 periods per year
        annualization_factor = (35000 ** 0.5)
        return (mean_return / std_return) * annualization_factor

    def add_result(self, result: MarketResult) -> None:
        """Add a market result to aggregate stats."""
        self.markets_processed += 1

        if result.pnl > 0:
            self.markets_won += 1
        else:
            self.markets_lost += 1

        self.total_pnl += result.pnl
        self.total_cost += result.total_cost

        if result.total_cost > 0:
            self.returns.append(result.roi)

        self.total_roi = self.total_pnl / self.total_cost if self.total_cost > 0 else 0
        self.last_update = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "start_time": self.start_time,
            "last_update": self.last_update,
            "markets_processed": self.markets_processed,
            "markets_won": self.markets_won,
            "markets_lost": self.markets_lost,
            "win_rate": self.win_rate,
            "total_pnl": self.total_pnl,
            "total_cost": self.total_cost,
            "total_roi": self.total_roi,
            "sharpe_ratio": self.sharpe_ratio,
        }


class MarketSimulation:
    """Manages simulation for a single market."""

    def __init__(
        self,
        market: Dict[str, Any],
        config: SimulationConfig,
    ):
        self.market = market
        self.config = config
        self.slug = market["slug"]
        self.coin = market.get("coin", "btc")

        # Initialize strategy with params dict
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
            strategy_id=f"sim_{self.slug}",
            name=f"Simulation {self.slug}",
            position_size=config.position_size,
            params=strategy_params,
        )

        # Initialize result
        self.result = MarketResult(
            slug=self.slug,
            coin=self.coin,
            condition_id=market.get("condition_id", ""),
            start_time=time.time(),
        )

        # Track pending orders (each has a unique 'order_id' key)
        self.pending_orders: List[Dict[str, Any]] = []
        self._next_order_id: int = 0

        # Last price and timestamp for staleness detection
        self.last_up_price: Optional[float] = None
        self.last_down_price: Optional[float] = None
        self.last_up_spread: float = 0.0   # Last known spread for UP token
        self.last_down_spread: float = 0.0  # Last known spread for DOWN token
        self.last_price_update_time: float = 0.0  # time.time() of last price update
        self.price_stale_threshold: float = 10.0  # seconds before prices are considered stale
        
        # WS warmup gate: don't create orders until both sides have confirmed WS prices.
        # This prevents the "initial burst" problem where REST orderbook data or WS
        # garbage (bid=0.01 ask=0.99) causes orders at wrong prices, leading to
        # one-sided fills and ECR deadlocks.
        self._ws_confirmed_up: bool = False
        self._ws_confirmed_down: bool = False
        self._ws_warmup_logged: bool = False
        
        # Order generation tick cooldown: WS sends 20+ price updates/second.
        # Without cooldown, the strategy generates 40+ orders/second, filling
        # against the static REST orderbook instantly and spending the entire
        # budget in 2 seconds (one-sided). With 1-second cooldown, the market
        # has time to move between orders, preventing rapid one-sided accumulation.
        self._last_order_tick_time: float = 0.0
        self._order_tick_interval: float = 1.0  # seconds between order generations

        # Orderbook depth cache (per token side)
        self.up_orderbook: Optional[OrderbookSnapshot] = None
        self.down_orderbook: Optional[OrderbookSnapshot] = None

    def process_price_update(self, price: PriceUpdate) -> None:
        """Process a price update and generate/fill orders."""
        up_token_id = self.market.get("up_token_id")
        down_token_id = self.market.get("down_token_id")

        # Update prices and mark WS side as confirmed
        if price.token_id == up_token_id:
            self.last_up_price = price.mid_price
            self.last_up_spread = price.spread if price.spread else 0.0
            self.last_price_update_time = time.time()
            if not self._ws_confirmed_up:
                self._ws_confirmed_up = True
                logger.info(
                    f"[{self.slug}] WS UP price confirmed: {price.mid_price:.4f} "
                    f"(bid={price.best_bid:.4f} ask={price.best_ask:.4f})"
                )
            logger.debug(f"[{self.slug}] UP price updated: {price.mid_price:.4f}")
        elif price.token_id == down_token_id:
            self.last_down_price = price.mid_price
            self.last_down_spread = price.spread if price.spread else 0.0
            self.last_price_update_time = time.time()
            if not self._ws_confirmed_down:
                self._ws_confirmed_down = True
                logger.info(
                    f"[{self.slug}] WS DOWN price confirmed: {price.mid_price:.4f} "
                    f"(bid={price.best_bid:.4f} ask={price.best_ask:.4f})"
                )
            logger.debug(f"[{self.slug}] DOWN price updated: {price.mid_price:.4f}")
        else:
            logger.warning(f"[{self.slug}] Unknown token_id: {price.token_id[:16]}... (up={up_token_id[:16] if up_token_id else 'None'}..., down={down_token_id[:16] if down_token_id else 'None'}...)")
            return

        if self.last_up_price is None or self.last_down_price is None:
            return

        # === WS warmup gate ===
        # Don't generate new orders until BOTH sides have confirmed WS prices.
        # This prevents the "initial burst" problem where stale/garbage data
        # causes orders at wrong prices → one-sided fills → ECR deadlock.
        # Existing pending orders are still checked for fills (in case WS data
        # confirms they're now fillable).
        ws_ready = self._ws_confirmed_up and self._ws_confirmed_down

        # Create price data for strategy
        price_data = PriceData(
            market_id=self.slug,
            timestamp=datetime.fromtimestamp(price.timestamp),
            up_price=self.last_up_price,
            down_price=self.last_down_price,
        )

        if ws_ready:
            if not self._ws_warmup_logged:
                logger.info(
                    f"[{self.slug}] WS warmup complete — both sides confirmed. "
                    f"UP={self.last_up_price:.4f} DOWN={self.last_down_price:.4f}. "
                    f"Starting order generation."
                )
                self._ws_warmup_logged = True

            # Tick cooldown: limit order generation to once per _order_tick_interval.
            # WS sends 20+ updates/second; without this, the strategy would generate
            # 40+ orders/second, filling against the static orderbook instantly.
            now = time.time()
            if now - self._last_order_tick_time >= self._order_tick_interval:
                self._last_order_tick_time = now
                
                # Get order signals from strategy
                signals = self.strategy.on_price_update(price_data)

                # Process each signal
                for signal in signals:
                    self._submit_order(signal, price_data)
        else:
            waiting_for = []
            if not self._ws_confirmed_up:
                waiting_for.append("UP")
            if not self._ws_confirmed_down:
                waiting_for.append("DOWN")
            logger.debug(
                f"[{self.slug}] WS warmup: waiting for {', '.join(waiting_for)} price confirmation"
            )

        # Check pending orders for fills (always, even during warmup)
        self._check_fills(price_data)

        # Force-sync strategy pending orders with runner's actual pending list
        # This fixes ghost entries caused by matching failures in cancel/fill
        self._sync_pending_orders()

    def update_orderbook(self, side: str, snapshot: OrderbookSnapshot) -> None:
        """
        Update cached orderbook for a token side.
        
        Args:
            side: "up" or "down"
            snapshot: Parsed OrderbookSnapshot
        """
        if side == "up":
            self.up_orderbook = snapshot
        else:
            self.down_orderbook = snapshot

    def _get_orderbook(self, side: str) -> Optional[OrderbookSnapshot]:
        """Get the cached orderbook for a side, or None if stale/missing."""
        book = self.up_orderbook if side == "up" else self.down_orderbook
        if book and book.is_valid:
            return book
        return None

    def _get_effective_spread(self, side: str) -> float:
        """
        Get effective spread tolerance using real orderbook data.
        
        Prefers orderbook depth data when available, falls back to
        WebSocket bid-ask spread, then conservative default.
        
        Returns:
            Effective spread tolerance as a fraction (e.g., 0.02 = 2%).
        """
        # Priority 1: Use real orderbook depth spread
        book = self._get_orderbook(side)
        if book and book.spread > 0:
            return max(book.spread, 0.005)
        
        # Priority 2: Use WebSocket bid-ask spread
        if side == "up" and self.last_up_spread > 0:
            real_spread = self.last_up_spread
        elif side == "down" and self.last_down_spread > 0:
            real_spread = self.last_down_spread
        else:
            # Conservative fallback when no real data available.
            # 3% is a realistic upper bound for 15-min crypto market spreads
            # on Polymarket (typical range 1-5%).  The previous 6% was too
            # generous and allowed almost all limit orders to fill instantly.
            real_spread = 0.03  # 3%
        
        # Minimum spread floor
        return max(real_spread, 0.005)  # At least 0.5%

    def _simulate_fill_with_depth(
        self, side: str, size: float, limit_price: float, market_price: float
    ) -> Optional[Tuple[float, float]]:
        """
        Simulate an order fill using orderbook depth when available.
        
        Walks the ask-side of the orderbook to determine realistic
        fill price and size considering available liquidity.
        Falls back to the simple spread-based model if no depth data.
        
        Args:
            side: "up" or "down"
            size: Order size (shares)
            limit_price: Limit price for the order
            market_price: Current mid/market price
        
        Returns:
            (fill_price, filled_size) or None if no fill.
        """
        book = self._get_orderbook(side)
        
        if book:
            # Staleness check: if the REST orderbook best_ask diverges too far from
            # the current WS market price, the depth data is stale and unreliable.
            # This is the root cause of one-sided fills: in a trending market, the
            # REST orderbook still shows asks at old (lower) prices for the rising side,
            # allowing "free" fills against phantom liquidity that no longer exists.
            # When divergence > 15%, fall through to the spread-based model which uses
            # the live WS price instead. (15% keeps depth model active longer in
            # volatile binary markets where 5-10% divergence is normal.)
            book_best_ask = book.best_ask
            if book_best_ask is not None:
                divergence = abs(book_best_ask - market_price) / market_price if market_price > 0 else 0
                if divergence > 0.15:
                    logger.debug(
                        f"[{self.slug}] Stale orderbook: {side.upper()} book_ask={book_best_ask:.4f} "
                        f"vs market={market_price:.4f} (div={divergence:.1%}). Using spread model."
                    )
                    book = None  # Force fallback to spread model
            
        if book:
            # Depth-based fill: walk the ask side of the orderbook
            result = book.simulate_buy_fill(size, limit_price)
            if result:
                avg_fill_price, filled_size = result
                # Add a small simulation noise (0.05% random component)
                import random
                noise = random.uniform(-0.0005, 0.0005)
                avg_fill_price = max(0.01, min(0.99, avg_fill_price * (1 + noise)))
                
                # Log depth-based fill
                bid_liq = book.total_bid_liquidity()
                ask_liq = book.total_ask_liquidity(limit_price)
                logger.debug(
                    f"[{self.slug}] Depth fill: {side.upper()} {filled_size:.1f}@{avg_fill_price:.4f} "
                    f"(limit={limit_price:.4f}, ask_liq={ask_liq:.0f}, bid_liq={bid_liq:.0f})"
                )
                return (avg_fill_price, filled_size)
            
            # Orderbook exists but limit price is below all asks - no fill
            return None
        
        # Fallback: spread-based model with probabilistic fills (no depth data)
        #
        # Real markets don't fill every limit order that's within the spread.
        # A limit order requires a TAKER to cross the spread and hit it.
        # Model: fill probability decreases as the limit price moves further
        # from the market price, and larger orders are harder to fill.
        import random
        
        spread_tolerance = self._get_effective_spread(side)
        price_diff = (market_price - limit_price) / market_price if market_price > 0 else 0
        
        # Size-based slippage: larger orders face worse fills
        order_value = size * limit_price
        size_slippage = min(0.005, order_value * 0.0001)  # Cap at 0.5%
        
        if limit_price >= market_price:
            # Aggressive (taker) order — fill at market + slippage
            fill_price = min(market_price * (1 + size_slippage), 0.99)
            return (fill_price, size)
        elif price_diff <= spread_tolerance:
            # Within spread tolerance — probabilistic fill
            #
            # Fill probability:
            #   - 90% when limit == market (right at the top of the book)
            #   - Linearly decays to 20% at the edge of the spread
            #   - Larger orders get a penalty (×0.8 for big orders)
            #
            # This prevents the old behavior where ANY limit within the spread
            # filled 100% of the time, which was unrealistically optimistic.
            proximity = 1.0 - (price_diff / spread_tolerance) if spread_tolerance > 0 else 1.0
            base_probability = 0.20 + 0.70 * proximity  # 20% → 90%
            
            # Size penalty: orders > 50 shares are harder to fill without
            # visible orderbook liquidity
            size_penalty = 1.0 if size <= 50 else max(0.5, 1.0 - (size - 50) * 0.002)
            fill_probability = base_probability * size_penalty
            
            if random.random() < fill_probability:
                return (limit_price, size)
            else:
                logger.debug(
                    f"[{self.slug}] Spread model: {side.upper()} limit={limit_price:.4f} "
                    f"not filled (prob={fill_probability:.0%}, diff={price_diff:.2%})"
                )
                return None
        
        return None

    def _link_order_id(self, runner_order_id: str, side: str, price: float) -> None:
        """
        Link the runner's unique order_id to the strategy's LimitOrder.
        
        The strategy creates LimitOrder entries in on_price_update() with its own
        order_id format (e.g., "up_1"). This method overwrites the strategy's
        order_id with the runner's unique ID for precise matching during
        fill/cancel operations.
        
        Only matches orders that haven't already been linked to a runner ID
        (strategy IDs use format "{side}_{N}", runner IDs use "{slug}_{side}_{N}").
        """
        # Find the most recent UNLINKED strategy pending order matching side and price
        for pending in reversed(self.strategy.pending_orders):
            if pending.side == side and abs(pending.price - price) < 0.001:
                # Skip orders already linked to a runner ID (contain slug prefix)
                if self.slug in pending.order_id:
                    continue
                pending.order_id = runner_order_id
                return

    def _submit_order(self, signal: OrderSignal, price_data: PriceData) -> None:
        """Submit an order based on strategy signal with depth-aware fill simulation."""
        self.result.orders_submitted += 1

        # Extract side - token_type YES=up, NO=down
        token_type = signal.token_type if isinstance(signal.token_type, str) else signal.token_type.value
        side_str = "up" if token_type in ("yes", "YES") else "down"

        # Generate unique order ID for precise matching
        self._next_order_id += 1
        order_id = f"{self.slug}_{side_str}_{self._next_order_id}"

        market_price = price_data.up_price if side_str == "up" else price_data.down_price
        
        # Detect taker orders: any order priced at or above market crosses
        # the spread as a taker and incurs fees on Polymarket.
        # All current strategy orders are maker (limit below market),
        # including rebalancing orders (market × 0.998).
        is_taker = signal.target_price >= market_price

        order = {
            "order_id": order_id,
            "side": side_str,
            "size": signal.size,
            "price": signal.target_price,
            "timestamp": time.time(),
            "is_taker": is_taker,
        }

        # Link runner order_id to the most recently added strategy pending order
        self._link_order_id(order_id, side_str, signal.target_price)

        # Attempt depth-aware fill simulation
        fill_result = self._simulate_fill_with_depth(
            side_str, signal.size, signal.target_price, market_price
        )

        if fill_result:
            fill_price, filled_size = fill_result
            # If partially filled, update the order size
            if filled_size < signal.size - 0.01:
                # Partial fill: execute what we can, leave rest pending
                filled_order = {**order, "size": filled_size}
                self._execute_fill(filled_order, fill_price)
                # Remaining goes to pending
                remaining = signal.size - filled_size
                remaining_order = {
                    **order,
                    "order_id": f"{order_id}_rem",
                    "size": remaining,
                }
                self.pending_orders.append(remaining_order)
                logger.debug(
                    f"[{self.slug}] Partial fill: {filled_size:.1f}/{signal.size:.1f} "
                    f"@ {fill_price:.4f}, {remaining:.1f} pending"
                )
            else:
                # Full fill
                self._execute_fill(order, fill_price)
        else:
            # No immediate fill - add to pending
            self.pending_orders.append(order)

    def _cancel_pending_order(self, order: Dict[str, Any], reason: str) -> None:
        """Cancel a pending order and free up strategy budget."""
        side = order["side"]
        limit_price = order["price"]
        order_id = order.get("order_id", "")
        logger.info(
            f"[{self.slug}] Cancelling {side.upper()} order ({reason}): "
            f"id={order_id}, limit={limit_price:.4f}, size={order['size']:.1f}"
        )
        # Remove from strategy's pending orders to free up budget
        # Use order_id for precise matching, fall back to side+price
        found = False
        for i, pending in enumerate(self.strategy.pending_orders):
            if order_id and pending.order_id == order_id:
                self.strategy.pending_orders.pop(i)
                found = True
                break
            elif not order_id and pending.side == side and abs(pending.price - limit_price) < 0.01:
                self.strategy.pending_orders.pop(i)
                found = True
                break
        if not found:
            logger.warning(
                f"[{self.slug}] Failed to find matching strategy pending order for "
                f"id={order_id} {side.upper()}@{limit_price:.4f} "
                f"(strategy has {len(self.strategy.pending_orders)} pending)"
            )

    def _sync_pending_orders(self) -> None:
        """
        Force-sync strategy's pending orders with runner's actual pending list.
        
        This is a safety net: if _cancel_pending_order or _execute_fill fails to
        match, ghost entries accumulate in the strategy's pending list, permanently
        consuming budget. This method removes any ghost entries.
        """
        runner_up_count = sum(1 for o in self.pending_orders if o["side"] == "up")
        runner_down_count = sum(1 for o in self.pending_orders if o["side"] == "down")

        strategy_up = [o for o in self.strategy.pending_orders if o.side == "up"]
        strategy_down = [o for o in self.strategy.pending_orders if o.side == "down"]

        ghost_up = len(strategy_up) - runner_up_count
        ghost_down = len(strategy_down) - runner_down_count

        if ghost_up > 0 or ghost_down > 0:
            # Trim strategy pending to match runner's count (keep newest entries)
            new_pending = strategy_up[-runner_up_count:] if runner_up_count > 0 else []
            new_pending += strategy_down[-runner_down_count:] if runner_down_count > 0 else []
            ghost_total = ghost_up + ghost_down
            ghost_cost = sum(o.cost for o in self.strategy.pending_orders) - sum(o.cost for o in new_pending)
            logger.info(
                f"[{self.slug}] Sync: removed {ghost_total} ghost pending orders "
                f"(freed ${ghost_cost:.2f} budget). "
                f"Strategy had UP:{len(strategy_up)}/DOWN:{len(strategy_down)}, "
                f"Runner has UP:{runner_up_count}/DOWN:{runner_down_count}"
            )
            self.strategy.pending_orders = new_pending

    def _check_fills(self, price_data: PriceData) -> None:
        """Check pending orders for fills using depth simulation, timeouts, and staleness."""
        remaining = []
        stale_threshold = self.config.stale_order_threshold
        order_timeout = self.config.order_timeout
        now = time.time()

        for order in self.pending_orders:
            side = order["side"]
            limit_price = order["price"]
            size = order["size"]

            market_price = price_data.up_price if side == "up" else price_data.down_price

            # Calculate price difference as percentage
            price_diff = (market_price - limit_price) / market_price if market_price > 0 else 0

            # Attempt depth-aware fill
            fill_result = self._simulate_fill_with_depth(side, size, limit_price, market_price)

            if fill_result:
                fill_price, filled_size = fill_result
                if filled_size < size - 0.01:
                    # Partial fill from depth
                    filled_order = {**order, "size": filled_size}
                    self._execute_fill(filled_order, fill_price)
                    order["size"] = size - filled_size
                    remaining.append(order)
                else:
                    # Full fill
                    self._execute_fill(order, fill_price)
            elif price_diff > stale_threshold:
                # Market moved too far from limit - cancel stale order
                self._cancel_pending_order(
                    order,
                    f"stale: market={market_price:.2f}, diff={price_diff:.1%}"
                )
            elif now - order["timestamp"] > order_timeout:
                # Order has been pending too long - cancel to free budget
                age = now - order["timestamp"]
                self._cancel_pending_order(
                    order,
                    f"timeout: {age:.0f}s > {order_timeout:.0f}s"
                )
            else:
                remaining.append(order)

        self.pending_orders = remaining

    def _execute_fill(self, order: Dict[str, Any], fill_price: float) -> None:
        """
        Execute an order fill, applying taker fee when applicable.
        
        Limit orders (maker) incur no fee on Polymarket.
        Rebalancing / market orders (taker) incur a fee that reduces shares received.
        The ``is_taker`` flag in the order dict controls this.
        """
        side = order["side"]
        size = order["size"]
        
        # Apply taker fee (rebalancing orders only)
        if order.get("is_taker", False):
            fee_rate = calculate_taker_fee_rate(fill_price)
            size_after_fee = apply_taker_fee_to_shares(size, fill_price)
            if fee_rate > 0.0001:
                logger.info(
                    f"[{self.slug}] Taker fee: {side.upper()} {size:.1f} → {size_after_fee:.1f} shares "
                    f"(fee rate={fee_rate:.2%}, cost=${size * fill_price:.4f})"
                )
            size = size_after_fee
        
        cost = size * fill_price

        self.result.orders_filled += 1

        if side == "up":
            self.result.up_shares += size
            self.result.up_cost += cost
            # Sync with strategy's internal position tracking
            self.strategy.up_position.add(size, fill_price)
        else:
            self.result.down_shares += size
            self.result.down_cost += cost
            # Sync with strategy's internal position tracking
            self.strategy.down_position.add(size, fill_price)

        # Remove corresponding order from strategy's pending_orders
        # Use order_id for precise matching, fall back to side+size/price
        order_id = order.get("order_id", "")
        found = False
        for i, pending in enumerate(self.strategy.pending_orders):
            if order_id and pending.order_id == order_id:
                self.strategy.pending_orders.pop(i)
                found = True
                break
            elif not order_id and pending.side == side:
                size_match = abs(pending.shares - size) / max(pending.shares, 0.001) < 0.05
                price_match = abs(pending.price - order["price"]) < 0.01
                if size_match or price_match:
                    self.strategy.pending_orders.pop(i)
                    found = True
                    break
        if not found:
            logger.debug(
                f"[{self.slug}] Fill: no matching strategy pending for "
                f"id={order_id} {side.upper()} size={size:.2f} price={order['price']:.4f} "
                f"(strategy has {len(self.strategy.pending_orders)} pending)"
            )

    def finalize(self, winner: str) -> MarketResult:
        """Finalize the market simulation with settlement."""
        self.result.winner = winner
        self.result.end_time = time.time()
        self.result.calculate_final_metrics()
        return self.result

    @property
    def prices_are_stale(self) -> bool:
        """Check if prices haven't been updated recently."""
        if self.last_price_update_time == 0:
            return True
        return (time.time() - self.last_price_update_time) > self.price_stale_threshold

    def get_current_prices(self) -> Tuple[Optional[float], Optional[float]]:
        """Get current prices, returning None if stale."""
        if self.prices_are_stale:
            return None, None
        return self.last_up_price, self.last_down_price

    def get_status(self) -> Dict[str, Any]:
        """Get current simulation status for this market."""
        up_p, down_p = self.get_current_prices()
        return {
            "coin": self.coin,
            "up_shares": self.result.up_shares,
            "down_shares": self.result.down_shares,
            "up_cost": self.result.up_cost,
            "down_cost": self.result.down_cost,
            "ecr": self.result.ecr if self.result.ecr != float('inf') else 999.99,
            "balance_ratio": self.result.balance_ratio,
            "orders_submitted": self.result.orders_submitted,
            "orders_filled": self.result.orders_filled,
            "current_up_price": up_p,
            "current_down_price": down_p,
            "prices_stale": self.prices_are_stale,
            "started_at": datetime.fromtimestamp(self.result.start_time).isoformat(),
            "pending_orders": len(self.pending_orders),
        }


class LiveRunner:
    """
    Real-time multi-market simulation runner.

    Features:
    - Continuous market discovery for BTC, ETH, SOL
    - Strategy execution with simulated fills
    - Configurable runtime duration
    - Metrics output and state persistence
    """

    def __init__(self, config: SimulationConfig):
        self.config = config
        self.stats = SimulationStats(start_time=time.time())

        # State
        self._running = False
        self._shutdown_requested = False
        self._shutdown_task: Optional[asyncio.Task] = None

        # Active simulations (slug -> MarketSimulation)
        self._active_sims: Dict[str, MarketSimulation] = {}

        # Completed results
        self._results: List[MarketResult] = []

        # Price streaming tasks (legacy, for fallback)
        self._price_tasks: Dict[str, asyncio.Task] = {}

        # Track skipped markets to avoid repeated logging
        self._skipped_markets: Set[str] = set()

        # Settlement confirmation tracking (slug -> consecutive_confirmations)
        self._settlement_confirmations: Dict[str, int] = {}
        self._settlement_confirmation_required: int = 3  # 3 consecutive checks (15s)

        # WebSocket manager for real-time price updates
        self._ws_manager: Optional[WebSocketManager] = None

        # HTTP data fetcher reference (set during run())
        self._fetcher: Optional[RealDataFetcher] = None

        # Mapping: token_id -> (slug, side)
        self._token_to_market: Dict[str, Tuple[str, str]] = {}

        # Output files
        self._ensure_output_dir()

    def _ensure_output_dir(self) -> None:
        """Create output directory if needed."""
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def status_file(self) -> Path:
        return self.config.output_dir / "status.json"

    @property
    def metrics_file(self) -> Path:
        return self.config.output_dir / "metrics.jsonl"

    @property
    def results_file(self) -> Path:
        return self.config.output_dir / "results.jsonl"

    def _write_status(self) -> None:
        """Write current status to status.json."""
        # Collect market details
        market_details = {}
        for slug, sim in self._active_sims.items():
            market_details[slug] = sim.get_status()

        status = {
            "running": self._running,
            "start_time": datetime.fromtimestamp(self.stats.start_time).isoformat(),
            "last_update": datetime.fromtimestamp(time.time()).isoformat(),
            "config": {
                "coins": self.config.coins,
                "duration_seconds": self.config.duration_seconds,
                "target_cost": self.config.target_cost,
            },
            "stats": self.stats.to_dict(),
            "active_markets": list(self._active_sims.keys()),
            "market_details": market_details,
        }

        # Write status — use unique tmp name to avoid race between concurrent callers
        try:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self.status_file.with_suffix(f".tmp.{os.getpid()}")
            with open(tmp_file, "w") as f:
                json.dump(status, f, indent=2)
            os.replace(str(tmp_file), str(self.status_file))  # atomic on POSIX
        except OSError as e:
            logger.warning(f"Failed to write status file: {e}")
            # Clean up orphaned tmp file
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass

    def _append_metrics(self) -> None:
        """Append current metrics to metrics.jsonl."""
        metrics = {
            "timestamp": datetime.now().isoformat(),
            "epoch": time.time(),
            **self.stats.to_dict(),
            "active_markets": len(self._active_sims),
        }

        with open(self.metrics_file, "a") as f:
            f.write(json.dumps(metrics) + "\n")

    def _append_result(self, result: MarketResult) -> None:
        """Append a market result to results.jsonl."""
        result_dict = {
            "timestamp": datetime.now().isoformat(),
            "slug": result.slug,
            "coin": result.coin,
            "winner": result.winner,
            "pnl": result.pnl,
            "roi": result.roi,
            "ecr": result.ecr,
            "balance_ratio": result.balance_ratio,
            "up_shares": result.up_shares,
            "down_shares": result.down_shares,
            "up_cost": result.up_cost,
            "down_cost": result.down_cost,
            "orders_submitted": result.orders_submitted,
            "orders_filled": result.orders_filled,
        }

        with open(self.results_file, "a") as f:
            f.write(json.dumps(result_dict) + "\n")

    def _archive_old_state(self) -> None:
        """Archive existing status file if present."""
        if self.status_file.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_name = f"status_{timestamp}.json"
            archive_path = self.config.output_dir / "archive" / archive_name
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            self.status_file.rename(archive_path)
            logger.info(f"Archived previous status to {archive_path}")

    def _load_state(self) -> bool:
        """Load state from status.json for resume. Returns True if loaded."""
        if not self.status_file.exists():
            return False

        try:
            with open(self.status_file) as f:
                status = json.load(f)

            # Restore stats
            stats_data = status.get("stats", {})
            self.stats.markets_processed = stats_data.get("markets_processed", 0)
            self.stats.markets_won = stats_data.get("markets_won", 0)
            self.stats.markets_lost = stats_data.get("markets_lost", 0)
            self.stats.total_pnl = stats_data.get("total_pnl", 0.0)
            self.stats.total_cost = stats_data.get("total_cost", 0.0)

            logger.info(f"Resumed from checkpoint: {self.stats.markets_processed} markets processed")
            return True

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load state: {e}")
            return False

    async def _handle_market_event(self, event: MarketEventData) -> None:
        """Handle market lifecycle events."""
        slug = event.market_slug

        if event.event_type == MarketEvent.MARKET_ACTIVE:
            # Start simulation for new market
            if slug not in self._active_sims:
                await self._start_market_simulation(event)

        elif event.event_type == MarketEvent.MARKET_SETTLING:
            # Market is about to settle
            logger.info(f"Market {slug} is settling soon")

        elif event.event_type == MarketEvent.MARKET_SETTLED:
            # Market has settled
            await self._finalize_market(slug, event.winner)

    def _is_market_valid(
        self,
        settlement_time: Optional[float],
        up_price: Optional[float],
        down_price: Optional[float],
        slug: str,
    ) -> Tuple[bool, str]:
        """
        Check if a market is valid for simulation.

        Returns:
            Tuple of (is_valid, reason_if_invalid)
        """
        now = time.time()

        # Check time until settlement
        if settlement_time:
            time_remaining = settlement_time - now
            if time_remaining <= 0:
                return False, "Already settled or settling"
            if time_remaining < self.config.min_trading_time:
                return False, f"Only {time_remaining:.0f}s remaining (need {self.config.min_trading_time:.0f}s)"

        # Check if outcome is already decided
        if up_price is not None and down_price is not None:
            price_sum = up_price + down_price
            min_price = min(up_price, down_price)

            if price_sum >= 0.98 and min_price < self.config.min_price_threshold:
                winner_side = "DOWN" if up_price < down_price else "UP"
                return False, f"Outcome decided ({winner_side} winning: UP={up_price:.3f}, DOWN={down_price:.3f})"

            # Entry skew filter: skip markets already too skewed at entry.
            # Arbitrage strategy needs roughly balanced markets; entering a skewed
            # market means limit orders on the expensive side rarely fill, creating
            # one-sided positions from the start.
            max_price = max(up_price, down_price)
            if max_price > self.config.max_entry_skew:
                dominant = "UP" if up_price > down_price else "DOWN"
                return False, (
                    f"Too skewed at entry ({dominant}={max_price:.1%}, "
                    f"threshold={self.config.max_entry_skew:.0%})"
                )

        return True, ""

    def _get_total_exposure(self) -> float:
        """Get total cost exposure across all active markets."""
        return sum(
            sim.result.up_cost + sim.result.down_cost
            for sim in self._active_sims.values()
        )

    def _can_start_new_market(self) -> bool:
        """Check if a new market can be started within global risk limits."""
        if len(self._active_sims) >= self.config.max_concurrent_markets:
            return False
        if self._get_total_exposure() >= self.config.max_total_exposure:
            return False
        return True

    async def _start_market_simulation(self, event: MarketEventData) -> None:
        """Start simulation for a new market."""
        slug = event.market_slug

        # Global fund management: check limits before starting
        if not self._can_start_new_market():
            exposure = self._get_total_exposure()
            logger.info(
                f"Skipping market {slug}: global limits reached "
                f"(active={len(self._active_sims)}/{self.config.max_concurrent_markets}, "
                f"exposure=${exposure:.2f}/${self.config.max_total_exposure:.2f})"
            )
            return

        market = {
            "slug": slug,
            "coin": event.coin,
            "condition_id": event.condition_id,
            "up_token_id": event.up_token_id,
            "down_token_id": event.down_token_id,
            "settlement_time": event.settlement_time,
        }

        sim = MarketSimulation(market, self.config)

        # Initialize strategy lifecycle (pass settlement_time for urgency calculation)
        sim.strategy.on_market_start(
            market_id=slug,
            market_info={
                "slug": slug,
                "coin": event.coin,
                "condition_id": event.condition_id,
                "settlement_time": event.settlement_time,
            }
        )

        self._active_sims[slug] = sim

        # Subscribe to WebSocket for real-time price updates
        await self._subscribe_market_ws(slug, sim)

        # Fetch initial orderbook depth via HTTP
        if self._fetcher:
            for side, token_id in [("up", event.up_token_id), ("down", event.down_token_id)]:
                if token_id:
                    try:
                        raw = await self._fetcher.get_orderbook(token_id)
                        if raw:
                            snapshot = OrderbookSnapshot.from_raw(raw)
                            sim.update_orderbook(side, snapshot)
                            logger.info(
                                f"[{slug}] Initial {side.upper()} orderbook: "
                                f"bids={len(snapshot.bids)} asks={len(snapshot.asks)} "
                                f"spread={snapshot.spread:.4f}"
                            )
                    except Exception as e:
                        logger.debug(f"Failed to fetch initial {side} orderbook for {slug}: {e}")

        logger.info(f"Started simulation for {slug} ({event.coin})")

    async def _finalize_market(self, slug: str, winner: Optional[str]) -> None:
        """Finalize a market simulation."""
        if slug not in self._active_sims:
            return

        sim = self._active_sims.pop(slug)

        # Unsubscribe from WebSocket
        await self._unsubscribe_market_ws(slug, sim)

        if winner:
            result = sim.finalize(winner)
            self._results.append(result)
            self.stats.add_result(result)
            self._append_result(result)

            logger.info(
                f"Market {slug} settled ({winner}): "
                f"PnL=${result.pnl:.2f}, ROI={result.roi*100:.1f}%"
            )
        else:
            logger.warning(f"Market {slug} ended without winner")

        # Stop price streaming for this market
        for token_key in [f"{slug}_up", f"{slug}_down"]:
            if token_key in self._price_tasks:
                self._price_tasks[token_key].cancel()
                del self._price_tasks[token_key]

    def _handle_price_update(self, slug: str, price: PriceUpdate) -> None:
        """Handle a price update for a market."""
        if slug in self._active_sims:
            self._active_sims[slug].process_price_update(price)

    def _on_ws_event(self, event_type: str, identifier: str, data: Any) -> None:
        """
        Handle WebSocket events (called in real-time).

        Args:
            event_type: "token_price", "token_trade", or legacy "price_update"
            identifier: token_id for token events, market_id for legacy events
            data: Event data dict
        """
        if event_type == "token_price":
            # Per-token price update (new format)
            token_id = identifier
            if token_id not in self._token_to_market:
                return

            slug, side = self._token_to_market[token_id]
            if slug not in self._active_sims:
                return

            # Only process if we have valid price data (not None)
            mid_price = data.get("mid_price")
            best_bid = data.get("best_bid")
            best_ask = data.get("best_ask")

            if mid_price is None:
                logger.debug(f"[WS] Ignoring token_price for {token_id[:8]}... - no mid_price")
                return

            spread = (best_ask - best_bid) if (best_ask is not None and best_bid is not None) else 0.0

            # Defense-in-depth: filter garbage that somehow bypassed WS client filter.
            # Primary filter is in websocket_client.py (_handle_orderbook_snapshot,
            # _process_single_orderbook). This is a safety net.
            if spread > 0.50:
                logger.warning(
                    f"[WS] Defense filter: garbage token_price for {token_id[:12]}... "
                    f"leaked through (spread={spread:.2f}). Dropped."
                )
                return

            price_update = PriceUpdate(
                token_id=token_id,
                mid_price=mid_price,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                timestamp=time.time(),
            )
            self._handle_price_update(slug, price_update)
            logger.debug(f"[WS] {slug} {side.upper()}: {mid_price:.4f}")

        elif event_type == "token_orderbook":
            # Per-token orderbook depth update
            token_id = identifier
            if token_id not in self._token_to_market:
                return

            slug, side = self._token_to_market[token_id]
            if slug not in self._active_sims:
                return

            # Parse and cache orderbook snapshot
            snapshot = OrderbookSnapshot.from_raw(data)
            self._active_sims[slug].update_orderbook(side, snapshot)
            logger.debug(
                f"[WS Book] {slug} {side.upper()}: "
                f"bids={len(snapshot.bids)} asks={len(snapshot.asks)} "
                f"spread={snapshot.spread:.4f}"
            )

        elif event_type == "token_trade":
            # Per-token trade (can use for price update)
            token_id = identifier
            if token_id not in self._token_to_market:
                return

            slug, side = self._token_to_market[token_id]
            if slug not in self._active_sims:
                return

            price = data.get("price")
            if price is not None:
                price_update = PriceUpdate(
                    token_id=token_id,
                    mid_price=float(price),
                    best_bid=float(price),
                    best_ask=float(price),
                    spread=0.0,
                    timestamp=time.time(),
                )
                self._handle_price_update(slug, price_update)
                logger.debug(f"[WS Trade] {slug} {side.upper()}: {price}")

    async def _subscribe_market_ws(self, slug: str, sim: MarketSimulation) -> None:
        """Subscribe to WebSocket for a market."""
        if not self._ws_manager:
            return

        condition_id = sim.market.get("condition_id")
        up_token = sim.market.get("up_token_id")
        down_token = sim.market.get("down_token_id")

        if not condition_id:
            logger.warning(f"No condition_id for {slug}, skipping WebSocket subscription")
            return

        token_ids = []
        if up_token:
            token_ids.append(up_token)
            self._token_to_market[up_token] = (slug, "up")
        if down_token:
            token_ids.append(down_token)
            self._token_to_market[down_token] = (slug, "down")

        if token_ids:
            await self._ws_manager.subscribe(condition_id, token_ids)
            logger.info(f"Subscribed to WebSocket for {slug} (condition_id={condition_id[:8]}...)")

    async def _unsubscribe_market_ws(self, slug: str, sim: MarketSimulation) -> None:
        """Unsubscribe from WebSocket for a market."""
        if not self._ws_manager:
            return

        condition_id = sim.market.get("condition_id")
        up_token = sim.market.get("up_token_id")
        down_token = sim.market.get("down_token_id")

        if condition_id:
            await self._ws_manager.unsubscribe(condition_id)

        # Clean up token mappings
        if up_token and up_token in self._token_to_market:
            del self._token_to_market[up_token]
        if down_token and down_token in self._token_to_market:
            del self._token_to_market[down_token]

    async def _orderbook_refresh_loop(self, fetcher: RealDataFetcher) -> None:
        """
        Periodically fetch orderbook depth via HTTP API for active markets.
        
        Acts as a fallback when WebSocket doesn't provide depth data,
        and ensures orderbook caches stay reasonably fresh.
        Refreshes every 15 seconds.
        """
        while self._running:
            try:
                for slug, sim in list(self._active_sims.items()):
                    up_token = sim.market.get("up_token_id")
                    down_token = sim.market.get("down_token_id")

                    # Only refresh if orderbook is stale or missing
                    if up_token and (sim.up_orderbook is None or not sim.up_orderbook.is_valid):
                        try:
                            raw = await fetcher.get_orderbook(up_token)
                            if raw:
                                snapshot = OrderbookSnapshot.from_raw(raw)
                                sim.update_orderbook("up", snapshot)
                                logger.debug(
                                    f"[HTTP Book] {slug} UP: "
                                    f"bids={len(snapshot.bids)} asks={len(snapshot.asks)}"
                                )
                        except Exception as e:
                            logger.debug(f"Failed to fetch UP orderbook for {slug}: {e}")

                    if down_token and (sim.down_orderbook is None or not sim.down_orderbook.is_valid):
                        try:
                            raw = await fetcher.get_orderbook(down_token)
                            if raw:
                                snapshot = OrderbookSnapshot.from_raw(raw)
                                sim.update_orderbook("down", snapshot)
                                logger.debug(
                                    f"[HTTP Book] {slug} DOWN: "
                                    f"bids={len(snapshot.bids)} asks={len(snapshot.asks)}"
                                )
                        except Exception as e:
                            logger.debug(f"Failed to fetch DOWN orderbook for {slug}: {e}")

                await asyncio.sleep(15.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in orderbook refresh: {e}")
                await asyncio.sleep(15)

    async def _market_scan_loop(self, fetcher: RealDataFetcher) -> None:
        """Periodically scan for new markets."""
        while self._running:
            try:
                # Find active markets
                markets_by_coin = await fetcher.find_multi_coin_markets(
                    coins=self.config.coins,
                    count_per_coin=5,
                    include_active=True,
                    include_closed=True,
                )

                for coin, markets in markets_by_coin.items():
                    for market in markets:
                        slug = market.get("slug")
                        if not slug:
                            continue

                        is_closed = market.get("closed", False)

                        if is_closed:
                            # Check if we have an active sim that needs finalizing
                            if slug in self._active_sims:
                                winner = market.get("winner")
                                await self._finalize_market(slug, winner)
                        else:
                            # Start simulation if not already running
                            if slug not in self._active_sims:
                                # Get settlement time from end_date
                                settlement_time = None
                                end_date_str = market.get("end_date")
                                if end_date_str:
                                    try:
                                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                                        settlement_time = end_dt.timestamp()
                                    except (ValueError, TypeError):
                                        pass

                                # Fetch current prices to validate market
                                up_price = None
                                down_price = None
                                up_token_id = market.get("up_token_id")
                                down_token_id = market.get("down_token_id")

                                if up_token_id:
                                    price_update = await fetcher.get_mid_price(up_token_id)
                                    if price_update:
                                        up_price = price_update.mid_price

                                if down_token_id:
                                    price_update = await fetcher.get_mid_price(down_token_id)
                                    if price_update:
                                        down_price = price_update.mid_price

                                # Validate market
                                is_valid, reason = self._is_market_valid(
                                    settlement_time, up_price, down_price, slug
                                )

                                if not is_valid:
                                    # Only log once per skipped market
                                    if slug not in self._skipped_markets:
                                        logger.info(f"Skipping market {slug}: {reason}")
                                        self._skipped_markets.add(slug)
                                    continue

                                event = MarketEventData(
                                    event_type=MarketEvent.MARKET_ACTIVE,
                                    market_slug=slug,
                                    coin=coin,
                                    condition_id=market.get("condition_id", ""),
                                    up_token_id=up_token_id or "",
                                    down_token_id=down_token_id or "",
                                    timestamp=time.time(),
                                    settlement_time=settlement_time,
                                )
                                await self._start_market_simulation(event)

                await asyncio.sleep(self.config.market_scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market scan: {e}")
                await asyncio.sleep(30)

    async def _settlement_check_loop(self) -> None:
        """
        Check for market settlements based on WebSocket price data.
        
        Uses multi-confirmation to avoid false settlements from price spikes.
        Requires N consecutive checks (default 3 = 15 seconds) meeting
        settlement criteria before finalizing.
        
        No HTTP calls - uses prices already updated by WebSocket.
        """
        while self._running:
            try:
                # Check for settlements using WebSocket prices
                markets_to_finalize: List[Tuple[str, str]] = []
                checked_slugs: Set[str] = set()

                for slug, sim in list(self._active_sims.items()):
                    checked_slugs.add(slug)

                    # Skip settlement check if prices are stale (network down)
                    if sim.prices_are_stale:
                        self._settlement_confirmations.pop(slug, None)
                        continue

                    up_p = sim.last_up_price or 0
                    down_p = sim.last_down_price or 0

                    # Safety checks to avoid false settlements
                    price_sum = up_p + down_p
                    if price_sum < 0.95 or price_sum > 1.05:
                        self._settlement_confirmations.pop(slug, None)
                        continue

                    settlement_threshold = 0.99
                    losing_threshold = 0.02

                    winner = None
                    if up_p >= settlement_threshold and down_p <= losing_threshold:
                        winner = "up"
                    elif down_p >= settlement_threshold and up_p <= losing_threshold:
                        winner = "down"

                    if winner:
                        # Increment confirmation counter
                        count = self._settlement_confirmations.get(slug, 0) + 1
                        self._settlement_confirmations[slug] = count
                        logger.debug(
                            f"Settlement check: {slug} {winner.upper()} wins "
                            f"(up={up_p:.3f}, down={down_p:.3f}) "
                            f"confirmation {count}/{self._settlement_confirmation_required}"
                        )
                        if count >= self._settlement_confirmation_required:
                            markets_to_finalize.append((slug, winner))
                    else:
                        # Reset confirmation if criteria no longer met
                        if slug in self._settlement_confirmations:
                            logger.debug(f"Settlement check: {slug} criteria no longer met, resetting")
                            self._settlement_confirmations.pop(slug, None)

                # Clean up confirmations for markets no longer active
                stale_keys = set(self._settlement_confirmations.keys()) - checked_slugs
                for key in stale_keys:
                    del self._settlement_confirmations[key]

                # Finalize settled markets (after multi-confirmation)
                for slug, winner in markets_to_finalize:
                    logger.info(
                        f"Detected settlement by price for {slug}: {winner} wins "
                        f"(confirmed {self._settlement_confirmation_required} times)"
                    )
                    self._settlement_confirmations.pop(slug, None)
                    await self._finalize_market(slug, winner)

                # Check every 5 seconds
                await asyncio.sleep(5.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in settlement check: {e}")
                await asyncio.sleep(5)

    async def _metrics_output_loop(self) -> None:
        """Periodically output metrics."""
        while self._running:
            try:
                self._write_status()
                self._append_metrics()

                # Calculate expected PnL from active markets
                expected_pnl = 0.0
                price_info = ""
                for slug, sim in self._active_sims.items():
                    coin = sim.coin.upper()
                    up_p, down_p = sim.get_current_prices()
                    filled = sim.result.orders_filled
                    submitted = sim.result.orders_submitted

                    # Format prices - show "N/A" if stale or no data
                    if sim.prices_are_stale:
                        stale_secs = int(time.time() - sim.last_price_update_time) if sim.last_price_update_time > 0 else 0
                        price_str = f"STALE({stale_secs}s)"
                    elif up_p is not None and down_p is not None:
                        price_str = f"{up_p:.2f}/{down_p:.2f}"
                    elif up_p is not None:
                        price_str = f"{up_p:.2f}/N/A"
                    elif down_p is not None:
                        price_str = f"N/A/{down_p:.2f}"
                    else:
                        price_str = "N/A"

                    # Calculate unrealized PnL for this market
                    # Hedged position = min shares, worst case = hedged - cost
                    up_shares = sim.result.up_shares
                    down_shares = sim.result.down_shares
                    total_cost = sim.result.up_cost + sim.result.down_cost
                    hedged = min(up_shares, down_shares)
                    market_expected_pnl = hedged - total_cost if hedged > 0 else 0
                    expected_pnl += market_expected_pnl

                    # Show ECR for this market
                    ecr = total_cost / hedged if hedged > 0 else 0
                    ecr_str = f"{ecr:.2f}" if hedged > 0 else "-"

                    price_info += f" | {coin}: {price_str} ECR={ecr_str} ({filled}/{submitted})"

                # Total expected = realized + unrealized
                total_expected = self.stats.total_pnl + expected_pnl

                logger.info(
                    f"Realized=${self.stats.total_pnl:.2f}, Expected=${total_expected:.2f}, "
                    f"Markets={self.stats.markets_processed}, "
                    f"Active={len(self._active_sims)}"
                    f"{price_info}"
                )

                await asyncio.sleep(self.config.metrics_output_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics output: {e}")
                await asyncio.sleep(60)

    async def _duration_watchdog(self) -> None:
        """Stop simulation after configured duration."""
        if self.config.duration_seconds <= 0:
            return  # Infinite run

        await asyncio.sleep(self.config.duration_seconds)
        logger.info(f"Duration ({self.config.duration_seconds}s) reached, shutting down")
        await self.stop()

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_signal(s))
            )

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal."""
        logger.info(f"Received {sig.name}, initiating graceful shutdown...")
        await self.stop()

    async def run(self, resume: bool = False) -> SimulationStats:
        """
        Run the live simulation.

        Args:
            resume: If True, resume from previous state

        Returns:
            Final simulation statistics
        """
        if self._running:
            raise RuntimeError("Simulation already running")

        # Handle state
        if resume:
            self._load_state()
        else:
            self._archive_old_state()

        self._running = True
        self._shutdown_requested = False

        # Setup signal handlers
        try:
            self._setup_signal_handlers()
        except NotImplementedError:
            # Signal handlers not supported (e.g., Windows)
            pass

        logger.info(
            f"Starting live simulation: coins={self.config.coins}, "
            f"duration={self.config.duration_seconds}s (WebSocket for prices)"
        )

        # Initialize WebSocket manager for real-time price updates
        self._ws_manager = WebSocketManager()
        self._ws_manager.add_callback(self._on_ws_event)
        await self._ws_manager.start()
        logger.info("WebSocket manager started for real-time price updates")

        async with RealDataFetcher() as fetcher:
            # Store fetcher reference for orderbook fetching in _start_market_simulation
            self._fetcher = fetcher

            # Start background tasks
            tasks = [
                asyncio.create_task(self._market_scan_loop(fetcher)),
                asyncio.create_task(self._settlement_check_loop()),
                asyncio.create_task(self._metrics_output_loop()),
                asyncio.create_task(self._orderbook_refresh_loop(fetcher)),
            ]

            if self.config.duration_seconds > 0:
                tasks.append(asyncio.create_task(self._duration_watchdog()))

            # Wait for shutdown
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                pass
            finally:
                # Cancel all tasks
                for task in tasks:
                    if not task.done():
                        task.cancel()

                # Wait for cancellation
                await asyncio.gather(*tasks, return_exceptions=True)

                # Stop WebSocket manager
                if self._ws_manager:
                    await self._ws_manager.stop()
                    logger.info("WebSocket manager stopped")
                
                # Clear fetcher reference
                self._fetcher = None

        # Final status write
        self._running = False
        self._write_status()
        self._append_metrics()

        logger.info(
            f"Simulation complete: {self.stats.markets_processed} markets, "
            f"PnL=${self.stats.total_pnl:.2f}"
        )

        return self.stats

    async def stop(self, timeout: float = 30.0) -> None:
        """
        Stop the simulation gracefully.

        Args:
            timeout: Maximum time to wait for graceful shutdown
        """
        if not self._running:
            return

        self._shutdown_requested = True
        logger.info("Stopping simulation...")

        # Write final state
        self._write_status()

        # Set running to false to stop loops
        self._running = False

        # Force stop after timeout
        async def _force_stop():
            await asyncio.sleep(timeout)
            if self._shutdown_requested:
                logger.warning(f"Shutdown timeout ({timeout}s), forcing exit")
                os._exit(1)

        self._shutdown_task = asyncio.create_task(_force_stop())
