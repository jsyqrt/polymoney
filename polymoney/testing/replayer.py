"""
Market Data Replayer - Replay historical market data for testing.

Reads real market data from DataStorage and replays it as PriceData events,
allowing strategies to be tested against historical data.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    Candlestick,
    MarketEnd,
    MarketSettled,
    MarketStart,
    PriceData,
    TokenType,
)
from polymoney.data.storage import DataStorage

logger = get_logger("testing.replayer")


@dataclass
class ReplayConfig:
    """Configuration for market data replay."""

    market_id: str
    replay_speed: float = 1.0  # 1.0 = real-time, 10.0 = 10x speed, 0 = max speed
    include_trades: bool = False
    settlement_winner: Optional[str] = None  # 'up'/'down' or None to use real result


@dataclass
class ReplayEvent:
    """A single replay event."""

    timestamp: datetime
    event_type: str  # 'price_update', 'market_start', 'market_end', 'market_settled'
    data: Any


@dataclass
class MarketReplayData:
    """Loaded market data ready for replay."""

    market_id: str
    title: str
    candlesticks: List[Candlestick]
    settlement_winner: Optional[str]  # Real settlement result
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    @property
    def duration_seconds(self) -> float:
        """Get market duration in seconds."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0


# Callback type for replay events
ReplayCallback = Callable[[str, str, Any], None]  # (event_type, market_id, data)


class MarketDataReplayer:
    """
    Replays historical market data from database.

    Compatible with WebSocketManager callback interface, allowing it to be
    used as a drop-in replacement for testing.

    Features:
    - Load candlestick data from DataStorage
    - Convert to PriceData events
    - Configurable replay speed
    - Market lifecycle events (start, end, settled)
    """

    def __init__(
        self,
        storage: DataStorage,
        replay_speed: float = 1.0,
    ):
        """
        Initialize replayer.

        Args:
            storage: DataStorage instance for reading historical data
            replay_speed: Playback speed (1.0=real-time, 0=max speed)
        """
        self.storage = storage
        self.replay_speed = replay_speed
        self._callbacks: List[ReplayCallback] = []
        self._running = False
        self._market_data: Optional[MarketReplayData] = None

    def add_callback(self, callback: ReplayCallback) -> None:
        """Add a callback for replay events (WebSocketManager compatible)."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: ReplayCallback) -> None:
        """Remove a callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _emit(self, event_type: str, market_id: str, data: Any) -> None:
        """Emit an event to all callbacks."""
        for callback in self._callbacks:
            try:
                callback(event_type, market_id, data)
            except Exception as e:
                logger.error(f"Error in replay callback: {e}")

    async def load_market(
        self,
        market_id: str,
        title: str = "",
        settlement_winner: Optional[str] = None,
        interval: str = "1m",
    ) -> MarketReplayData:
        """
        Load historical data for a market.

        Args:
            market_id: Market condition ID
            title: Market title
            settlement_winner: Override settlement result (or None for actual)
            interval: Candlestick interval to use

        Returns:
            MarketReplayData with loaded candlesticks
        """
        logger.info(f"Loading market data: {market_id}")

        # Load candlesticks from storage
        candlesticks = await self.storage.get_candlesticks(
            market_id=market_id,
            interval=interval,
            limit=10000,  # Get all available data
        )

        if not candlesticks:
            logger.warning(f"No candlestick data found for market {market_id}")
            return MarketReplayData(
                market_id=market_id,
                title=title,
                candlesticks=[],
                settlement_winner=settlement_winner,
            )

        # Sort by timestamp
        candlesticks.sort(key=lambda c: c.timestamp)

        start_time = candlesticks[0].timestamp if candlesticks else None
        end_time = candlesticks[-1].timestamp if candlesticks else None

        self._market_data = MarketReplayData(
            market_id=market_id,
            title=title,
            candlesticks=candlesticks,
            settlement_winner=settlement_winner,
            start_time=start_time,
            end_time=end_time,
        )

        logger.info(
            f"Loaded {len(candlesticks)} candlesticks for {market_id}, "
            f"duration: {self._market_data.duration_seconds:.0f}s"
        )

        return self._market_data

    def candlestick_to_price_data(self, candle: Candlestick) -> PriceData:
        """
        Convert a candlestick to PriceData.

        Uses close price as the current price.
        Ensures up_price + down_price = 1.0
        """
        # Assume candlestick is for YES/UP token
        up_price = candle.close

        # Ensure valid range [0.01, 0.99]
        up_price = max(0.01, min(0.99, up_price))
        down_price = 1.0 - up_price

        return PriceData(
            market_id=candle.market_id,
            up_price=up_price,
            down_price=down_price,
            timestamp=candle.timestamp,
        )

    async def replay(
        self,
        market_data: Optional[MarketReplayData] = None,
    ) -> None:
        """
        Replay market data, emitting events to callbacks.

        Args:
            market_data: Market data to replay (or use previously loaded)
        """
        data = market_data or self._market_data
        if not data:
            raise ValueError("No market data loaded. Call load_market() first.")

        if not data.candlesticks:
            logger.warning(f"No candlesticks to replay for {data.market_id}")
            return

        self._running = True
        logger.info(
            f"Starting replay for {data.market_id} at {self.replay_speed}x speed"
        )

        # Emit market start
        start_event = MarketStart(
            market_id=data.market_id,
            title=data.title,
            metadata={
                "replay": True,
                "replay_speed": self.replay_speed,
                "candlestick_count": len(data.candlesticks),
            },
        )
        self._emit("market_start", data.market_id, start_event)

        # Replay candlesticks
        prev_time: Optional[datetime] = None

        for i, candle in enumerate(data.candlesticks):
            if not self._running:
                logger.info("Replay stopped")
                break

            # Calculate delay based on replay speed
            if prev_time and self.replay_speed > 0:
                time_diff = (candle.timestamp - prev_time).total_seconds()
                delay = time_diff / self.replay_speed
                if delay > 0:
                    await asyncio.sleep(delay)

            # Convert to PriceData and emit
            price_data = self.candlestick_to_price_data(candle)
            self._emit("price_update", data.market_id, price_data)

            prev_time = candle.timestamp

            # Log progress periodically
            if (i + 1) % 100 == 0:
                logger.debug(f"Replayed {i + 1}/{len(data.candlesticks)} candlesticks")

        # Emit market end
        final_candle = data.candlesticks[-1] if data.candlesticks else None
        end_event = MarketEnd(
            market_id=data.market_id,
            final_up_price=final_candle.close if final_candle else None,
            final_down_price=1.0 - final_candle.close if final_candle else None,
        )
        self._emit("market_end", data.market_id, end_event)

        # Emit settlement
        if data.settlement_winner:
            settled_event = MarketSettled(
                market_id=data.market_id,
                winner=TokenType(data.settlement_winner.lower()),
            )
            self._emit("market_settled", data.market_id, settled_event)

        self._running = False
        logger.info(f"Replay complete for {data.market_id}")

    def stop(self) -> None:
        """Stop the replay."""
        self._running = False

    @property
    def is_running(self) -> bool:
        """Check if replay is running."""
        return self._running

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status (WebSocketManager compatible)."""
        return {
            "connected": True,  # Always "connected" for replay
            "replay_mode": True,
            "replay_speed": self.replay_speed,
            "is_running": self._running,
            "market_loaded": self._market_data is not None,
        }
