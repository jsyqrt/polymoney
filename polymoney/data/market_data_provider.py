"""
MarketDataProvider - Centralized market data management.

Extracted from LiveRunner, this component manages:
- Market discovery via HTTP scanning (RealDataFetcher)
- Real-time price streaming via WebSocket
- Orderbook depth caching and refresh
- Settlement detection via price confirmation
- Token ID mapping (token_id -> (slug, side))

The provider communicates with consumers (StrategyEngine, OrderExecutor)
exclusively through async callbacks, keeping a clean separation of concerns.
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from polymoney.core.logging import get_logger
from polymoney.data.real_data_fetcher import (
    MarketEvent,
    MarketEventData,
    PriceUpdate,
    RealDataFetcher,
)
from polymoney.data.websocket_client import WebSocketManager
from polymoney.simulation.live_runner import OrderbookSnapshot, SimulationConfig

logger = get_logger("data.market_data_provider")


# Callback type aliases
MarketDiscoveredCallback = Callable[[MarketEventData], Awaitable[None]]
PriceUpdateCallback = Callable[[str, PriceUpdate], Awaitable[None]]  # slug, price
OrderbookUpdateCallback = Callable[[str, str, OrderbookSnapshot], Awaitable[None]]  # slug, side, snapshot
OrderbookIncrementalCallback = Callable[[str, str, Dict[str, Any]], Awaitable[None]]  # slug, side, changes
SettlementCallback = Callable[[str, str], Awaitable[None]]  # slug, winner
MarketClosedCallback = Callable[[str, Optional[str]], Awaitable[None]]  # slug, winner


class MarketDataProvider:
    """
    Centralized market data provider.
    
    Manages all Polymarket data sources (REST + WebSocket) and routes
    events to registered callbacks. Consumers register callbacks for:
    
    - Market discovery (new tradeable markets found)
    - Price updates (real-time from WS)
    - Orderbook updates (full snapshots and incremental deltas)
    - Settlements (price-based detection with multi-confirmation)
    - Market closures (HTTP-scan detected)
    
    Usage:
        provider = MarketDataProvider(config)
        provider.on_market_discovered = my_handler
        provider.on_price_update = my_price_handler
        await provider.start()
        ...
        await provider.stop()
    """

    def __init__(self, config: SimulationConfig):
        self.config = config
        
        # Data sources
        self._ws_manager: Optional[WebSocketManager] = None
        self._fetcher: Optional[RealDataFetcher] = None
        self._owns_fetcher = False  # Whether we created the fetcher
        
        # Token ID mapping: token_id -> (slug, side)
        self._token_to_market: Dict[str, Tuple[str, str]] = {}
        
        # Active market tracking
        self._active_markets: Set[str] = set()  # slugs
        self._skipped_markets: Set[str] = set()  # slugs we've logged as skipped
        
        # Market metadata: slug -> {condition_id, up_token_id, down_token_id, ...}
        self._market_meta: Dict[str, Dict[str, Any]] = {}
        
        # Settlement confirmation tracking: slug -> consecutive_confirmations
        self._settlement_confirmations: Dict[str, int] = {}
        self._settlement_confirmation_required: int = 3
        
        # Last known prices for settlement detection: slug -> (up_price, down_price, last_update_time)
        self._last_prices: Dict[str, Tuple[float, float, float]] = {}
        
        # Running state
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # Callbacks (set by consumer before start)
        self.on_market_discovered: Optional[MarketDiscoveredCallback] = None
        self.on_price_update: Optional[PriceUpdateCallback] = None
        self.on_orderbook_update: Optional[OrderbookUpdateCallback] = None
        self.on_orderbook_incremental: Optional[OrderbookIncrementalCallback] = None
        self.on_settlement: Optional[SettlementCallback] = None
        self.on_market_closed: Optional[MarketClosedCallback] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, fetcher: Optional[RealDataFetcher] = None) -> None:
        """
        Start the data provider.
        
        Args:
            fetcher: Optional pre-existing RealDataFetcher. If not provided,
                     one will be created and managed internally.
        """
        if self._running:
            return
        
        self._running = True
        
        # Setup data fetcher
        if fetcher:
            self._fetcher = fetcher
            self._owns_fetcher = False
        else:
            self._fetcher = RealDataFetcher()
            await self._fetcher.__aenter__()
            self._owns_fetcher = True
        
        # Setup WebSocket
        self._ws_manager = WebSocketManager()
        self._ws_manager.add_callback(self._on_ws_event)
        await self._ws_manager.start()
        logger.info("MarketDataProvider started (WS + HTTP)")
        
        # Start background loops
        self._tasks = [
            asyncio.create_task(self._market_scan_loop()),
            asyncio.create_task(self._settlement_check_loop()),
            asyncio.create_task(self._orderbook_refresh_loop()),
        ]

    async def stop(self) -> None:
        """Stop the data provider and clean up resources."""
        if not self._running:
            return
        
        self._running = False
        
        # Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        
        # Stop WebSocket
        if self._ws_manager:
            await self._ws_manager.stop()
            self._ws_manager = None
            logger.info("WebSocket manager stopped")
        
        # Clean up fetcher if we own it
        if self._owns_fetcher and self._fetcher:
            await self._fetcher.__aexit__(None, None, None)
            self._fetcher = None
        
        logger.info("MarketDataProvider stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_market(self, slug: str, meta: Dict[str, Any]) -> None:
        """
        Register a market as active (called after consumer accepts discovery).
        
        Args:
            slug: Market slug
            meta: Market metadata dict with token IDs, condition_id, etc.
        """
        self._active_markets.add(slug)
        self._market_meta[slug] = meta

    def unregister_market(self, slug: str) -> None:
        """Unregister a market (called after settlement/finalization)."""
        self._active_markets.discard(slug)
        self._market_meta.pop(slug, None)
        self._settlement_confirmations.pop(slug, None)
        self._last_prices.pop(slug, None)
        
        # Clean up token mappings
        to_remove = [
            tid for tid, (s, _) in self._token_to_market.items() if s == slug
        ]
        for tid in to_remove:
            del self._token_to_market[tid]

    def get_token_ids(self, slug: str) -> Tuple[str, str]:
        """Get (up_token_id, down_token_id) for a market."""
        meta = self._market_meta.get(slug, {})
        return (meta.get("up_token_id", ""), meta.get("down_token_id", ""))

    def get_market_meta(self, slug: str) -> Dict[str, Any]:
        """Get full metadata for a market."""
        return self._market_meta.get(slug, {})

    @property
    def active_market_count(self) -> int:
        return len(self._active_markets)

    async def subscribe_market(self, slug: str, meta: Dict[str, Any]) -> None:
        """Subscribe to WebSocket for a market and fetch initial orderbooks."""
        condition_id = meta.get("condition_id")
        up_token = meta.get("up_token_id")
        down_token = meta.get("down_token_id")

        # Register token mapping
        if up_token:
            self._token_to_market[up_token] = (slug, "up")
        if down_token:
            self._token_to_market[down_token] = (slug, "down")

        # Subscribe to WS
        if self._ws_manager and condition_id:
            token_ids = [t for t in [up_token, down_token] if t]
            if token_ids:
                await self._ws_manager.subscribe(condition_id, token_ids)
                logger.info(
                    f"Subscribed to WS for {slug} "
                    f"(condition_id={condition_id[:8]}...)"
                )

        # Fetch initial orderbooks via HTTP
        if self._fetcher:
            for side, token_id in [("up", up_token), ("down", down_token)]:
                if not token_id:
                    continue
                try:
                    raw = await self._fetcher.get_orderbook(token_id)
                    if raw:
                        snapshot = OrderbookSnapshot.from_raw(raw)
                        if self.on_orderbook_update:
                            await self.on_orderbook_update(slug, side, snapshot)
                        logger.info(
                            f"[{slug}] Initial {side.upper()} orderbook: "
                            f"bids={len(snapshot.bids)} asks={len(snapshot.asks)} "
                            f"spread={snapshot.spread:.4f}"
                        )
                except Exception as e:
                    logger.debug(
                        f"Failed to fetch initial {side} orderbook for {slug}: {e}"
                    )

    async def unsubscribe_market(self, slug: str) -> None:
        """Unsubscribe from WebSocket for a market."""
        meta = self._market_meta.get(slug, {})
        condition_id = meta.get("condition_id")

        if self._ws_manager and condition_id:
            await self._ws_manager.unsubscribe(condition_id)

        self.unregister_market(slug)

    # ------------------------------------------------------------------
    # Market validation
    # ------------------------------------------------------------------

    def is_market_valid(
        self,
        settlement_time: Optional[float],
        up_price: Optional[float],
        down_price: Optional[float],
        slug: str,
    ) -> Tuple[bool, str]:
        """
        Check if a market is valid for trading.
        
        Returns:
            (is_valid, reason_if_invalid)
        """
        now = time.time()

        if settlement_time:
            time_remaining = settlement_time - now
            if time_remaining <= 0:
                return False, "Already settled or settling"
            if time_remaining < self.config.min_trading_time:
                return False, (
                    f"Only {time_remaining:.0f}s remaining "
                    f"(need {self.config.min_trading_time:.0f}s)"
                )

        if up_price is not None and down_price is not None:
            price_sum = up_price + down_price
            min_price = min(up_price, down_price)

            if price_sum >= 0.98 and min_price < self.config.min_price_threshold:
                winner_side = "DOWN" if up_price < down_price else "UP"
                return False, (
                    f"Outcome decided ({winner_side} winning: "
                    f"UP={up_price:.3f}, DOWN={down_price:.3f})"
                )

            max_price = max(up_price, down_price)
            if max_price > self.config.max_entry_skew:
                dominant = "UP" if up_price > down_price else "DOWN"
                return False, (
                    f"Too skewed at entry ({dominant}={max_price:.1%}, "
                    f"threshold={self.config.max_entry_skew:.0%})"
                )

        return True, ""

    def can_start_new_market(self) -> bool:
        """Check if a new market can be started within global limits."""
        if len(self._active_markets) >= self.config.max_concurrent_markets:
            return False
        return True

    # ------------------------------------------------------------------
    # WebSocket event handler
    # ------------------------------------------------------------------

    def _on_ws_event(self, event_type: str, identifier: str, data: Any) -> None:
        """
        Handle WebSocket events.
        
        Routes token_price, token_orderbook, and token_trade events
        to appropriate callbacks.
        """
        if event_type == "token_price":
            self._handle_token_price(identifier, data)
        elif event_type == "token_orderbook":
            self._handle_token_orderbook(identifier, data)
        elif event_type == "token_trade":
            self._handle_token_trade(identifier, data)

    def _handle_token_price(self, token_id: str, data: Dict[str, Any]) -> None:
        """Handle per-token price update."""
        if token_id not in self._token_to_market:
            return

        slug, side = self._token_to_market[token_id]
        if slug not in self._active_markets:
            return

        mid_price = data.get("mid_price")
        best_bid = data.get("best_bid")
        best_ask = data.get("best_ask")

        if mid_price is None:
            return

        spread = (
            (best_ask - best_bid)
            if (best_ask is not None and best_bid is not None)
            else 0.0
        )

        # Garbage filter
        if spread > 0.50:
            logger.warning(
                f"[WS] Garbage token_price for {token_id[:12]}... "
                f"(spread={spread:.2f}). Dropped."
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

        # Update last known prices for settlement detection
        current = self._last_prices.get(slug, (0.0, 0.0, 0.0))
        if side == "up":
            self._last_prices[slug] = (mid_price, current[1], time.time())
        else:
            self._last_prices[slug] = (current[0], mid_price, time.time())

        # Notify callback
        if self.on_price_update:
            # Create task to avoid blocking WS thread
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.on_price_update(slug, price_update))
            except RuntimeError:
                pass

    def _handle_token_orderbook(self, token_id: str, data: Dict[str, Any]) -> None:
        """Handle per-token orderbook update."""
        if token_id not in self._token_to_market:
            return

        slug, side = self._token_to_market[token_id]
        if slug not in self._active_markets:
            return

        is_incremental = data.get("type") == "delta" or data.get("incremental", False)

        if is_incremental:
            if self.on_orderbook_incremental:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(
                            self.on_orderbook_incremental(slug, side, data)
                        )
                except RuntimeError:
                    pass
        else:
            snapshot = OrderbookSnapshot.from_raw(data)
            if self.on_orderbook_update:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(
                            self.on_orderbook_update(slug, side, snapshot)
                        )
                except RuntimeError:
                    pass

    def _handle_token_trade(self, token_id: str, data: Dict[str, Any]) -> None:
        """Handle per-token trade event."""
        if token_id not in self._token_to_market:
            return

        slug, side = self._token_to_market[token_id]
        if slug not in self._active_markets:
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

            if self.on_price_update:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(self.on_price_update(slug, price_update))
                except RuntimeError:
                    pass

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _market_scan_loop(self) -> None:
        """Periodically scan for new markets via HTTP."""
        while self._running:
            try:
                if not self._fetcher:
                    await asyncio.sleep(self.config.market_scan_interval)
                    continue

                markets_by_coin = await self._fetcher.find_multi_coin_markets(
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
                            # Notify about closed markets
                            if slug in self._active_markets:
                                winner = market.get("winner")
                                if self.on_market_closed:
                                    await self.on_market_closed(slug, winner)
                        else:
                            # Discover new active markets
                            if slug not in self._active_markets:
                                await self._discover_market(coin, market)

                await asyncio.sleep(self.config.market_scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market scan: {e}")
                await asyncio.sleep(30)

    async def _discover_market(self, coin: str, market: Dict[str, Any]) -> None:
        """Process a newly discovered market."""
        slug = market.get("slug", "")

        # Check global limits
        if not self.can_start_new_market():
            return

        # Parse settlement time
        settlement_time = None
        end_date_str = market.get("end_date")
        if end_date_str:
            try:
                end_dt = datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
                settlement_time = end_dt.timestamp()
            except (ValueError, TypeError):
                pass

        # Fetch current prices for validation
        up_price = None
        down_price = None
        up_token_id = market.get("up_token_id")
        down_token_id = market.get("down_token_id")

        if self._fetcher:
            if up_token_id:
                pu = await self._fetcher.get_mid_price(up_token_id)
                if pu:
                    up_price = pu.mid_price
            if down_token_id:
                pu = await self._fetcher.get_mid_price(down_token_id)
                if pu:
                    down_price = pu.mid_price

        # Validate market
        is_valid, reason = self.is_market_valid(
            settlement_time, up_price, down_price, slug
        )
        if not is_valid:
            if slug not in self._skipped_markets:
                logger.info(f"Skipping market {slug}: {reason}")
                self._skipped_markets.add(slug)
            return

        # Create event and notify callback
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

        if self.on_market_discovered:
            await self.on_market_discovered(event)

    async def _settlement_check_loop(self) -> None:
        """
        Check for settlements using two methods:
        
        1. WS price confirmation: if one side >= 0.97 and the other <= 0.04
           for 3 consecutive checks (~15s), declare settlement.
        2. HTTP fallback: for markets past their settlement_time + grace
           period, query the Gamma Markets API directly to check closure.
           This catches cases where WS prices stall or never reach the
           extreme thresholds (e.g., UP=0.985 but <0.99).
        """
        http_check_interval = 30.0  # seconds between HTTP settlement checks
        last_http_check = 0.0

        while self._running:
            try:
                markets_to_settle: List[Tuple[str, str]] = []
                checked_slugs: Set[str] = set()
                now = time.time()

                for slug in list(self._active_markets):
                    checked_slugs.add(slug)

                    prices = self._last_prices.get(slug)
                    if not prices:
                        self._settlement_confirmations.pop(slug, None)
                        continue

                    up_p, down_p, last_update = prices
                    
                    # Skip if prices are stale (>10s old)
                    if now - last_update > 10.0:
                        self._settlement_confirmations.pop(slug, None)
                        continue

                    price_sum = up_p + down_p
                    if price_sum < 0.95 or price_sum > 1.05:
                        self._settlement_confirmations.pop(slug, None)
                        continue

                    winner = None
                    if up_p >= 0.97 and down_p <= 0.04:
                        winner = "up"
                    elif down_p >= 0.97 and up_p <= 0.04:
                        winner = "down"

                    if winner:
                        count = self._settlement_confirmations.get(slug, 0) + 1
                        self._settlement_confirmations[slug] = count
                        logger.debug(
                            f"Settlement check: {slug} {winner.upper()} "
                            f"(up={up_p:.3f}, down={down_p:.3f}) "
                            f"confirmation {count}/{self._settlement_confirmation_required}"
                        )
                        if count >= self._settlement_confirmation_required:
                            markets_to_settle.append((slug, winner))
                    else:
                        if slug in self._settlement_confirmations:
                            self._settlement_confirmations.pop(slug, None)

                # --- HTTP fallback for overdue markets ---
                if now - last_http_check >= http_check_interval:
                    last_http_check = now
                    http_settled = await self._check_overdue_settlements()
                    for slug, winner in http_settled:
                        if slug not in [s for s, _ in markets_to_settle]:
                            markets_to_settle.append((slug, winner))

                # Clean stale confirmations
                stale_keys = (
                    set(self._settlement_confirmations.keys()) - checked_slugs
                )
                for key in stale_keys:
                    del self._settlement_confirmations[key]

                # Notify settlements
                for slug, winner in markets_to_settle:
                    logger.info(
                        f"Settlement detected for {slug}: {winner} wins "
                        f"(confirmed {self._settlement_confirmation_required} times)"
                    )
                    self._settlement_confirmations.pop(slug, None)
                    if self.on_settlement:
                        await self.on_settlement(slug, winner)

                await asyncio.sleep(5.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in settlement check: {e}")
                await asyncio.sleep(5)

    async def _check_overdue_settlements(self) -> List[Tuple[str, str]]:
        """
        HTTP fallback: check active markets that are past their settlement
        time via the Gamma Markets API.
        
        Returns list of (slug, winner) for markets confirmed as settled.
        """
        results: List[Tuple[str, str]] = []
        if not self._fetcher:
            return results

        now = time.time()
        grace_period = 60.0  # wait 60s after settlement_time before HTTP check

        for slug in list(self._active_markets):
            meta = self._market_meta.get(slug, {})
            settlement_time = meta.get("settlement_time")
            if not settlement_time:
                continue

            if now < settlement_time + grace_period:
                continue

            # Market is overdue — query API for settlement status
            try:
                event_data = await self._fetcher.check_market_state(meta)
                if event_data and event_data.event_type == MarketEvent.MARKET_SETTLED:
                    winner = event_data.winner
                    if winner:
                        logger.info(
                            f"HTTP settlement fallback: {slug} → "
                            f"{winner.upper()} wins (API confirmed)"
                        )
                        results.append((slug, winner))
                    else:
                        # Market closed but no clear winner — try price-based
                        prices = self._last_prices.get(slug)
                        if prices:
                            up_p, down_p, _ = prices
                            if up_p > down_p and up_p > 0.80:
                                logger.info(
                                    f"HTTP settlement fallback: {slug} → "
                                    f"UP wins (price={up_p:.2f}, closed)"
                                )
                                results.append((slug, "up"))
                            elif down_p > up_p and down_p > 0.80:
                                logger.info(
                                    f"HTTP settlement fallback: {slug} → "
                                    f"DOWN wins (price={down_p:.2f}, closed)"
                                )
                                results.append((slug, "down"))
                            else:
                                logger.warning(
                                    f"HTTP settlement: {slug} closed but "
                                    f"no clear winner (up={up_p:.2f}, "
                                    f"down={down_p:.2f})"
                                )
            except Exception as e:
                logger.debug(f"HTTP settlement check failed for {slug}: {e}")

    async def _orderbook_refresh_loop(self) -> None:
        """Periodically refresh orderbooks via HTTP for active markets."""
        while self._running:
            try:
                if not self._fetcher:
                    await asyncio.sleep(15.0)
                    continue

                for slug in list(self._active_markets):
                    meta = self._market_meta.get(slug, {})
                    up_token = meta.get("up_token_id")
                    down_token = meta.get("down_token_id")

                    for side, token_id in [("up", up_token), ("down", down_token)]:
                        if not token_id:
                            continue
                        try:
                            raw = await self._fetcher.get_orderbook(token_id)
                            if raw:
                                snapshot = OrderbookSnapshot.from_raw(raw)
                                if self.on_orderbook_update:
                                    await self.on_orderbook_update(
                                        slug, side, snapshot
                                    )
                                logger.debug(
                                    f"[HTTP Book] {slug} {side.upper()}: "
                                    f"bids={len(snapshot.bids)} "
                                    f"asks={len(snapshot.asks)}"
                                )
                        except Exception as e:
                            logger.debug(
                                f"Failed to fetch {side} orderbook for {slug}: {e}"
                            )

                await asyncio.sleep(15.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in orderbook refresh: {e}")
                await asyncio.sleep(15)
