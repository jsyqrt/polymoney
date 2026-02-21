"""
Shared types and helpers for the trading system.

Provides:
- Polymarket taker fee model (calculate_taker_fee_rate, apply_taker_fee_to_shares)
- FillCalibrationTracker for spread-model calibration
- OrderbookSnapshot for depth-based fill simulation
- SimulationConfig — unified configuration for paper and live trading
- MarketResult, SimulationStats — result and aggregate statistics types
- LiquidityCompetitionTracker — multi-market fill competition model
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from polymoney.core.logging import get_logger

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


class FillCalibrationTracker:
    """
    Tracks fill attempt outcomes for probability model calibration.
    
    Records each fill evaluation event — the predicted fill probability and
    whether the order actually filled — enabling post-hoc analysis to tune
    the spread-based probability model parameters (base_probability range,
    size_penalty curve, proximity mapping).
    
    Usage:
        tracker = FillCalibrationTracker()
        tracker.record(probability=0.65, filled=True, ...)
        
        # After simulation, export for analysis:
        stats = tracker.get_calibration_stats()
        tracker.export_to_jsonl("fill_calibration.jsonl")
    """
    
    def __init__(self):
        self._events: List[Dict[str, Any]] = []
    
    def record(
        self,
        side: str,
        limit_price: float,
        market_price: float,
        size: float,
        spread_tolerance: float,
        fill_probability: float,
        filled: bool,
        fill_source: str = "spread",  # "depth", "spread", or "taker"
        slug: str = "",
    ) -> None:
        """
        Record a fill evaluation event.
        
        Args:
            side: "up" or "down"
            limit_price: Order's limit price
            market_price: Market price at evaluation time
            size: Order size in shares
            spread_tolerance: Effective spread used for probability calculation
            fill_probability: Computed fill probability (0-1)
            filled: Whether the order actually filled
            fill_source: Which fill model was used
            slug: Market slug for reference
        """
        price_diff = (market_price - limit_price) / market_price if market_price > 0 else 0
        self._events.append({
            "timestamp": time.time(),
            "slug": slug,
            "side": side,
            "limit_price": limit_price,
            "market_price": market_price,
            "price_diff": price_diff,
            "size": size,
            "spread_tolerance": spread_tolerance,
            "fill_probability": fill_probability,
            "filled": filled,
            "fill_source": fill_source,
        })
    
    def get_calibration_stats(self) -> Dict[str, Any]:
        """
        Compute calibration statistics for model tuning.
        
        Groups fill events by probability bucket (0-10%, 10-20%, ..., 90-100%)
        and computes actual fill rate per bucket. A well-calibrated model has
        actual_rate ≈ bucket_midpoint for each bucket.
        
        Returns:
            Dict with bucket stats, total events, overall fill rate, and
            Brier score (lower is better).
        """
        if not self._events:
            return {"total_events": 0}
        
        # Only look at spread-model events (depth fills are always fill/no-fill)
        spread_events = [e for e in self._events if e["fill_source"] == "spread"]
        
        if not spread_events:
            return {"total_events": len(self._events), "spread_events": 0}
        
        # Group by probability bucket
        buckets: Dict[str, List[bool]] = {}
        for i in range(10):
            bucket_key = f"{i*10}-{(i+1)*10}%"
            buckets[bucket_key] = []
        
        brier_sum = 0.0
        for event in spread_events:
            prob = event["fill_probability"]
            filled = event["filled"]
            bucket_idx = min(9, int(prob * 10))
            bucket_key = f"{bucket_idx*10}-{(bucket_idx+1)*10}%"
            buckets[bucket_key].append(filled)
            brier_sum += (prob - (1.0 if filled else 0.0)) ** 2
        
        brier_score = brier_sum / len(spread_events) if spread_events else 0
        
        bucket_stats = {}
        for key, fills in buckets.items():
            if fills:
                bucket_stats[key] = {
                    "count": len(fills),
                    "actual_fill_rate": sum(fills) / len(fills),
                }
        
        total_fills = sum(1 for e in spread_events if e["filled"])
        return {
            "total_events": len(self._events),
            "spread_events": len(spread_events),
            "spread_fill_rate": total_fills / len(spread_events),
            "brier_score": brier_score,
            "buckets": bucket_stats,
        }
    
    def export_to_jsonl(self, filepath: str) -> int:
        """
        Export all events to a JSONL file for offline analysis.
        
        Returns:
            Number of events exported.
        """
        import json as _json
        with open(filepath, "a") as f:
            for event in self._events:
                f.write(_json.dumps(event) + "\n")
        count = len(self._events)
        self._events.clear()
        return count


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
    
    def apply_incremental_update(self, changes: Dict[str, Any]) -> None:
        """
        Apply incremental orderbook changes (adds, removes, updates).
        
        Supports delta messages from WS that modify individual levels instead
        of sending the full orderbook each time. This keeps the local orderbook
        more up-to-date between full REST snapshots.
        
        Expected format:
            {
                "bids": [{"price": "0.50", "size": "100"}, ...],
                "asks": [{"price": "0.55", "size": "50"}, ...],
            }
        
        A level with size=0 removes that price level.
        A level with size>0 adds or replaces that price level.
        """
        self.timestamp = time.time()
        
        for raw_level in changes.get("bids", []):
            price, size = self._parse_level(raw_level)
            if price is None:
                continue
            # Remove existing level at this price
            self.bids = [(p, s) for p, s in self.bids if abs(p - price) > 0.0001]
            # Add back if size > 0
            if size is not None and size > 0:
                self.bids.append((price, size))
        # Re-sort bids descending
        if changes.get("bids"):
            self.bids.sort(key=lambda x: x[0], reverse=True)
        
        for raw_level in changes.get("asks", []):
            price, size = self._parse_level(raw_level)
            if price is None:
                continue
            # Remove existing level at this price
            self.asks = [(p, s) for p, s in self.asks if abs(p - price) > 0.0001]
            # Add back if size > 0
            if size is not None and size > 0:
                self.asks.append((price, size))
        # Re-sort asks ascending
        if changes.get("asks"):
            self.asks.sort(key=lambda x: x[0])

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
    max_entry_skew: float = 0.85     # Skip markets where max(up, down) > 85% at entry

    # Global fund management parameters
    max_concurrent_markets: int = 6       # Max markets trading simultaneously
    max_total_exposure: float = 500.0     # Max total cost across all markets

    # Redemption delay (paper mode only; live mode uses real redemption).
    # Seconds between settlement detection and capital release.
    # Models the real-world gap: settlement → API reflects → redeem tx confirms.
    # 0 = instant (unrealistic), 60 = typical real-world latency.
    redemption_delay: float = 60.0

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

    # Aggregate metrics (per-market sum)
    total_pnl: float = 0.0
    total_cost: float = 0.0       # Sum of all market costs (counts recycled capital multiple times)
    total_roi: float = 0.0        # total_pnl / total_cost (per-dollar efficiency)

    # Capital efficiency metrics
    peak_exposure: float = 0.0    # Highest simultaneous capital at risk
    capital_recovered: float = 0.0  # Total value recovered from winning settlements
    capital_turnover: float = 0.0  # total_cost / peak_exposure (how many times capital was recycled)

    # Running calculations for Sharpe
    returns: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.markets_processed == 0:
            return 0.0
        return self.markets_won / self.markets_processed

    @property
    def capital_roi(self) -> float:
        """ROI relative to actual capital deployed (not recycled total).

        This is the true return on investment: how much profit was generated
        per dollar of actual capital committed at peak.  When funds are
        recycled across markets, this is higher than total_roi because the
        same capital is used multiple times.
        """
        if self.peak_exposure <= 0:
            return 0.0
        return self.total_pnl / self.peak_exposure

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

    def update_exposure(self, current_exposure: float) -> None:
        """Track peak capital exposure for capital efficiency metrics."""
        if current_exposure > self.peak_exposure:
            self.peak_exposure = current_exposure

    def add_result(self, result: MarketResult) -> None:
        """Add a market result to aggregate stats."""
        self.markets_processed += 1

        if result.pnl > 0:
            self.markets_won += 1
        else:
            self.markets_lost += 1

        self.total_pnl += result.pnl
        self.total_cost += result.total_cost

        # Track capital recovered from settlement (winning shares = USDC back)
        if result.winner == "up":
            self.capital_recovered += result.up_shares
        elif result.winner == "down":
            self.capital_recovered += result.down_shares

        if result.total_cost > 0:
            self.returns.append(result.roi)

        self.total_roi = self.total_pnl / self.total_cost if self.total_cost > 0 else 0
        self.capital_turnover = self.total_cost / self.peak_exposure if self.peak_exposure > 0 else 0
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
            "peak_exposure": self.peak_exposure,
            "capital_recovered": self.capital_recovered,
            "capital_roi": self.capital_roi,
            "capital_turnover": self.capital_turnover,
            "sharpe_ratio": self.sharpe_ratio,
        }


class LiquidityCompetitionTracker:
    """
    Tracks fill activity across all active markets to model liquidity competition.
    
    When multiple markets are filling orders simultaneously, real-world liquidity
    is shared among them. This tracker records recent fill events and provides a
    competition factor that can reduce fill probability for the spread-based model.
    
    Competition factor:
      - 1 active market filling: no penalty (factor=1.0)
      - 3+ markets filling concurrently in last 5s: ~20% penalty (factor=0.8)
      - 6+ markets: ~40% penalty (factor=0.6)
    """
    
    def __init__(self, window_seconds: float = 5.0):
        self.window_seconds = window_seconds
        self._fill_events: List[Tuple[float, str]] = []  # (timestamp, slug)
    
    def record_fill(self, slug: str) -> None:
        """Record that a market had a fill."""
        self._fill_events.append((time.time(), slug))
        # Prune old events
        cutoff = time.time() - self.window_seconds * 2
        self._fill_events = [(t, s) for t, s in self._fill_events if t >= cutoff]
    
    def get_competition_factor(self, exclude_slug: str = "") -> float:
        """
        Get fill probability multiplier based on concurrent fill activity.
        
        Args:
            exclude_slug: Exclude this market from the count (self)
            
        Returns:
            Multiplier from 0.6 to 1.0 (lower = more competition).
        """
        now = time.time()
        cutoff = now - self.window_seconds
        
        # Count distinct markets with recent fills
        active_slugs = set()
        for ts, slug in self._fill_events:
            if ts >= cutoff and slug != exclude_slug:
                active_slugs.add(slug)
        
        concurrent = len(active_slugs)
        if concurrent <= 1:
            return 1.0
        
        # Penalty: each additional concurrent market reduces probability by ~10%
        # Cap at 0.6 (40% max penalty with 6+ concurrent markets)
        penalty = min(0.4, (concurrent - 1) * 0.10)
        return 1.0 - penalty
