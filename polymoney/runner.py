"""
TradingRunner - Top-level orchestrator for paper and live trading.

Wires together:
- MarketDataProvider (data layer)
- MarketContext (per-market strategy state)
- OrderExecutor (simulated or live execution)
- FillManager (fill tracking and position reconciliation)
- Metrics output (status.json, metrics.jsonl, results.jsonl)

Replaces the monolithic LiveRunner with a clean, modular architecture
that supports both paper trading (SimulatedExecutor) and real trading
(LiveExecutor) through the same code path.

Usage:
    config = SimulationConfig(...)
    runner = TradingRunner(config, mode="paper")  # or "live"
    stats = await runner.run()
"""

import asyncio
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from polymoney.core.logging import get_logger
from polymoney.core.models import OrderSignal, PriceData, TokenType
from polymoney.data.market_data_provider import MarketDataProvider
from polymoney.data.real_data_fetcher import (
    MarketEvent,
    MarketEventData,
    PriceUpdate,
)
from polymoney.execution.executor import (
    ExecutionOrder,
    FillEvent,
    MarketExecutionConfig,
    OrderResult,
    OrderResultStatus,
)
from polymoney.execution.fill_manager import FillManager
from polymoney.execution.simulated import SimulatedExecutor
from polymoney.execution.live import LiveExecutor
from polymoney.simulation.live_runner import (
    LiquidityCompetitionTracker,
    MarketResult,
    OrderbookSnapshot,
    SimulationConfig,
    SimulationStats,
)
from polymoney.monitoring.alerts import AlertManager
from polymoney.risk.manager import RiskManager
from polymoney.risk.kill_switch import KillSwitch
from polymoney.strategy.market_context import MarketContext

logger = get_logger("runner")


