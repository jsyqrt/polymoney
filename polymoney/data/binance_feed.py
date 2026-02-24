"""
Binance real-time price feed via WebSocket.

Provides sub-second BTC/ETH/SOL prices from Binance trade stream,
independent of Polymarket's Chainlink oracle. Used for:
- Directional signal: compare Binance price delta vs epoch start
- Latency advantage: Binance updates 100ms-3s ahead of Polymarket

The feed tracks per-epoch start prices so the strategy can compute
(current_binance - epoch_start_binance) / epoch_start_binance
as a directional confidence signal.
"""

import asyncio
import json
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

import websockets

from polymoney.core.logging import get_logger

logger = get_logger("data.binance_feed")

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

COIN_TO_SYMBOL = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
}


class BinancePriceFeed:
    """Real-time price feed from Binance trade stream.

    Subscribes to ``<symbol>@trade`` for each configured coin and
    maintains latest price + per-epoch start prices for delta
    calculation.
    """

    def __init__(
        self,
        coins: Optional[List[str]] = None,
        on_price_update: Optional[Callable] = None,
    ):
        self._coins = [c.lower() for c in (coins or ["btc", "eth", "sol"])]
        self._symbols = {
            coin: COIN_TO_SYMBOL.get(coin, f"{coin}usdt")
            for coin in self._coins
        }

        self._latest_prices: Dict[str, float] = {}
        self._latest_timestamps: Dict[str, float] = {}

        # epoch_start_prices[coin][epoch_ts] = binance_price_at_start
        self._epoch_start_prices: Dict[str, Dict[int, float]] = defaultdict(dict)

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_delay = 1.0

        self.on_price_update = on_price_update

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket connection in the background."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Binance feed starting for {self._coins}"
        )

    async def stop(self) -> None:
        """Stop the feed and close the WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Binance feed stopped")

    def get_price(self, coin: str) -> Optional[float]:
        """Return the latest Binance price for a coin, or None."""
        return self._latest_prices.get(coin.lower())

    def record_epoch_start(self, coin: str, epoch_timestamp: int) -> None:
        """Record the current Binance price as the start of an epoch.

        Call this when a new market is discovered to anchor the delta
        calculation.  If Binance price is not yet available, the first
        price update for that coin will be used instead.
        """
        coin = coin.lower()
        price = self._latest_prices.get(coin)
        if price is not None:
            self._epoch_start_prices[coin][epoch_timestamp] = price
            logger.debug(
                f"Epoch start recorded: {coin} epoch={epoch_timestamp} "
                f"price=${price:.2f}"
            )
        else:
            # Mark as pending — will be filled on first price update
            self._epoch_start_prices[coin][epoch_timestamp] = 0.0

    def get_epoch_delta(self, coin: str, epoch_timestamp: int) -> Optional[float]:
        """Return (current - start) / start for a given epoch.

        Returns None if start price is not available or zero.
        """
        coin = coin.lower()
        current = self._latest_prices.get(coin)
        if current is None:
            return None

        start = self._epoch_start_prices.get(coin, {}).get(epoch_timestamp)
        if not start or start <= 0:
            return None

        return (current - start) / start

    def cleanup_old_epochs(self, max_age_seconds: float = 86400) -> None:
        """Remove epoch entries older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        for coin in list(self._epoch_start_prices.keys()):
            epochs = self._epoch_start_prices[coin]
            to_remove = [ts for ts in epochs if ts < cutoff]
            for ts in to_remove:
                del epochs[ts]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main reconnection loop."""
        while self._running:
            try:
                streams = "/".join(
                    f"{sym}@trade" for sym in self._symbols.values()
                )
                url = f"{BINANCE_WS_URL}/{streams}"

                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info(f"Binance WS connected: {url}")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            self._handle_trade(msg)
                        except Exception as e:
                            logger.debug(f"Binance msg parse error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(
                        f"Binance WS disconnected: {e}. "
                        f"Reconnecting in {self._reconnect_delay:.0f}s"
                    )
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2, 60.0
                    )

    def _handle_trade(self, msg: dict) -> None:
        """Process a single Binance trade message."""
        symbol = msg.get("s", "").lower()
        price_str = msg.get("p")
        if not symbol or not price_str:
            return

        try:
            price = float(price_str)
        except (TypeError, ValueError):
            return

        # Reverse-lookup coin from symbol
        coin = None
        for c, s in self._symbols.items():
            if s == symbol:
                coin = c
                break
        if coin is None:
            return

        self._latest_prices[coin] = price
        self._latest_timestamps[coin] = time.time()

        # Fill any pending epoch starts (recorded before price was available)
        for epoch_ts, start_price in self._epoch_start_prices.get(coin, {}).items():
            if start_price == 0.0:
                self._epoch_start_prices[coin][epoch_ts] = price

        if self.on_price_update:
            try:
                self.on_price_update(coin, price, time.time())
            except Exception:
                pass
