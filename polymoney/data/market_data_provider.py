"""
MarketDataProvider - Centralized market data management.

Extracted from LiveRunner, this component manages:
- Market discovery via HTTP scanning (RealDataFetcher)
- Real-time price streaming via WebSocket (including best_bid_ask)
- Orderbook depth caching and refresh
- Settlement detection via authoritative exchange data only
  (WS market_resolved events + HTTP API outcome_prices)
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
        
        # Last known prices (used by price_update callback, NOT for settlement)
        self._last_prices: Dict[str, Tuple[float, float, float]] = {}
        
        # Markets already resolved via WS (avoid duplicate HTTP detection)
        self._ws_resolved_markets: Set[str] = set()
        
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
        self._last_prices.pop(slug, None)
        self._ws_resolved_markets.discard(slug)
        
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
        
        Routes token_price, token_orderbook, token_trade, and
        market_resolved events to appropriate callbacks.
        """
        if event_type == "token_price":
            self._handle_token_price(identifier, data)
        elif event_type == "token_orderbook":
            self._handle_token_orderbook(identifier, data)
        elif event_type == "token_trade":
            self._handle_token_trade(identifier, data)
        elif event_type == "market_resolved":
            self._handle_market_resolved(identifier, data)

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
        source = data.get("source", "")

        if mid_price is None:
            return

        spread = (
            (best_ask - best_bid)
            if (best_ask is not None and best_bid is not None)
            else 0.0
        )

        # best_bid_ask events come from the merged orderbook and always have
        # valid spreads; only apply the garbage filter to native-book sources.
        if source != "best_bid_ask" and spread > 0.50:
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

    def _handle_market_resolved(self, condition_id: str, data: Dict[str, Any]) -> None:
        """Handle market_resolved event from WebSocket.
        
        Provides instant, definitive settlement detection directly from the
        exchange — no need for price-threshold heuristics or HTTP polling.
        """
        winning_asset_id = data.get("winning_asset_id")
        if not winning_asset_id:
            logger.warning(f"market_resolved without winning_asset_id: {data}")
            return

        # Resolve winning side: look up which token is the winner
        slug = None
        winner_side = None
        for tid, (s, side) in self._token_to_market.items():
            if tid == winning_asset_id:
                slug = s
                winner_side = side
                break

        if not slug or slug not in self._active_markets:
            # Could be a market we're not tracking
            asset_ids = data.get("assets_ids", [])
            for tid in asset_ids:
                if tid in self._token_to_market:
                    s, _ = self._token_to_market[tid]
                    if s in self._active_markets:
                        slug = s
                        winner_side = "up" if winning_asset_id == asset_ids[0] else "down"
                        break

        if slug and winner_side:
            logger.info(
                f"[WS] Settlement via market_resolved: {slug} → "
                f"{winner_side.upper()} wins (definitive)"
            )
            self._ws_resolved_markets.add(slug)
            if self.on_settlement:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(self.on_settlement(slug, winner_side))
                except RuntimeError:
                    pass
        else:
            logger.warning(
                f"market_resolved for unknown market: "
                f"winner={winning_asset_id[:12]}..."
            )

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _market_scan_loop(self) -> None:
        """Periodically scan for new markets via HTTP.

        Scans all configured timeframes (15m, 1h, 4h) for each coin.
        """
        # Timeframes to scan — configurable, defaults to 15m only for
        # backward compatibility.  Set config.timeframes = ["15m", "1h", "4h"]
        # to enable multi-timeframe discovery.
        scan_timeframes = getattr(self.config, "timeframes", None) or ["15m"]

        while self._running:
            try:
                if not self._fetcher:
                    await asyncio.sleep(self.config.market_scan_interval)
                    continue

                # Scan 15m markets via existing method
                if "15m" in scan_timeframes:
                    markets_by_coin = await self._fetcher.find_multi_coin_markets(
                        coins=self.config.coins,
                        count_per_coin=5,
                        include_active=True,
                        include_closed=True,
                    )
                    await self._process_scanned_markets(markets_by_coin)

                # Scan 1h and 4h markets via new timeframe-aware method
                for tf in scan_timeframes:
                    if tf == "15m":
                        continue
                    for coin in (self.config.coins or ["btc", "eth", "sol"]):
                        try:
                            tf_markets = await self._fetcher.find_markets_by_timeframe(
                                coin=coin,
                                timeframe=tf,
                                count=3,
                                include_active=True,
                                include_closed=True,
                            )
                            await self._process_scanned_markets({coin: tf_markets})
                        except Exception as e:
                            logger.debug(f"Scan {coin}/{tf}: {e}")

                await asyncio.sleep(self.config.market_scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market scan: {e}")
                await asyncio.sleep(30)

    async def _process_scanned_markets(
        self, markets_by_coin: Dict[str, list]
    ) -> None:
        """Process scan results: notify closures and discover new markets."""
        for coin, markets in markets_by_coin.items():
            for market in markets:
                slug = market.get("slug")
                if not slug:
                    continue

                is_closed = market.get("closed", False)

                if is_closed:
                    if slug in self._active_markets:
                        winner = market.get("winner")
                        if self.on_market_closed:
                            await self.on_market_closed(slug, winner)
                else:
                    if slug not in self._active_markets:
                        await self._discover_market(coin, market)

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

        # Detect timeframe from slug or market metadata
        timeframe = market.get("timeframe", "15m")
        if not timeframe or timeframe == "15m":
            if "-updown-1h-" in slug:
                timeframe = "1h"
            elif "-updown-4h-" in slug:
                timeframe = "4h"
            elif "-updown-15m-" in slug:
                timeframe = "15m"
            elif "-updown-5m-" in slug:
                timeframe = "5m"

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
            timeframe=timeframe,
        )

        if self.on_market_discovered:
            await self.on_market_discovered(event)

    async def _settlement_check_loop(self) -> None:
        """
        Check for settlements using ONLY authoritative exchange data:
        
        1. Primary: WS `market_resolved` event (handled by _handle_market_resolved,
           instant and definitive — no action needed here).
        2. Backup: HTTP API poll for overdue markets. Queries the Gamma Markets
           API `check_market_state()` which returns the definitive
           `outcome_prices` from the exchange.
        
        NO price-based heuristics. If the exchange hasn't confirmed settlement,
        we don't guess. If a market is significantly overdue and the API still
        can't confirm, we log an error — the market will be handled by the
        market_scan_loop's closure detection.
        """
        http_check_interval = 15.0

        while self._running:
            try:
                now = time.time()
                settled: List[Tuple[str, str]] = []

                for slug in list(self._active_markets):
                    if slug in self._ws_resolved_markets:
                        continue

                    meta = self._market_meta.get(slug, {})
                    settlement_time = meta.get("settlement_time")
                    if not settlement_time:
                        continue

                    # Only query API after settlement_time + grace period
                    grace_period = 30.0
                    if now < settlement_time + grace_period:
                        continue

                    if not self._fetcher:
                        continue

                    try:
                        event_data = await self._fetcher.check_market_state(meta)
                        if event_data and event_data.event_type == MarketEvent.MARKET_SETTLED:
                            winner = event_data.winner
                            if winner:
                                logger.info(
                                    f"[HTTP] Settlement confirmed: {slug} → "
                                    f"{winner.upper()} wins (API authoritative)"
                                )
                                settled.append((slug, winner))
                            else:
                                overdue = now - settlement_time
                                logger.error(
                                    f"[HTTP] Market {slug} closed by exchange but "
                                    f"no winner in outcome_prices (overdue {overdue:.0f}s). "
                                    f"NOT guessing — waiting for authoritative data."
                                )
                    except Exception as e:
                        logger.debug(f"HTTP settlement check failed for {slug}: {e}")

                for slug, winner in settled:
                    if self.on_settlement:
                        await self.on_settlement(slug, winner)

                await asyncio.sleep(http_check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in settlement check loop: {e}")
                await asyncio.sleep(http_check_interval)

    # _check_overdue_settlements removed: settlement detection now uses only
    # authoritative exchange data (WS market_resolved + HTTP API outcome_prices).
    # No price-based guessing.

    async def _orderbook_refresh_loop(self) -> None:
        """Periodically refresh orderbooks via HTTP for active markets.
        
        Uses an 8-second interval (down from 15s) because 15-min crypto
        markets move fast and the REST book becomes stale quickly.
        """
        refresh_interval = 8.0

        while self._running:
            try:
                if not self._fetcher:
                    await asyncio.sleep(refresh_interval)
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

                await asyncio.sleep(refresh_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in orderbook refresh: {e}")
                await asyncio.sleep(refresh_interval)