class TradingRunner:
    """
    Top-level trading orchestrator.
    
    Manages the full lifecycle:
    1. Market discovery (via MarketDataProvider)
    2. Strategy execution (via MarketContext + PositionArbitrageStrategy)
    3. Order execution (via OrderExecutor — simulated or live)
    4. Fill tracking (via FillManager)
    5. Metrics and status reporting
    6. Graceful shutdown with signal handling
    """

    def __init__(self, config: SimulationConfig, mode: str = "paper"):
        """
        Args:
            config: Simulation/trading configuration
            mode: "paper" for simulated execution, "live" for real CLOB
        """
        self.config = config
        self.mode = mode
        self.stats = SimulationStats(start_time=time.time())

        # State
        self._running = False
        self._shutdown_requested = False
        self._shutdown_task: Optional[asyncio.Task] = None

        # Data layer
        self.data_provider = MarketDataProvider(config)

        # Execution layer
        if mode == "live":
            self.executor = LiveExecutor()
            # ClobClient will be initialized in run() after validation
        else:
            liquidity_tracker = LiquidityCompetitionTracker()
            self.executor = SimulatedExecutor(
                liquidity_tracker=liquidity_tracker
            )

        # Fill manager
        trade_log = config.output_dir / "trades.jsonl"
        self.fill_manager = FillManager(trade_log_path=trade_log)

        # Risk management
        self.risk_manager = RiskManager(
            daily_loss_limit=getattr(config, "daily_loss_limit", 50.0),
            per_market_loss_limit=getattr(config, "per_market_loss_limit", 10.0),
            max_total_exposure=config.max_total_exposure,
        )
        self.kill_switch = KillSwitch(
            kill_file=config.output_dir / "KILL_SWITCH"
        )

        # Alert manager (configured from env)
        from polymoney.core.config import AlertConfig
        alert_config = AlertConfig()
        self.alert_manager = AlertManager(
            webhook_url=alert_config.webhook_url,
            enabled=alert_config.enable_alerts,
        )

        # Active market contexts: slug -> MarketContext
        self._contexts: Dict[str, MarketContext] = {}

        # Completed results
        self._results: List[MarketResult] = []

        # Output directory
        config.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, resume: bool = False) -> SimulationStats:
        """
        Run the trading loop.
        
        Args:
            resume: If True, resume from previous state
            
        Returns:
            Final simulation statistics
        """
        if self._running:
            raise RuntimeError("Runner already active")

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
            pass

        # Initialize ClobClient for live mode
        if self.mode == "live":
            await self._init_clob_client()

        logger.info(
            f"Starting TradingRunner: mode={self.mode}, coins={self.config.coins}, "
            f"duration={self.config.duration_seconds}s"
        )

        # Wire callbacks
        self.data_provider.on_market_discovered = self._on_market_discovered
        self.data_provider.on_price_update = self._on_price_update
        self.data_provider.on_orderbook_update = self._on_orderbook_update
        self.data_provider.on_orderbook_incremental = self._on_orderbook_incremental
        self.data_provider.on_settlement = self._on_settlement
        self.data_provider.on_market_closed = self._on_market_closed

        # Start kill switch monitoring
        self.kill_switch.on_kill = self._on_kill_switch
        await self.kill_switch.start_monitoring()

        # Start data provider
        await self.data_provider.start()

        # Start background tasks
        tasks = [
            asyncio.create_task(self._metrics_output_loop()),
        ]
        if self.config.duration_seconds > 0:
            tasks.append(asyncio.create_task(self._duration_watchdog()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            # Stop kill switch
            await self.kill_switch.stop_monitoring()

            # Stop data provider
            await self.data_provider.stop()

            # Cancel all pending orders
            cancelled = await self.executor.cancel_all()
            if cancelled > 0:
                logger.info(f"Cancelled {cancelled} pending orders on shutdown")

        # Final output
        self._running = False
        self._write_status()
        self._append_metrics()

        logger.info(
            f"TradingRunner complete: {self.stats.markets_processed} markets, "
            f"PnL=${self.stats.total_pnl:.2f}"
        )
        return self.stats

    async def stop(self, timeout: float = 30.0) -> None:
        """Stop gracefully."""
        if not self._running:
            return

        self._shutdown_requested = True
        logger.info("Stopping TradingRunner...")
        self._write_status()
        self._running = False

        async def _force_stop():
            await asyncio.sleep(timeout)
            if self._shutdown_requested:
                logger.warning(f"Shutdown timeout ({timeout}s), forcing exit")
                os._exit(1)

        self._shutdown_task = asyncio.create_task(_force_stop())

    # ------------------------------------------------------------------
    # ClobClient initialization (live mode)
    # ------------------------------------------------------------------

    async def _init_clob_client(self) -> None:
        """
        Initialize ClobClient for live trading.
        
        Reads configuration from environment / .env:
        - POLYMARKET_PRIVATE_KEY (required)
        - POLYMARKET_HOST (default: https://clob.polymarket.com)
        - POLYMARKET_CHAIN_ID (default: 137)
        - POLYMARKET_FUNDER (optional)
        - POLYMARKET_SIGNATURE_TYPE (default: 0 = EOA)
        """
        from polymoney.core.config import PolymarketConfig

        pm_config = PolymarketConfig()

        if not pm_config.private_key:
            raise RuntimeError(
                "Live trading requires POLYMARKET_PRIVATE_KEY. "
                "Set it in .env or as an environment variable."
            )

        try:
            from py_clob_client.client import ClobClient

            client = ClobClient(
                host=pm_config.host,
                key=pm_config.private_key,
                chain_id=pm_config.chain_id,
                funder=pm_config.funder,
                signature_type=pm_config.signature_type,
            )

            # Validate wallet — derive address to confirm key is valid
            try:
                # py-clob-client derives the address from the private key
                # on construction. Attempt a read-only API call to verify connectivity.
                api_keys = client.derive_api_key()
                logger.info(f"CLOB client initialized (API key derived)")
            except Exception as e:
                logger.warning(
                    f"CLOB client initialized but API key derivation failed: {e}. "
                    f"You may need to create API credentials on Polymarket."
                )

            # Set client on executor
            if isinstance(self.executor, LiveExecutor):
                self.executor.set_client(client)

            logger.info(
                f"Live executor ready: host={pm_config.host}, "
                f"chain_id={pm_config.chain_id}"
            )

        except ImportError:
            raise RuntimeError(
                "py-clob-client is required for live trading. "
                "Install with: pip install py-clob-client"
            )

    # ------------------------------------------------------------------
    # Data provider callbacks
    # ------------------------------------------------------------------

    async def _on_market_discovered(self, event: MarketEventData) -> None:
        """Handle new market discovery."""
        slug = event.market_slug

        if slug in self._contexts:
            return

        # Check exposure limits
        total_exposure = self.fill_manager.get_total_exposure()
        if total_exposure >= self.config.max_total_exposure:
            logger.info(
                f"Skipping {slug}: exposure limit "
                f"(${total_exposure:.2f}/${self.config.max_total_exposure:.2f})"
            )
            return

        if len(self._contexts) >= self.config.max_concurrent_markets:
            logger.info(
                f"Skipping {slug}: max concurrent markets "
                f"({len(self._contexts)}/{self.config.max_concurrent_markets})"
            )
            return

        # Create market context
        market = {
            "slug": slug,
            "coin": event.coin,
            "condition_id": event.condition_id,
            "up_token_id": event.up_token_id,
            "down_token_id": event.down_token_id,
            "settlement_time": event.settlement_time,
        }
        ctx = MarketContext(market, self.config)

        # Initialize strategy lifecycle
        ctx.strategy.on_market_start(
            market_id=slug,
            market_info={
                "slug": slug,
                "coin": event.coin,
                "condition_id": event.condition_id,
                "settlement_time": event.settlement_time,
                "min_order_size": getattr(event, "min_order_size", None),
            },
        )

        self._contexts[slug] = ctx

        # Register with data provider
        self.data_provider.register_market(slug, market)

        # Register with executor
        exec_config = MarketExecutionConfig(
            market_id=slug,
            condition_id=event.condition_id,
            up_token_id=event.up_token_id,
            down_token_id=event.down_token_id,
            order_timeout=self.config.order_timeout,
            stale_order_threshold=self.config.stale_order_threshold,
        )
        self.executor.register_market(exec_config)

        # Register with fill manager
        self.fill_manager.register_market(slug)

        # Subscribe to WS and fetch initial orderbooks
        await self.data_provider.subscribe_market(slug, market)

        logger.info(f"Started market context for {slug} ({event.coin})")

    async def _on_price_update(self, slug: str, price: PriceUpdate) -> None:
        """Handle price update — generate signals and submit orders."""
        ctx = self._contexts.get(slug)
        if not ctx:
            return

        # Propagate WS spread to executor for spread-based fill model
        up_token_id = ctx.market.get("up_token_id")
        down_token_id = ctx.market.get("down_token_id")
        if price.token_id == up_token_id:
            self.executor.update_spread(slug, "up", price.spread)
        elif price.token_id == down_token_id:
            self.executor.update_spread(slug, "down", price.spread)

        # Generate order signals from strategy
        signals = ctx.process_price_update(price)

        # Submit each signal to executor
        for signal in signals:
            await self._submit_signal(ctx, signal)

        # Check pending orders for fills
        if ctx.last_up_price is not None and ctx.last_down_price is not None:
            fill_events = await self.executor.check_fills(
                slug, ctx.last_up_price, ctx.last_down_price
            )
            if fill_events:
                self._process_fill_events(ctx, fill_events)

        # Clean up strategy pending orders
        ctx.sync_pending_orders()

    async def _on_orderbook_update(
        self, slug: str, side: str, snapshot: OrderbookSnapshot
    ) -> None:
        """Handle full orderbook snapshot."""
        self.executor.update_orderbook(slug, side, snapshot)

    async def _on_orderbook_incremental(
        self, slug: str, side: str, changes: Dict[str, Any]
    ) -> None:
        """Handle incremental orderbook update."""
        self.executor.update_orderbook_incremental(slug, side, changes)

    async def _on_settlement(self, slug: str, winner: str) -> None:
        """Handle settlement detection."""
        await self._finalize_market(slug, winner)

    async def _on_market_closed(self, slug: str, winner: Optional[str]) -> None:
        """Handle market closure detected by HTTP scan."""
        if slug in self._contexts:
            await self._finalize_market(slug, winner)

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def _on_kill_switch(self, reason: str) -> None:
        """Handle kill switch activation."""
        logger.critical(f"Kill switch triggered: {reason}")
        await self.alert_manager.alert_kill_switch(reason)
        await self.stop(timeout=10.0)

    async def _submit_signal(
        self, ctx: MarketContext, signal: OrderSignal
    ) -> None:
        """Submit an order signal to the executor."""
        # Risk check
        if self.kill_switch.is_activated:
            return
        if not self.risk_manager.can_trade(
            market_id=ctx.slug, order_cost=signal.size * signal.target_price
        ):
            return

        ctx.record_order_submitted()

        # Resolve side
        token_type = (
            signal.token_type
            if isinstance(signal.token_type, str)
            else signal.token_type.value
        )
        side = "up" if token_type in ("yes", "YES") else "down"

        # Detect taker
        market_price = (
            ctx.last_up_price if side == "up" else ctx.last_down_price
        )
        is_taker = (
            signal.target_price >= market_price
            if market_price is not None
            else False
        )

        # Build execution order
        order = ExecutionOrder(
            market_id=ctx.slug,
            side=side,
            price=signal.target_price,
            size=signal.size,
            is_taker=is_taker,
            strategy_id=ctx.strategy.strategy_id,
        )

        # Submit to executor
        result = await self.executor.submit_order(order)

        # Link order ID back to strategy's pending list
        ctx.link_order(signal, result.order_id, is_taker)

        # Handle immediate fills
        if result.status == OrderResultStatus.FILLED:
            ctx.apply_fill(side, result.fill_size, result.fill_price, is_taker)
            ctx.remove_pending_order(result.order_id)
            self.fill_manager.process_fills([
                FillEvent(
                    order_id=result.order_id,
                    market_id=ctx.slug,
                    side=side,
                    fill_price=result.fill_price,
                    fill_size=result.fill_size,
                    is_taker=is_taker,
                    timestamp=time.time(),
                )
            ])

        elif result.status == OrderResultStatus.PARTIALLY_FILLED:
            ctx.apply_fill(side, result.fill_size, result.fill_price, is_taker)
            # Update pending order remaining size
            for p in ctx.strategy.pending_orders:
                if p.order_id == result.order_id:
                    p.shares = result.pending_size
                    p.cost = p.shares * p.price
                    break
            self.fill_manager.process_fills([
                FillEvent(
                    order_id=result.order_id,
                    market_id=ctx.slug,
                    side=side,
                    fill_price=result.fill_price,
                    fill_size=result.fill_size,
                    is_taker=is_taker,
                    is_partial=True,
                    remaining_size=result.pending_size,
                    timestamp=time.time(),
                )
            ])

    def _process_fill_events(
        self, ctx: MarketContext, events: List[FillEvent]
    ) -> None:
        """Process fill events from check_fills."""
        for event in events:
            if event.is_cancelled:
                ctx.remove_pending_order(event.order_id)
            elif event.is_partial:
                ctx.apply_fill(
                    event.side, event.fill_size, event.fill_price, event.is_taker
                )
                for p in ctx.strategy.pending_orders:
                    if p.order_id == event.order_id:
                        p.shares = event.remaining_size
                        p.cost = p.shares * p.price
                        break
            else:
                # Full fill
                ctx.apply_fill(
                    event.side, event.fill_size, event.fill_price, event.is_taker
                )
                ctx.remove_pending_order(event.order_id)

        self.fill_manager.process_fills(events)

    # ------------------------------------------------------------------
    # Market finalization
    # ------------------------------------------------------------------

    async def _finalize_market(
        self, slug: str, winner: Optional[str]
    ) -> None:
        """Finalize a market after settlement."""
        ctx = self._contexts.pop(slug, None)
        if not ctx:
            return

        # Cancel pending orders
        cancelled = await self.executor.cancel_all(slug)
        if cancelled > 0:
            logger.info(f"Cancelled {cancelled} pending orders for {slug}")

        # Unregister from components
        self.executor.unregister_market(slug)
        await self.data_provider.unsubscribe_market(slug)
        final_pos = self.fill_manager.unregister_market(slug)

        if winner:
            result = ctx.finalize(winner)
            self._results.append(result)
            self.stats.add_result(result)
            self._append_result(result)

            # Record result in risk manager
            self.risk_manager.record_result(slug, result.pnl)

            # Alert for significant settlements
            if abs(result.pnl) > 1.0:
                asyncio.create_task(
                    self.alert_manager.alert_settlement(
                        slug, winner, result.pnl, result.roi
                    )
                )

            logger.info(
                f"Market {slug} settled ({winner}): "
                f"PnL=${result.pnl:.2f}, ROI={result.roi * 100:.1f}%"
            )
        else:
            logger.warning(f"Market {slug} ended without winner")

    # ------------------------------------------------------------------
    # Metrics and output
    # ------------------------------------------------------------------

    async def _metrics_output_loop(self) -> None:
        """Periodically output metrics."""
        while self._running:
            try:
                self._write_status()
                self._append_metrics()

                # Log summary
                expected_pnl = 0.0
                price_info = ""
                for slug, ctx in self._contexts.items():
                    coin = ctx.coin.upper()
                    up_p, down_p = ctx.get_current_prices()
                    filled = ctx.result.orders_filled
                    submitted = ctx.result.orders_submitted

                    if ctx.prices_are_stale:
                        secs = (
                            int(time.time() - ctx.last_price_update_time)
                            if ctx.last_price_update_time > 0
                            else 0
                        )
                        price_str = f"STALE({secs}s)"
                    elif up_p is not None and down_p is not None:
                        price_str = f"{up_p:.2f}/{down_p:.2f}"
                    else:
                        price_str = "N/A"

                    hedged = min(ctx.result.up_shares, ctx.result.down_shares)
                    total_cost = ctx.result.total_cost
                    market_pnl = hedged - total_cost if hedged > 0 else 0
                    expected_pnl += market_pnl

                    ecr = total_cost / hedged if hedged > 0 else 0
                    ecr_str = f"{ecr:.2f}" if hedged > 0 else "-"
                    price_info += (
                        f" | {coin}: {price_str} ECR={ecr_str} "
                        f"({filled}/{submitted})"
                    )

                total_expected = self.stats.total_pnl + expected_pnl
                logger.info(
                    f"[{self.mode.upper()}] "
                    f"Realized=${self.stats.total_pnl:.2f}, "
                    f"Expected=${total_expected:.2f}, "
                    f"Markets={self.stats.markets_processed}, "
                    f"Active={len(self._contexts)}"
                    f"{price_info}"
                )

                await asyncio.sleep(self.config.metrics_output_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics output: {e}")
                await asyncio.sleep(60)

    async def _duration_watchdog(self) -> None:
        """Stop after configured duration."""
        if self.config.duration_seconds <= 0:
            return
        await asyncio.sleep(self.config.duration_seconds)
        logger.info(
            f"Duration ({self.config.duration_seconds}s) reached, shutting down"
        )
        await self.stop()

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(self._handle_signal(s))
            )

    async def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info(f"Received {sig.name}, initiating graceful shutdown...")
        await self.stop()

    # ------------------------------------------------------------------
    # File I/O (same format as LiveRunner for compatibility)
    # ------------------------------------------------------------------

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
        market_details = {}
        for slug, ctx in self._contexts.items():
            market_details[slug] = ctx.get_status()

        status = {
            "running": self._running,
            "mode": self.mode,
            "start_time": datetime.fromtimestamp(self.stats.start_time).isoformat(),
            "last_update": datetime.now().isoformat(),
            "config": {
                "coins": self.config.coins,
                "duration_seconds": self.config.duration_seconds,
                "target_cost": self.config.target_cost,
            },
            "stats": self.stats.to_dict(),
            "active_markets": list(self._contexts.keys()),
            "market_details": market_details,
        }

        try:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            tmp_file = self.status_file.with_suffix(f".tmp.{os.getpid()}")
            with open(tmp_file, "w") as f:
                json.dump(status, f, indent=2)
            os.replace(str(tmp_file), str(self.status_file))
        except OSError as e:
            logger.warning(f"Failed to write status: {e}")

    def _append_metrics(self) -> None:
        metrics = {
            "timestamp": datetime.now().isoformat(),
            "epoch": time.time(),
            **self.stats.to_dict(),
            "active_markets": len(self._contexts),
        }
        with open(self.metrics_file, "a") as f:
            f.write(json.dumps(metrics) + "\n")

    def _append_result(self, result: MarketResult) -> None:
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
        if self.status_file.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = self.config.output_dir / "archive" / f"status_{ts}.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            self.status_file.rename(archive)

    def _load_state(self) -> bool:
        if not self.status_file.exists():
            return False
        try:
            with open(self.status_file) as f:
                status = json.load(f)
            stats_data = status.get("stats", {})
            self.stats.markets_processed = stats_data.get("markets_processed", 0)
            self.stats.markets_won = stats_data.get("markets_won", 0)
            self.stats.markets_lost = stats_data.get("markets_lost", 0)
            self.stats.total_pnl = stats_data.get("total_pnl", 0.0)
            self.stats.total_cost = stats_data.get("total_cost", 0.0)
            logger.info(
                f"Resumed from checkpoint: "
                f"{self.stats.markets_processed} markets processed"
            )
            return True
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load state: {e}")
            return False
