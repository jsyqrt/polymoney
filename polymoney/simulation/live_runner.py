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
from typing import Any, Callable, Dict, List, Optional, Set

from polymoney.core.logging import get_logger
from polymoney.core.models import OrderSignal, PriceData, TokenType, TradeSide
from polymoney.data.real_data_fetcher import (
    RealDataFetcher,
    MarketEvent,
    MarketEventData,
    PriceUpdate,
)
from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy

logger = get_logger("simulation.live_runner")


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
    target_cost: float = 0.98
    batch_size: float = 100.0
    ecr_threshold: float = 1.05
    enable_ecr_stoploss: bool = True
    enable_rebalancing: bool = True
    enable_trend_detection: bool = True
    enable_urgency_pricing: bool = True
    
    # Simulation parameters
    market_scan_interval: float = 300.0  # 5 minutes
    price_update_interval: float = 5.0   # 5 seconds
    metrics_output_interval: float = 300.0  # 5 minutes
    
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
            "batch_size": config.batch_size,
            "ecr_threshold": config.ecr_threshold,
            "enable_ecr_stoploss": config.enable_ecr_stoploss,
            "enable_rebalancing": config.enable_rebalancing,
            "enable_trend_detection": config.enable_trend_detection,
            "enable_urgency_pricing": config.enable_urgency_pricing,
        }
        self.strategy = PositionArbitrageStrategy(
            strategy_id=f"sim_{self.slug}",
            name=f"Simulation {self.slug}",
            position_size=config.batch_size * 10,  # Allow 10 batches
            params=strategy_params,
        )
        
        # Initialize result
        self.result = MarketResult(
            slug=self.slug,
            coin=self.coin,
            condition_id=market.get("condition_id", ""),
            start_time=time.time(),
        )
        
        # Track pending orders
        self.pending_orders: List[Dict[str, Any]] = []
        
        # Last price
        self.last_up_price: Optional[float] = None
        self.last_down_price: Optional[float] = None
    
    def process_price_update(self, price: PriceUpdate) -> None:
        """Process a price update and generate/fill orders."""
        up_token_id = self.market.get("up_token_id")
        down_token_id = self.market.get("down_token_id")
        
        if price.token_id == up_token_id:
            self.last_up_price = price.mid_price
        elif price.token_id == down_token_id:
            self.last_down_price = price.mid_price
        
        if self.last_up_price is None or self.last_down_price is None:
            return
        
        # Create price data for strategy
        price_data = PriceData(
            market_id=self.slug,
            timestamp=datetime.fromtimestamp(price.timestamp),
            up_price=self.last_up_price,
            down_price=self.last_down_price,
        )
        
        # Get order signals from strategy
        signals = self.strategy.on_price_update(price_data)
        
        # Process each signal
        for signal in signals:
            self._submit_order(signal, price_data)
        
        # Check pending orders for fills
        self._check_fills(price_data)
    
    def _submit_order(self, signal: OrderSignal, price_data: PriceData) -> None:
        """Submit an order based on strategy signal."""
        self.result.orders_submitted += 1
        
        # Extract side - token_type YES=up, NO=down
        token_type = signal.token_type if isinstance(signal.token_type, str) else signal.token_type.value
        side_str = "up" if token_type in ("yes", "YES") else "down"
        
        order = {
            "side": side_str,
            "size": signal.size,
            "price": signal.target_price,
            "timestamp": time.time(),
        }
        
        # For market orders or aggressive limits, fill immediately
        if side_str == "up":
            market_price = price_data.up_price
        else:
            market_price = price_data.down_price
        
        if signal.target_price >= market_price:
            # Fill immediately
            self._execute_fill(order, market_price)
        else:
            # Add to pending
            self.pending_orders.append(order)
    
    def _check_fills(self, price_data: PriceData) -> None:
        """Check pending orders for fills."""
        remaining = []
        
        for order in self.pending_orders:
            side = order["side"]
            limit_price = order["price"]
            
            if side == "up":
                market_price = price_data.up_price
            else:
                market_price = price_data.down_price
            
            if market_price <= limit_price:
                self._execute_fill(order, market_price)
            else:
                remaining.append(order)
        
        self.pending_orders = remaining
    
    def _execute_fill(self, order: Dict[str, Any], fill_price: float) -> None:
        """Execute an order fill."""
        side = order["side"]
        size = order["size"]
        cost = size * fill_price
        
        self.result.orders_filled += 1
        
        if side == "up":
            self.result.up_shares += size
            self.result.up_cost += cost
        else:
            self.result.down_shares += size
            self.result.down_cost += cost
    
    def finalize(self, winner: str) -> MarketResult:
        """Finalize the market simulation with settlement."""
        self.result.winner = winner
        self.result.end_time = time.time()
        self.result.calculate_final_metrics()
        return self.result


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
        
        # Price streaming tasks
        self._price_tasks: Dict[str, asyncio.Task] = {}
        
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
        }
        
        # Atomic write
        tmp_file = self.status_file.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(status, f, indent=2)
        tmp_file.rename(self.status_file)
    
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
    
    async def _start_market_simulation(self, event: MarketEventData) -> None:
        """Start simulation for a new market."""
        slug = event.market_slug
        
        market = {
            "slug": slug,
            "coin": event.coin,
            "condition_id": event.condition_id,
            "up_token_id": event.up_token_id,
            "down_token_id": event.down_token_id,
        }
        
        sim = MarketSimulation(market, self.config)
        self._active_sims[slug] = sim
        
        logger.info(f"Started simulation for {slug} ({event.coin})")
        
        # Note: Price streaming is handled by the main market monitor
    
    async def _finalize_market(self, slug: str, winner: Optional[str]) -> None:
        """Finalize a market simulation."""
        if slug not in self._active_sims:
            return
        
        sim = self._active_sims.pop(slug)
        
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
                                event = MarketEventData(
                                    event_type=MarketEvent.MARKET_ACTIVE,
                                    market_slug=slug,
                                    coin=coin,
                                    condition_id=market.get("condition_id", ""),
                                    up_token_id=market.get("up_token_id", ""),
                                    down_token_id=market.get("down_token_id", ""),
                                    timestamp=time.time(),
                                )
                                await self._start_market_simulation(event)
                
                await asyncio.sleep(self.config.market_scan_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market scan: {e}")
                await asyncio.sleep(30)
    
    async def _price_update_loop(self, fetcher: RealDataFetcher) -> None:
        """Periodically update prices for all active markets."""
        while self._running:
            try:
                for slug, sim in list(self._active_sims.items()):
                    # Get UP price
                    up_token = sim.market.get("up_token_id")
                    if up_token:
                        price = await fetcher.get_mid_price(up_token)
                        if price:
                            self._handle_price_update(slug, price)
                    
                    # Get DOWN price
                    down_token = sim.market.get("down_token_id")
                    if down_token:
                        price = await fetcher.get_mid_price(down_token)
                        if price:
                            self._handle_price_update(slug, price)
                
                await asyncio.sleep(self.config.price_update_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in price update: {e}")
                await asyncio.sleep(5)
    
    async def _metrics_output_loop(self) -> None:
        """Periodically output metrics."""
        while self._running:
            try:
                self._write_status()
                self._append_metrics()
                
                logger.info(
                    f"Metrics: PnL=${self.stats.total_pnl:.2f}, "
                    f"Markets={self.stats.markets_processed}, "
                    f"WinRate={self.stats.win_rate*100:.0f}%, "
                    f"Sharpe={self.stats.sharpe_ratio:.2f}"
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
            f"duration={self.config.duration_seconds}s"
        )
        
        async with RealDataFetcher() as fetcher:
            # Start background tasks
            tasks = [
                asyncio.create_task(self._market_scan_loop(fetcher)),
                asyncio.create_task(self._price_update_loop(fetcher)),
                asyncio.create_task(self._metrics_output_loop()),
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
