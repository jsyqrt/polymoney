"""
Real Data Fetcher - Fetch real market data from Polymarket APIs.

This module provides functionality to:
- Discover BTC 15-minute Up/Down markets via slug patterns
- Fetch market details from Gamma API and CLOB API
- Retrieve historical price data for backtesting
- Cache data for efficiency

Example usage:
    from polymoney.data.real_data_fetcher import RealDataFetcher
    
    async with RealDataFetcher() as fetcher:
        # Find recent BTC 15-min markets
        markets = await fetcher.find_btc_15min_markets(count=10)
        
        # Get price history for a specific market
        history = await fetcher.get_price_history(
            token_id=markets[0]["up_token_id"],
            interval="1h",
            fidelity=1
        )
        
        # Convert to candlesticks
        candles = fetcher.prices_to_candlesticks(
            history, 
            market_id="test",
            interval_minutes=1
        )
"""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from polymoney.core.logging import get_logger
from polymoney.core.models import Candlestick, TokenType

logger = get_logger("data.real_fetcher")


class MarketEvent(Enum):
    """Market lifecycle events."""
    MARKET_ACTIVE = "market_active"      # Market becomes tradeable
    MARKET_SETTLING = "market_settling"  # Market within 2 minutes of settlement
    MARKET_SETTLED = "market_settled"    # Market has settled


@dataclass
class PriceUpdate:
    """Real-time price update from orderbook."""
    token_id: str
    mid_price: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread: float
    timestamp: float
    is_stale: bool = False


@dataclass
class MarketEventData:
    """Data associated with a market event."""
    event_type: MarketEvent
    market_slug: str
    coin: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    timestamp: float
    settlement_time: Optional[float] = None
    winner: Optional[str] = None  # For MARKET_SETTLED


# Type alias for event callbacks
MarketEventCallback = Callable[[MarketEventData], None]
PriceUpdateCallback = Callable[[PriceUpdate], None]


class RateLimiter:
    """Simple rate limiter with exponential backoff."""
    
    def __init__(
        self,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        max_retries: int = 5,
    ):
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self._retry_count: Dict[str, int] = {}
        self._last_request: Dict[str, float] = {}
        self._min_interval = 0.1  # 100ms between requests
    
    async def wait_if_needed(self, key: str = "default") -> None:
        """Wait if necessary before making a request."""
        now = time.time()
        last = self._last_request.get(key, 0)
        elapsed = now - last
        
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        
        self._last_request[key] = time.time()
    
    def get_backoff_delay(self, key: str = "default") -> float:
        """Get exponential backoff delay for a key."""
        retries = self._retry_count.get(key, 0)
        delay = self.initial_delay * (2 ** retries)
        return min(delay, self.max_delay)
    
    def record_failure(self, key: str = "default") -> bool:
        """Record a failure and return True if should retry."""
        self._retry_count[key] = self._retry_count.get(key, 0) + 1
        return self._retry_count[key] <= self.max_retries
    
    def reset(self, key: str = "default") -> None:
        """Reset retry count after success."""
        self._retry_count[key] = 0


class DataCache:
    """File-based cache for market data."""
    
    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or Path.home() / ".polymoney" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: Dict[str, Tuple[Any, float]] = {}
        self._memory_ttl = 300  # 5 minutes for metadata
    
    def _get_cache_path(self, key: str) -> Path:
        """Get file path for a cache key."""
        safe_key = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{safe_key}.json"
    
    def get(self, key: str, ttl: Optional[float] = None) -> Optional[Any]:
        """Get cached data if not expired."""
        # Check memory cache first
        if key in self._memory_cache:
            data, cached_at = self._memory_cache[key]
            if ttl is None or (time.time() - cached_at) < ttl:
                return data
        
        # Check file cache
        path = self._get_cache_path(key)
        if path.exists():
            try:
                with open(path) as f:
                    cached = json.load(f)
                cached_at = cached.get("cached_at", 0)
                
                # For completed market data, never expire
                if cached.get("immutable", False):
                    return cached["data"]
                
                # Check TTL
                if ttl is None or (time.time() - cached_at) < ttl:
                    return cached["data"]
            except (json.JSONDecodeError, KeyError):
                pass
        
        return None
    
    def set(self, key: str, data: Any, immutable: bool = False) -> None:
        """Store data in cache."""
        # Memory cache
        self._memory_cache[key] = (data, time.time())
        
        # File cache
        path = self._get_cache_path(key)
        try:
            with open(path, "w") as f:
                json.dump({
                    "data": data,
                    "cached_at": time.time(),
                    "immutable": immutable,
                }, f)
        except Exception as e:
            logger.warning(f"Failed to write cache: {e}")


