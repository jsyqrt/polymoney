"""
TradingRunner - Top-level orchestrator for paper and live trading.

Wires together:
- MarketDataProvider (data layer)
- MarketContext (per-market strategy state)
- OrderExecutor (simulated or live execution)
- FillManager (fill tracking and position reconciliation)
- Metrics output (status.json, metrics.jsonl, results.jsonl)

Supports both paper trading (SimulatedExecutor) and real trading
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
from polymoney.execution.redeemer import PaperRedeemer, PositionRedeemer
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
        self.fill_manager = FillManager(
            trade_log_path=trade_log,
        )

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

        # Position redeemer (initialized below for paper, in _init_clob_client for live)
        if mode == "live":
            self.redeemer = None  # will be set in _init_clob_client
        else:
            self.redeemer = PaperRedeemer(
                redemption_delay=getattr(config, "redemption_delay", 60.0)
            )

        # Safety flag: set True if any redemption fails, blocks new market entry
        self._redeem_failed = False

        # CLOB client reference for balance queries (set in _init_clob_client)
        self._clob_client = None

        # Available cash tracking: prevents entering markets without sufficient funds.
        # Live mode: fetched from Polymarket API (USDC collateral balance).
        # Paper mode: initialized from config as fallback.
        # Between API refreshes, deducted on fills and restored on redemption.
        self._available_cash: float = getattr(config, "initial_balance", config.max_total_exposure)
        self._unredeemed_markets: Set[str] = set()

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
            if not self._preflight_checks():
                logger.error("Preflight checks FAILED — aborting to prevent losses")
                self._running = False
                raise RuntimeError(
                    "Preflight checks failed. Fix the issues above before trading."
                )

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

        # Start position redeemer background scan (live mode)
        if self.redeemer is not None:
            await self.redeemer.start_background_scan()

        # Start background tasks
        tasks = [
            asyncio.create_task(self._metrics_output_loop()),
        ]
        if self.mode == "live":
            tasks.append(asyncio.create_task(self._position_reconciliation_loop()))
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

            # Stop position redeemer
            if self.redeemer is not None:
                await self.redeemer.stop()

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

            # Validate wallet and set up API credentials
            try:
                # For Magic wallets (signature_type=1), create API creds automatically
                if pm_config.signature_type == 1:
                    api_creds = client.create_or_derive_api_creds()
                    client.set_api_creds(api_creds)
                    logger.info(f"CLOB client initialized with auto-derived API credentials for Magic wallet")
                else:
                    # For EOA wallets, derive API key
                    api_keys = client.derive_api_key()
                    logger.info(f"CLOB client initialized (API key derived)")
            except Exception as e:
                logger.warning(
                    f"CLOB client initialized but API credential setup failed: {e}. "
                    f"You may need to create API credentials on Polymarket."
                )

            # Keep a reference for balance queries
            self._clob_client = client

            # Set client on executor
            if isinstance(self.executor, LiveExecutor):
                self.executor.set_client(client)

            # Fetch real USDC balance from Polymarket
            api_balance = self._fetch_usdc_balance()
            if api_balance is not None:
                self._available_cash = api_balance
                logger.info(f"USDC balance from API: ${api_balance:.2f}")
            else:
                logger.warning(
                    f"Could not fetch USDC balance from API, "
                    f"using config fallback: ${self._available_cash:.2f}"
                )

            logger.info(
                f"Live executor ready: host={pm_config.host}, "
                f"chain_id={pm_config.chain_id}"
            )

            # Initialize position redeemer for automatic fund recovery
            try:
                self.redeemer = PositionRedeemer(
                    private_key=pm_config.private_key,
                    funder=pm_config.funder,
                    signature_type=pm_config.signature_type,
                    builder_api_key=pm_config.builder_api_key,
                    builder_secret=pm_config.builder_secret,
                    builder_passphrase=pm_config.builder_passphrase,
                )
                if self.redeemer.is_available:
                    self.redeemer.on_scan_result = self._on_redeemer_scan_result
                    logger.info("Position redeemer initialized for automatic redemption")
                else:
                    logger.warning(
                        "polymarket-apis not installed; auto-redeem disabled. "
                        "Install with: pip install polymarket-apis"
                    )
                    self.redeemer = None
            except Exception as e:
                logger.warning(f"Position redeemer initialization failed: {e}")
                self.redeemer = None

        except ImportError:
            raise RuntimeError(
                "py-clob-client is required for live trading. "
                "Install with: pip install py-clob-client"
            )

    def _fetch_usdc_balance(self) -> Optional[float]:
        """Query Polymarket API for the account's USDC collateral balance.

        The CLOB ``/balance-allowance`` endpoint returns the on-chain ERC-20
        balance as a string in the token's smallest unit.  USDC on Polygon has
        6 decimals, so the raw value is divided by 1e6.
        """
        if self._clob_client is None:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            resp = self._clob_client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if resp and isinstance(resp, dict):
                raw = resp.get("balance", "0")
                balance = float(raw) / 1e6  # USDC has 6 decimals on Polygon
                logger.debug(f"USDC balance API raw={raw}, parsed=${balance:.6f}")
                return balance
            logger.warning(f"Unexpected balance API response: {resp}")
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch USDC balance: {e}")
            return None

    # ------------------------------------------------------------------
    # Preflight checks (live mode)
    # ------------------------------------------------------------------

    def _preflight_checks(self) -> bool:
        """Verify all critical interfaces before trading starts.

        Returns True if all checks pass, False if any fail.
        A failure here means trading MUST NOT proceed — the system would
        lose money due to inaccurate position tracking or broken APIs.
        """
        checks_passed = True
        failures: list = []

        def _check(name: str, ok: bool, detail: str = "") -> None:
            nonlocal checks_passed
            status = "OK" if ok else "FAIL"
            msg = f"  [{status}] {name}"
            if detail:
                msg += f": {detail}"
            if ok:
                logger.info(msg)
            else:
                logger.error(msg)
                failures.append(name)
                checks_passed = False

        logger.info("=" * 60)
        logger.info("PREFLIGHT CHECKS — verifying all interfaces before trading")
        logger.info("=" * 60)

        # 1. CLOB client exists
        _check(
            "CLOB client initialized",
            self._clob_client is not None,
        )
        if not self._clob_client:
            logger.error("Cannot continue preflight without CLOB client")
            return False

        # 2. Maker address extracted (critical for trade verification)
        maker_addr = None
        if isinstance(self.executor, LiveExecutor):
            maker_addr = self.executor._maker_address
        _check(
            "Maker address extracted",
            maker_addr is not None,
            maker_addr[:16] + "..." if maker_addr else "MISSING — fills will use limit price, not real VWAP",
        )

        # 3. L2 auth (required for get_trades, get_order, post_order)
        l2_ok = False
        try:
            self._clob_client.assert_level_2_auth()
            l2_ok = True
        except Exception:
            pass
        _check(
            "L2 authentication",
            l2_ok,
            "" if l2_ok else "get_trades/get_order will fail — cannot verify fills",
        )

        # 4. USDC balance API
        balance = self._fetch_usdc_balance()
        balance_ok = balance is not None and balance > 0
        _check(
            "USDC balance API",
            balance_ok,
            f"${balance:.2f}" if balance is not None else "API returned None",
        )

        # 5. get_trades API (actual round-trip test)
        trades_ok = False
        trades_detail = "skipped (requires L2 auth + maker address)"
        if l2_ok and maker_addr:
            try:
                from py_clob_client.clob_types import TradeParams
                result_trades = self._clob_client.get_trades(
                    params=TradeParams(maker_address=maker_addr)
                )
                trades_ok = isinstance(result_trades, list)
                trades_detail = f"returned {len(result_trades)} trades" if trades_ok else "unexpected response type"
            except Exception as e:
                trades_detail = str(e)
        _check("Trades API (get_trades)", trades_ok, trades_detail)

        # 6. get_order API (test with a dummy ID — expect graceful failure)
        order_api_ok = False
        if l2_ok:
            try:
                self._clob_client.get_order("0x" + "0" * 64)
                order_api_ok = True  # even None/empty is fine, no exception
            except Exception as e:
                err_str = str(e).lower()
                if "not found" in err_str or "404" in err_str:
                    order_api_ok = True  # expected — API is reachable
        _check(
            "Order status API (get_order)",
            order_api_ok,
            "" if order_api_ok else "cannot poll order fill status",
        )

        # 7. Server connectivity
        server_ok = False
        try:
            resp = self._clob_client.get_ok()
            server_ok = resp == "OK" or bool(resp)
        except Exception:
            pass
        _check("CLOB server connectivity", server_ok)

        logger.info("=" * 60)
        if checks_passed:
            logger.info("ALL PREFLIGHT CHECKS PASSED — safe to trade")
        else:
            logger.error(
                f"PREFLIGHT FAILED: {len(failures)} check(s) failed: "
                + ", ".join(failures)
            )
        logger.info("=" * 60)

        return checks_passed

    # ------------------------------------------------------------------
    # Data provider callbacks
    # ------------------------------------------------------------------

    async def _on_market_discovered(self, event: MarketEventData) -> None:
        """Handle new market discovery."""
        slug = event.market_slug

        if slug in self._contexts:
            return

        # Safety: block new markets only if cash is critically low
        if self._redeem_failed:
            logger.warning(
                f"Skipping {slug}: trading suspended (critical cash shortage)"
            )
            return

        # Log pending redemptions but don't block — cash check below is sufficient
        if self.fill_manager.has_pending_redemptions:
            logger.debug(
                f"Note: {self.fill_manager.pending_redemption_count} "
                f"redemption(s) pending during market discovery"
            )

        # Block if background redeemer found unredeemed positions it can't recover
        if self._unredeemed_markets:
            logger.info(
                f"Skipping {slug}: {len(self._unredeemed_markets)} unredeemed market(s) "
                f"from prior runs blocking new entry"
            )
            return

        # Check available cash — need at least position_size for a new market
        if self._available_cash < self.config.target_cost * 5:
            logger.info(
                f"Skipping {slug}: insufficient cash "
                f"(${self._available_cash:.2f} available, need "
                f"${self.config.target_cost * 5:.2f} minimum)"
            )
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
            fill_cost = result.fill_size * result.fill_price
            self._available_cash -= fill_cost
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
            fill_cost = result.fill_size * result.fill_price
            self._available_cash -= fill_cost
            ctx.apply_fill(side, result.fill_size, result.fill_price, is_taker)
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
                fill_cost = event.fill_size * event.fill_price
                self._available_cash -= fill_cost
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
                fill_cost = event.fill_size * event.fill_price
                self._available_cash -= fill_cost
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
        final_pos = self.fill_manager.unregister_market(slug, winner=winner)

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

            # Automatic redemption: recover capital from settled market
            settlement_value = (
                result.up_shares if winner == "up" else result.down_shares
            )
            if self.redeemer is not None and result.condition_id:
                asyncio.create_task(
                    self._auto_redeem(result.condition_id, slug, settlement_value)
                )
            else:
                # No redeemer available — release capital immediately
                self.fill_manager.release_redemption(slug)
                self._available_cash += settlement_value
        else:
            logger.warning(f"Market {slug} ended without winner")

    async def _auto_redeem(
        self, condition_id: str, slug: str, settlement_value: float
    ) -> None:
        """
        Attempt automatic redemption after settlement.

        Runs as a fire-and-forget task so it doesn't block the main loop.
        Retries up to 3 times with exponential backoff if the Data API
        hasn't yet reflected the settlement.

        SAFETY: If all retries fail, sets _redeem_failed flag which
        blocks new market entry and triggers graceful shutdown.
        """
        max_retries = 3

        for attempt in range(max_retries):
            try:
                results = await self.redeemer.redeem_market(
                    condition_id, settlement_value=settlement_value
                )

                if results:
                    successes = [r for r in results if r.success]
                    failures = [r for r in results if not r.success]
                    total_value = sum(r.value_redeemed for r in successes)

                    if successes and not failures:
                        # Full success — restore cash
                        self.fill_manager.release_redemption(slug)
                        self._available_cash += total_value
                        logger.info(
                            f"Auto-redeem {slug}: {len(successes)} position(s) "
                            f"redeemed, ${total_value:.2f} recovered "
                            f"(available: ${self._available_cash:.2f})"
                        )
                        return
                    elif successes:
                        # Partial success
                        logger.warning(
                            f"Auto-redeem {slug}: partial — "
                            f"{len(successes)} ok, {len(failures)} failed"
                        )
                        # Treat partial as failure — capital not fully recovered
                    else:
                        # All failed
                        logger.warning(
                            f"Auto-redeem {slug}: all {len(results)} attempts failed"
                        )
                else:
                    # No positions found yet — may not be reflected in Data API
                    if attempt < max_retries - 1:
                        wait = 30 * (2 ** attempt)
                        logger.info(
                            f"Auto-redeem {slug}: no positions found yet, "
                            f"retry in {wait}s (attempt {attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(wait)
                        continue

                # If we got results but had failures, retry
                if attempt < max_retries - 1:
                    wait = 30 * (2 ** attempt)
                    logger.info(
                        f"Auto-redeem {slug}: retrying in {wait}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue

                break
            except Exception as e:
                logger.error(f"Auto-redeem {slug} error: {e}", exc_info=True)
                if attempt < max_retries - 1:
                    wait = 30 * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                break

        # All retries exhausted — trigger safety stop
        await self._on_redeem_failure(slug)

    def _on_redeemer_scan_result(
        self, unredeemed_count: int, failed_titles: List[str]
    ) -> None:
        """Handle background redeemer scan results.

        If unredeemed positions from prior runs are found and cannot be
        redeemed, block new market entry to prevent trading without
        sufficient available capital.
        """
        if unredeemed_count == 0:
            if self._unredeemed_markets:
                logger.info(
                    "All prior unredeemed positions cleared — resuming trading"
                )
                self._unredeemed_markets.clear()
        else:
            self._unredeemed_markets = set(failed_titles)
            logger.warning(
                f"Background scan: {unredeemed_count} position(s) cannot be redeemed. "
                f"New market entry blocked until resolved."
            )

    async def _on_redeem_failure(self, slug: str) -> None:
        """Handle redemption failure gracefully without halting all trading.

        Capital from the settled market stays locked. New market entry
        continues if sufficient cash remains. The failed redemption is
        scheduled for background retry with exponential backoff (5m, 15m,
        30m). Only halts if available cash drops below safety threshold.
        """
        pending = self.fill_manager.pending_redemption_count
        locked_value = sum(self.fill_manager._pending_redemptions.values())

        msg = (
            f"REDEMPTION FAILED for {slug}. "
            f"{pending} redemption(s) pending, ${locked_value:.2f} locked. "
            f"Scheduling background retry. "
            f"Available cash: ${self._available_cash:.2f}"
        )
        logger.error(msg)

        # Send alert (non-critical — don't halt)
        try:
            await self.alert_manager.send_alert(
                title="Redemption Failure — Retrying",
                message=msg,
                level="warning",
            )
        except Exception:
            pass

        # Schedule background retry with longer backoff
        asyncio.create_task(
            self._retry_redemption(slug, locked_value)
        )

        # Only halt if cash is critically low (can't fund even one market)
        min_cash = self.config.target_cost * 5
        if self._available_cash < min_cash:
            logger.critical(
                f"Available cash ${self._available_cash:.2f} below "
                f"minimum ${min_cash:.2f} — halting trading"
            )
            self._redeem_failed = True
            await self.stop(timeout=30.0)

    async def _retry_redemption(self, slug: str, expected_value: float) -> None:
        """Background retry loop for failed redemptions with exponential backoff."""
        delays = [300, 900, 1800]  # 5m, 15m, 30m
        meta = None
        for ctx_slug, ctx in list(self._contexts.items()):
            if ctx_slug == slug:
                meta = ctx.market
                break

        condition_id = ""
        if meta:
            condition_id = meta.get("condition_id", "")
        elif slug in self.fill_manager._pending_redemptions:
            # Try to find condition_id from results
            for r in self._results:
                if r.slug == slug and r.condition_id:
                    condition_id = r.condition_id
                    break

        if not condition_id or self.redeemer is None:
            logger.warning(
                f"Cannot retry redemption for {slug}: "
                f"no condition_id or redeemer"
            )
            return

        for i, delay in enumerate(delays):
            logger.info(
                f"Redemption retry for {slug} in {delay}s "
                f"(attempt {i + 1}/{len(delays)})"
            )
            await asyncio.sleep(delay)

            if not self._running:
                return

            try:
                results = await self.redeemer.redeem_market(
                    condition_id, settlement_value=expected_value
                )
                if results:
                    successes = [r for r in results if r.success]
                    if successes:
                        total_value = sum(r.value_redeemed for r in successes)
                        self.fill_manager.release_redemption(slug)
                        self._available_cash += total_value
                        logger.info(
                            f"Redemption retry SUCCESS for {slug}: "
                            f"${total_value:.2f} recovered"
                        )
                        return
            except Exception as e:
                logger.warning(f"Redemption retry failed for {slug}: {e}")

        logger.error(
            f"All redemption retries exhausted for {slug}. "
            f"Capital remains locked — manual intervention required."
        )

    # ------------------------------------------------------------------
    # Metrics and output
    # ------------------------------------------------------------------

    async def _metrics_output_loop(self) -> None:
        """Periodically output metrics."""
        _balance_refresh_counter = 0
        while self._running:
            try:
                # Periodically sync _available_cash with the real API balance
                # (every 5 metrics cycles ≈ every 5 × metrics_output_interval seconds).
                _balance_refresh_counter += 1
                if self.mode == "live" and _balance_refresh_counter % 5 == 0:
                    api_balance = self._fetch_usdc_balance()
                    if api_balance is not None:
                        self._available_cash = api_balance

                # Track peak capital exposure for capital efficiency metrics
                current_exposure = self.fill_manager.get_total_exposure()
                self.stats.update_exposure(current_exposure)

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
                    if hedged > 0:
                        ecr_str = f"{ecr:.2f}"
                    elif total_cost > 0:
                        # Has cost but one-sided position — show which side
                        one_side = "UP" if ctx.result.up_shares > 0 else "DOWN"
                        ecr_str = f"1-side({one_side})"
                    else:
                        ecr_str = "-"
                    price_info += (
                        f" | {coin}: {price_str} ECR={ecr_str} "
                        f"({filled}/{submitted})"
                    )

                total_expected = self.stats.total_pnl + expected_pnl
                cash_str = f", Cash=${self._available_cash:.2f}" if self.mode == "live" else ""
                logger.info(
                    f"[{self.mode.upper()}] "
                    f"Realized=${self.stats.total_pnl:.2f}, "
                    f"Expected=${total_expected:.2f}, "
                    f"Markets={self.stats.markets_processed}, "
                    f"Active={len(self._contexts)}"
                    f"{cash_str}"
                    f"{price_info}"
                )

                await asyncio.sleep(self.config.metrics_output_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics output: {e}")
                await asyncio.sleep(60)

    async def _position_reconciliation_loop(self) -> None:
        """
        Periodically compare internal position tracking with on-chain state.
        
        Queries the Trades API for each active market's token and compares
        total verified shares with the FillManager's internal tracking.
        Logs discrepancies and corrects internal state when the on-chain
        data is authoritative.
        """
        reconcile_interval = 120.0  # every 2 minutes
        await asyncio.sleep(30.0)  # initial delay to let markets initialize

        while self._running:
            try:
                if not self._clob_client:
                    await asyncio.sleep(reconcile_interval)
                    continue

                maker_address = None
                if hasattr(self.executor, "_maker_address"):
                    maker_address = self.executor._maker_address
                if not maker_address:
                    await asyncio.sleep(reconcile_interval)
                    continue

                for slug, ctx in list(self._contexts.items()):
                    meta = ctx.market
                    up_token = meta.get("up_token_id", "")
                    down_token = meta.get("down_token_id", "")

                    if not up_token or not down_token:
                        continue

                    try:
                        from py_clob_client.clob_types import TradeParams

                        onchain_up = 0.0
                        onchain_down = 0.0

                        for side, token_id in [("up", up_token), ("down", down_token)]:
                            params = TradeParams(
                                maker_address=maker_address,
                                asset_id=token_id,
                            )
                            trades = self._clob_client.get_trades(params=params)
                            if not trades or not isinstance(trades, list):
                                continue

                            total = 0.0
                            for trade in trades:
                                status = trade.get("status", "")
                                if status in (
                                    "TRADE_STATUS_CONFIRMED",
                                    "TRADE_STATUS_MINED",
                                    "TRADE_STATUS_MATCHED",
                                ):
                                    try:
                                        total += float(trade.get("size", 0))
                                    except (ValueError, TypeError):
                                        continue

                            if side == "up":
                                onchain_up = total
                            else:
                                onchain_down = total

                        pos = self.fill_manager.get_position(slug)
                        if not pos:
                            continue

                        up_diff = pos.up_shares - onchain_up
                        down_diff = pos.down_shares - onchain_down

                        threshold = 0.5  # tolerate < 0.5 share difference
                        if abs(up_diff) > threshold or abs(down_diff) > threshold:
                            logger.warning(
                                f"RECONCILIATION MISMATCH [{slug}]: "
                                f"Internal UP={pos.up_shares:.2f} vs Chain={onchain_up:.2f} "
                                f"(diff={up_diff:+.2f}), "
                                f"Internal DOWN={pos.down_shares:.2f} vs Chain={onchain_down:.2f} "
                                f"(diff={down_diff:+.2f})"
                            )

                            # Correct internal state to match on-chain (authoritative)
                            if onchain_up < pos.up_shares and up_diff > threshold:
                                correction = up_diff
                                pos.up_shares = onchain_up
                                logger.info(
                                    f"RECONCILIATION CORRECTED [{slug}]: "
                                    f"UP shares reduced by {correction:.2f} "
                                    f"(phantom fills removed)"
                                )
                            if onchain_down < pos.down_shares and down_diff > threshold:
                                correction = down_diff
                                pos.down_shares = onchain_down
                                logger.info(
                                    f"RECONCILIATION CORRECTED [{slug}]: "
                                    f"DOWN shares reduced by {correction:.2f} "
                                    f"(phantom fills removed)"
                                )
                        else:
                            logger.debug(
                                f"Reconciliation OK [{slug}]: "
                                f"UP={pos.up_shares:.2f}, DOWN={pos.down_shares:.2f}"
                            )

                    except ImportError:
                        logger.debug("TradeParams not available for reconciliation")
                        break
                    except Exception as e:
                        logger.debug(f"Reconciliation failed for {slug}: {e}")

                await asyncio.sleep(reconcile_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in position reconciliation: {e}")
                await asyncio.sleep(reconcile_interval)

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