class RealDataFetcher:
    """
    Fetch real market data from Polymarket APIs.
    
    Supports:
    - Market discovery via slug patterns (Gamma API) for BTC, ETH, SOL
    - Market details from CLOB API
    - Historical price data
    - Real-time price streaming via orderbook polling
    - Rate limiting and caching
    """
    
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    # Supported coins and their slug patterns
    SUPPORTED_COINS = {
        "btc": "btc-updown-15m",
        "eth": "eth-updown-15m",
        "sol": "sol-updown-15m",
    }
    
    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        timeout: float = 30.0,
    ):
        self.cache = DataCache(cache_dir)
        self.rate_limiter = RateLimiter()
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    @property
    def client(self) -> httpx.AsyncClient:
        """Get HTTP client, creating one if needed."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client
    
    async def _request_with_retry(
        self,
        method: str,
        url: str,
        key: str,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """Make HTTP request with rate limiting and retry."""
        while True:
            await self.rate_limiter.wait_if_needed(key)
            
            try:
                response = await self.client.request(method, url, **kwargs)
                
                if response.status_code == 429:  # Rate limited
                    if self.rate_limiter.record_failure(key):
                        delay = self.rate_limiter.get_backoff_delay(key)
                        logger.warning(f"Rate limited, waiting {delay}s")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error("Max retries exceeded for rate limit")
                        return None
                
                self.rate_limiter.reset(key)
                return response
                
            except httpx.RequestError as e:
                if self.rate_limiter.record_failure(key):
                    delay = self.rate_limiter.get_backoff_delay(key)
                    logger.warning(f"Request failed: {e}, retrying in {delay}s")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(f"Max retries exceeded: {e}")
                    return None
    
    async def get_event_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """
        Get event details by slug from Gamma API.
        
        Args:
            slug: Event slug (e.g., "btc-updown-15m-1770106500")
            
        Returns:
            Event data dict or None if not found
        """
        # Check cache
        cache_key = f"event:{slug}"
        cached = self.cache.get(cache_key, ttl=300)
        if cached:
            return cached
        
        url = f"{self.GAMMA_API_URL}/events/slug/{slug}"
        response = await self._request_with_retry("GET", url, "gamma")
        
        if response and response.status_code == 200:
            data = response.json()
            
            # Cache with TTL for active events, immutable for closed
            is_closed = data.get("closed", False)
            self.cache.set(cache_key, data, immutable=is_closed)
            
            return data
        
        return None
    
    async def get_market_by_condition_id(
        self,
        condition_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get market details from CLOB API by condition ID.
        
        Args:
            condition_id: Market condition ID (hex string)
            
        Returns:
            Market data dict or None if not found
        """
        cache_key = f"market:{condition_id}"
        cached = self.cache.get(cache_key, ttl=300)
        if cached:
            return cached
        
        url = f"{self.CLOB_API_URL}/markets/{condition_id}"
        response = await self._request_with_retry("GET", url, "clob")
        
        if response and response.status_code == 200:
            data = response.json()
            is_closed = data.get("closed", False)
            self.cache.set(cache_key, data, immutable=is_closed)
            return data
        
        return None
    
    async def get_price_history(
        self,
        token_id: str,
        interval: str = "1d",
        fidelity: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Get historical price data for a token.
        
        Args:
            token_id: Token ID (numeric string)
            interval: Time interval ("1h", "6h", "1d", "1w", "max")
                      Note: "1h" only works for very recent data
                      Use "6h", "1d" or "max" for historical data
            fidelity: Data point interval in minutes (1 = 1-minute granularity)
            
        Returns:
            List of price points [{"t": timestamp, "p": price}, ...]
        """
        cache_key = f"prices:{token_id}:{interval}:{fidelity}"
        cached = self.cache.get(cache_key, ttl=60)  # 1 minute cache
        if cached:
            return cached
        
        url = f"{self.CLOB_API_URL}/prices-history"
        params = {
            "market": token_id,
            "interval": interval,
            "fidelity": fidelity,
        }
        
        response = await self._request_with_retry("GET", url, "clob", params=params)
        
        if response and response.status_code == 200:
            data = response.json()
            history = data.get("history", [])
            
            # Don't cache empty results for long
            if history:
                self.cache.set(cache_key, history)
            
            return history
        
        # Fallback: try other intervals if this one fails
        if interval == "1h" and not history:
            for fallback_interval in ["6h", "1d", "max"]:
                params["interval"] = fallback_interval
                response = await self._request_with_retry("GET", url, "clob", params=params)
                if response and response.status_code == 200:
                    data = response.json()
                    history = data.get("history", [])
                    if history:
                        self.cache.set(cache_key, history)
                        return history
        
        return []
    
    async def get_last_trade_price(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        Get last trade price for a token.
        
        Args:
            token_id: Token ID
            
        Returns:
            Dict with "price" and "side" or None
        """
        url = f"{self.CLOB_API_URL}/last-trade-price"
        response = await self._request_with_retry(
            "GET", url, "clob", params={"token_id": token_id}
        )
        
        if response and response.status_code == 200:
            return response.json()
        
        return None
    
    async def find_15min_markets(
        self,
        coin: str = "btc",
        count: int = 10,
        include_active: bool = True,
        include_closed: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Find 15-minute Up/Down markets for a specific coin.
        
        Args:
            coin: Coin type ("btc", "eth", "sol")
            count: Number of markets to find
            include_active: Include active/open markets
            include_closed: Include closed/settled markets
            
        Returns:
            List of market info dicts with keys:
            - slug, title, closed, end_date, coin
            - condition_id, up_token_id, down_token_id
            - winner (for closed markets)
        """
        coin = coin.lower()
        if coin not in self.SUPPORTED_COINS:
            raise ValueError(f"Unsupported coin: {coin}. Supported: {list(self.SUPPORTED_COINS.keys())}")
        
        slug_prefix = self.SUPPORTED_COINS[coin]
        markets = []
        now = int(time.time())
        
        # Generate timestamps for recent 15-min intervals
        # Go back up to 100 intervals (~25 hours) to find enough markets
        for i in range(100):
            if len(markets) >= count:
                break
            
            # Calculate timestamp rounded to 15-min boundary
            ts = now - (i * 15 * 60)
            ts = (ts // 900) * 900
            
            slug = f"{slug_prefix}-{ts}"
            event = await self.get_event_by_slug(slug)
            
            if not event:
                continue
            
            is_closed = event.get("closed", False)
            
            # Filter based on active/closed preference
            if is_closed and not include_closed:
                continue
            if not is_closed and not include_active:
                continue
            
            # Extract market details
            event_markets = event.get("markets", [])
            if not event_markets:
                continue
            
            market = event_markets[0]
            
            # Parse JSON strings if needed (Gamma API returns some fields as JSON strings)
            token_ids = market.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            
            outcomes = market.get("outcomes", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            outcome_prices = market.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)
            
            # Determine UP and DOWN token IDs
            up_token_id = None
            down_token_id = None
            winner = None
            
            for idx, outcome in enumerate(outcomes):
                if outcome.lower() == "up":
                    up_token_id = token_ids[idx] if idx < len(token_ids) else None
                    if outcome_prices and idx < len(outcome_prices):
                        if outcome_prices[idx] == "1":
                            winner = "up"
                elif outcome.lower() == "down":
                    down_token_id = token_ids[idx] if idx < len(token_ids) else None
                    if outcome_prices and idx < len(outcome_prices):
                        if outcome_prices[idx] == "1":
                            winner = "down"
            
            markets.append({
                "slug": slug,
                "title": event.get("title"),
                "closed": is_closed,
                "end_date": event.get("endDate"),
                "start_time": event.get("startTime"),
                "condition_id": market.get("conditionId"),
                "up_token_id": up_token_id,
                "down_token_id": down_token_id,
                "winner": winner,
                "volume": market.get("volume"),
                "coin": coin,  # Tag with coin type
            })
        
        return markets

    async def find_btc_15min_markets(
        self,
        count: int = 10,
        include_active: bool = True,
        include_closed: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Find BTC 15-minute Up/Down markets (backwards compatible).
        
        This is a convenience wrapper for find_15min_markets(coin="btc").
        """
        return await self.find_15min_markets(
            coin="btc",
            count=count,
            include_active=include_active,
            include_closed=include_closed,
        )
    
    async def find_multi_coin_markets(
        self,
        coins: List[str] = None,
        count_per_coin: int = 10,
        include_active: bool = True,
        include_closed: bool = True,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Find 15-minute markets for multiple coins in parallel.
        
        Args:
            coins: List of coins to search (default: all supported)
            count_per_coin: Number of markets per coin
            include_active: Include active/open markets
            include_closed: Include closed/settled markets
            
        Returns:
            Dict mapping coin -> list of markets
        """
        if coins is None:
            coins = list(self.SUPPORTED_COINS.keys())
        
        # Validate coins
        for coin in coins:
            if coin.lower() not in self.SUPPORTED_COINS:
                raise ValueError(f"Unsupported coin: {coin}")
        
        # Query all coins in parallel
        tasks = [
            self.find_15min_markets(
                coin=coin,
                count=count_per_coin,
                include_active=include_active,
                include_closed=include_closed,
            )
            for coin in coins
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        markets_by_coin = {}
        for coin, result in zip(coins, results):
            if isinstance(result, Exception):
                logger.error(f"Failed to fetch {coin} markets: {result}")
                markets_by_coin[coin] = []
            else:
                markets_by_coin[coin] = result
        
        return markets_by_coin
    
    async def find_all_active_markets(
        self,
        coins: List[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find all currently active 15-minute markets across multiple coins.
        
        Args:
            coins: List of coins to search (default: all supported)
            
        Returns:
            Combined list of active markets sorted by end time (soonest first)
        """
        markets_by_coin = await self.find_multi_coin_markets(
            coins=coins,
            count_per_coin=5,  # Usually only 1-2 active at a time
            include_active=True,
            include_closed=False,
        )
        
        # Combine and sort by end time
        all_markets = []
        for markets in markets_by_coin.values():
            all_markets.extend(markets)
        
        # Sort by end_date (soonest first)
        all_markets.sort(key=lambda m: m.get("end_date") or "")
        
        return all_markets

    # =========================================================================
    # Price Streaming
    # =========================================================================
    
    async def get_orderbook(
        self,
        token_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get current orderbook for a token.
        
        Args:
            token_id: Token ID
            
        Returns:
            Orderbook dict with bids and asks, or None on error
        """
        url = f"{self.CLOB_API_URL}/book"
        response = await self._request_with_retry(
            "GET", url, "clob", params={"token_id": token_id}
        )
        
        if response and response.status_code == 200:
            return response.json()
        return None
    
    async def get_mid_price(
        self,
        token_id: str,
    ) -> Optional[PriceUpdate]:
        """
        Get current mid price from orderbook.
        
        Args:
            token_id: Token ID
            
        Returns:
            PriceUpdate with mid price and spread, or None on error
        """
        orderbook = await self.get_orderbook(token_id)
        if not orderbook:
            # Fallback to last trade price
            last_trade = await self.get_last_trade_price(token_id)
            if last_trade:
                return PriceUpdate(
                    token_id=token_id,
                    mid_price=float(last_trade.get("price", 0.5)),
                    best_bid=None,
                    best_ask=None,
                    spread=0,
                    timestamp=time.time(),
                    is_stale=True,
                )
            return None
        
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        
        if best_bid is not None and best_ask is not None:
            mid_price = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
        elif best_bid is not None:
            mid_price = best_bid
            spread = 0
        elif best_ask is not None:
            mid_price = best_ask
            spread = 0
        else:
            # Empty orderbook, fallback to last trade
            last_trade = await self.get_last_trade_price(token_id)
            if last_trade:
                mid_price = float(last_trade.get("price", 0.5))
            else:
                mid_price = 0.5  # Default to 50%
            spread = 0
        
        return PriceUpdate(
            token_id=token_id,
            mid_price=mid_price,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            timestamp=time.time(),
        )
    
    async def stream_prices(
        self,
        token_id: str,
        callback: PriceUpdateCallback,
        interval_seconds: float = 5.0,
        stale_threshold_seconds: float = 30.0,
    ) -> asyncio.Task:
        """
        Start streaming price updates for a token via orderbook polling.
        
        Args:
            token_id: Token ID to stream
            callback: Callback function for price updates
            interval_seconds: Polling interval (default 5s)
            stale_threshold_seconds: Time after which price is considered stale
            
        Returns:
            Asyncio task that can be cancelled to stop streaming
        """
        async def _stream_loop():
            last_update_time = time.time()
            
            while True:
                try:
                    price_update = await self.get_mid_price(token_id)
                    
                    if price_update:
                        # Check staleness
                        time_since_update = time.time() - last_update_time
                        if time_since_update > stale_threshold_seconds:
                            price_update.is_stale = True
                            logger.warning(f"Price data stale for {token_id} ({time_since_update:.1f}s)")
                        else:
                            last_update_time = time.time()
                        
                        callback(price_update)
                    else:
                        logger.warning(f"Failed to get price for {token_id}")
                    
                    await asyncio.sleep(interval_seconds)
                    
                except asyncio.CancelledError:
                    logger.info(f"Price streaming stopped for {token_id}")
                    raise
                except Exception as e:
                    logger.error(f"Error in price stream for {token_id}: {e}")
                    await asyncio.sleep(interval_seconds)
        
        task = asyncio.create_task(_stream_loop())
        return task
    
    # =========================================================================
    # Market Lifecycle Events
    # =========================================================================
    
    async def check_market_state(
        self,
        market: Dict[str, Any],
    ) -> Optional[MarketEventData]:
        """
        Check current state of a market and return event if state changed.
        
        Args:
            market: Market info dict from find_15min_markets
            
        Returns:
            MarketEventData if a lifecycle event should be emitted, None otherwise
        """
        slug = market.get("slug")
        coin = market.get("coin", "btc")
        
        # Refresh market data
        event = await self.get_event_by_slug(slug)
        if not event:
            return None
        
        is_closed = event.get("closed", False)
        end_date_str = event.get("endDate")
        
        # Parse end date
        settlement_time = None
        if end_date_str:
            try:
                # Handle ISO format
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                settlement_time = end_dt.timestamp()
            except (ValueError, TypeError):
                pass
        
        now = time.time()
        
        # Check for settled
        if is_closed:
            # Determine winner
            markets_data = event.get("markets", [])
            winner = None
            if markets_data:
                market_data = markets_data[0]
                outcomes = market_data.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                outcome_prices = market_data.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                
                for idx, outcome in enumerate(outcomes):
                    if idx < len(outcome_prices) and outcome_prices[idx] == "1":
                        winner = outcome.lower()
            
            return MarketEventData(
                event_type=MarketEvent.MARKET_SETTLED,
                market_slug=slug,
                coin=coin,
                condition_id=market.get("condition_id", ""),
                up_token_id=market.get("up_token_id", ""),
                down_token_id=market.get("down_token_id", ""),
                timestamp=now,
                settlement_time=settlement_time,
                winner=winner,
            )
        
        # Check if settling (within 2 minutes of end)
        if settlement_time and (settlement_time - now) <= 120:
            return MarketEventData(
                event_type=MarketEvent.MARKET_SETTLING,
                market_slug=slug,
                coin=coin,
                condition_id=market.get("condition_id", ""),
                up_token_id=market.get("up_token_id", ""),
                down_token_id=market.get("down_token_id", ""),
                timestamp=now,
                settlement_time=settlement_time,
            )
        
        # Market is active
        return MarketEventData(
            event_type=MarketEvent.MARKET_ACTIVE,
            market_slug=slug,
            coin=coin,
            condition_id=market.get("condition_id", ""),
            up_token_id=market.get("up_token_id", ""),
            down_token_id=market.get("down_token_id", ""),
            timestamp=now,
            settlement_time=settlement_time,
        )
    
    async def monitor_markets(
        self,
        coins: List[str] = None,
        event_callback: MarketEventCallback = None,
        scan_interval_seconds: float = 60.0,
    ) -> asyncio.Task:
        """
        Start monitoring markets and emit lifecycle events.
        
        Args:
            coins: List of coins to monitor (default: all)
            event_callback: Callback for market events
            scan_interval_seconds: Interval between market scans
            
        Returns:
            Asyncio task that can be cancelled to stop monitoring
        """
        if coins is None:
            coins = list(self.SUPPORTED_COINS.keys())
        
        # Track known markets and their last known state
        known_markets: Dict[str, MarketEvent] = {}
        
        async def _monitor_loop():
            while True:
                try:
                    # Find active markets
                    markets_by_coin = await self.find_multi_coin_markets(
                        coins=coins,
                        count_per_coin=5,
                        include_active=True,
                        include_closed=True,  # To detect settlements
                    )
                    
                    for coin, markets in markets_by_coin.items():
                        for market in markets:
                            slug = market.get("slug")
                            if not slug:
                                continue
                            
                            # Check market state
                            event_data = await self.check_market_state(market)
                            if not event_data:
                                continue
                            
                            # Check if state changed
                            last_state = known_markets.get(slug)
                            current_state = event_data.event_type
                            
                            if last_state != current_state:
                                # State changed, emit event
                                known_markets[slug] = current_state
                                
                                if event_callback:
                                    try:
                                        event_callback(event_data)
                                    except Exception as e:
                                        logger.error(f"Event callback error: {e}")
                                
                                logger.info(f"Market {slug}: {current_state.value}")
                                
                                # Clean up settled markets from tracking
                                if current_state == MarketEvent.MARKET_SETTLED:
                                    # Keep for a bit to avoid re-detection
                                    pass
                    
                    await asyncio.sleep(scan_interval_seconds)
                    
                except asyncio.CancelledError:
                    logger.info("Market monitoring stopped")
                    raise
                except Exception as e:
                    logger.error(f"Error in market monitoring: {e}")
                    await asyncio.sleep(scan_interval_seconds)
        
        task = asyncio.create_task(_monitor_loop())
        return task
    
    def prices_to_candlesticks(
        self,
        price_history: List[Dict[str, Any]],
        market_id: str,
        interval_minutes: int = 1,
        token_type: TokenType = TokenType.YES,
    ) -> List[Candlestick]:
        """
        Convert price history to candlesticks.
        
        Args:
            price_history: List of {"t": timestamp, "p": price} dicts
            market_id: Market identifier
            interval_minutes: Candlestick interval in minutes
            token_type: Token type (YES/UP or NO/DOWN)
            
        Returns:
            List of Candlestick objects
        """
        if not price_history:
            return []
        
        # Sort by timestamp
        sorted_history = sorted(price_history, key=lambda x: x["t"])
        
        # Group into intervals
        interval_seconds = interval_minutes * 60
        candlesticks = []
        current_candle: Optional[Dict[str, Any]] = None
        
        for point in sorted_history:
            ts = point["t"]
            price = point["p"]
            
            # Calculate interval start
            interval_start = (ts // interval_seconds) * interval_seconds
            
            if current_candle is None or current_candle["start"] != interval_start:
                # Save previous candle
                if current_candle:
                    candlesticks.append(self._make_candlestick(
                        current_candle, market_id, interval_minutes, token_type
                    ))
                
                # Start new candle
                current_candle = {
                    "start": interval_start,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "prices": [price],
                }
            else:
                # Update current candle
                current_candle["high"] = max(current_candle["high"], price)
                current_candle["low"] = min(current_candle["low"], price)
                current_candle["close"] = price
                current_candle["prices"].append(price)
        
        # Don't forget the last candle
        if current_candle:
            candlesticks.append(self._make_candlestick(
                current_candle, market_id, interval_minutes, token_type
            ))
        
        return candlesticks
    
    def _make_candlestick(
        self,
        candle_data: Dict[str, Any],
        market_id: str,
        interval_minutes: int,
        token_type: TokenType,
    ) -> Candlestick:
        """Create a Candlestick object from aggregated data."""
        return Candlestick(
            market_id=market_id,
            interval=f"{interval_minutes}m",
            timestamp=datetime.fromtimestamp(candle_data["start"]),
            token_type=token_type,
            open=candle_data["open"],
            high=candle_data["high"],
            low=candle_data["low"],
            close=candle_data["close"],
            volume=float(len(candle_data["prices"])),  # Approximate volume
        )
    
    async def get_market_candlesticks(
        self,
        slug: str,
        interval_minutes: int = 1,
    ) -> Tuple[List[Candlestick], Dict[str, Any]]:
        """
        Get candlesticks for a market by slug.
        
        Args:
            slug: Market slug (e.g., "btc-updown-15m-1770106500")
            interval_minutes: Candlestick interval
            
        Returns:
            Tuple of (candlesticks, market_info)
        """
        # Get event details
        event = await self.get_event_by_slug(slug)
        if not event:
            return [], {}
        
        markets = event.get("markets", [])
        if not markets:
            return [], {}
        
        market = markets[0]
        
        # Parse JSON strings if needed
        token_ids = market.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        
        outcomes = market.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        
        outcome_prices = market.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)
        
        # Get UP token ID
        up_token_id = None
        for idx, outcome in enumerate(outcomes):
            if outcome.lower() == "up" and idx < len(token_ids):
                up_token_id = token_ids[idx]
                break
        
        if not up_token_id:
            up_token_id = token_ids[0] if token_ids else None
        
        if not up_token_id:
            return [], {}
        
        # Get price history (use 1d for good coverage of recent markets)
        history = await self.get_price_history(
            up_token_id,
            interval="1d",
            fidelity=1,
        )
        
        # Convert to candlesticks
        candlesticks = self.prices_to_candlesticks(
            history,
            market_id=market.get("conditionId", slug),
            interval_minutes=interval_minutes,
            token_type=TokenType.YES,
        )
        
        # Prepare market info - winner detection using already-parsed outcome_prices
        winner = None
        for idx, outcome in enumerate(outcomes):
            if idx < len(outcome_prices) and outcome_prices[idx] == "1":
                winner = outcome.lower()
        
        market_info = {
            "slug": slug,
            "title": event.get("title"),
            "condition_id": market.get("conditionId"),
            "closed": event.get("closed"),
            "winner": winner,
            "up_token_id": up_token_id,
            "volume": market.get("volume"),
        }
        
        return candlesticks, market_info


# Convenience function for quick testing
async def test_fetcher():
    """Test the RealDataFetcher."""
    async with RealDataFetcher() as fetcher:
        print("Finding BTC 15-min markets...")
        markets = await fetcher.find_btc_15min_markets(count=5, include_active=False)
        
        for m in markets:
            print(f"\n{m['title']}")
            print(f"  Slug: {m['slug']}")
            print(f"  Winner: {m['winner']}")
            vol = float(m['volume']) if m['volume'] else 0
            print(f"  Volume: ${vol:,.2f}" if vol else "  Volume: N/A")
        
        if markets:
            print(f"\n\nFetching candlesticks for {markets[0]['slug']}...")
            candles, info = await fetcher.get_market_candlesticks(markets[0]["slug"])
            print(f"Got {len(candles)} candlesticks")
            
            if candles:
                print(f"\nFirst candle: {candles[0].timestamp} - O:{candles[0].open:.3f} H:{candles[0].high:.3f} L:{candles[0].low:.3f} C:{candles[0].close:.3f}")
                print(f"Last candle:  {candles[-1].timestamp} - O:{candles[-1].open:.3f} H:{candles[-1].high:.3f} L:{candles[-1].low:.3f} C:{candles[-1].close:.3f}")


if __name__ == "__main__":
    asyncio.run(test_fetcher())
