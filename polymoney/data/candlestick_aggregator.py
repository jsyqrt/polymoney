"""
Candlestick Aggregator - Aggregates trades into OHLCV candlesticks.

Supports multiple time intervals and separate YES/NO token tracking.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from polymoney.core.logging import get_logger
from polymoney.core.models import Candlestick, TokenType

logger = get_logger("data.candlestick")


# Interval durations in seconds
INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


class CandleBuilder:
    """Builds a single candlestick from trades."""

    def __init__(
        self,
        market_id: str,
        interval: str,
        token_type: TokenType,
        timestamp: datetime,
    ):
        self.market_id = market_id
        self.interval = interval
        self.token_type = token_type
        self.timestamp = timestamp

        self.open: Optional[float] = None
        self.high: float = 0.0
        self.low: float = 1.0
        self.close: float = 0.0
        self.volume: float = 0.0
        self.trade_count: int = 0

    def add_trade(self, price: float, size: float) -> None:
        """Add a trade to the candlestick."""
        if self.open is None:
            self.open = price

        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size
        self.trade_count += 1

    def to_candlestick(self) -> Candlestick:
        """Convert to Candlestick model."""
        return Candlestick(
            market_id=self.market_id,
            interval=self.interval,
            timestamp=self.timestamp,
            token_type=self.token_type,
            open=self.open or 0.5,
            high=self.high if self.trade_count > 0 else 0.5,
            low=self.low if self.trade_count > 0 else 0.5,
            close=self.close if self.trade_count > 0 else 0.5,
            volume=self.volume,
        )

    @property
    def is_empty(self) -> bool:
        """Check if no trades have been recorded."""
        return self.trade_count == 0


# Type alias for candle completion callback
CandleCallback = Callable[[Candlestick], None]


class CandlestickAggregator:
    """
    Aggregates trades into candlesticks for multiple intervals.

    Features:
    - Multi-interval support (1m, 5m, 15m, 1h, 4h, 1d)
    - Separate YES/NO token tracking
    - Automatic interval boundary detection
    - Callback on candlestick completion
    """

    def __init__(
        self,
        market_id: str,
        intervals: Optional[List[str]] = None,
        on_candle_complete: Optional[CandleCallback] = None,
    ):
        """
        Initialize aggregator.

        Args:
            market_id: Market condition ID
            intervals: List of intervals to aggregate (default: 1m, 5m, 15m, 1h)
            on_candle_complete: Callback when a candlestick completes
        """
        self.market_id = market_id
        self.intervals = intervals or ["1m", "5m", "15m", "1h"]
        self.on_candle_complete = on_candle_complete

        # Validate intervals
        for interval in self.intervals:
            if interval not in INTERVAL_SECONDS:
                raise ValueError(f"Invalid interval: {interval}")

        # Current candle builders: {interval: {token_type: CandleBuilder}}
        self._builders: Dict[str, Dict[TokenType, CandleBuilder]] = {}

        # Initialize builders
        self._init_builders()

        # Timer tasks for interval boundaries
        self._timer_tasks: Dict[str, asyncio.Task] = {}

    def _init_builders(self) -> None:
        """Initialize candle builders for all intervals and token types."""
        now = datetime.now()

        for interval in self.intervals:
            self._builders[interval] = {}
            interval_start = self._get_interval_start(now, interval)

            for token_type in [TokenType.YES, TokenType.NO]:
                self._builders[interval][token_type] = CandleBuilder(
                    market_id=self.market_id,
                    interval=interval,
                    token_type=token_type,
                    timestamp=interval_start,
                )

    def _get_interval_start(self, dt: datetime, interval: str) -> datetime:
        """Get the start of the current interval."""
        seconds = INTERVAL_SECONDS[interval]

        if interval == "1d":
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)

        # Align to interval boundary
        total_seconds = dt.hour * 3600 + dt.minute * 60 + dt.second
        interval_start_seconds = (total_seconds // seconds) * seconds

        hours = interval_start_seconds // 3600
        minutes = (interval_start_seconds % 3600) // 60
        secs = interval_start_seconds % 60

        return dt.replace(hour=hours, minute=minutes, second=secs, microsecond=0)

    def _get_next_boundary(self, interval: str) -> datetime:
        """Get the next interval boundary time."""
        now = datetime.now()
        interval_start = self._get_interval_start(now, interval)
        return interval_start + timedelta(seconds=INTERVAL_SECONDS[interval])

    def on_trade(
        self,
        price: float,
        size: float,
        token_type: TokenType = TokenType.YES,
    ) -> None:
        """
        Process a new trade.

        Args:
            price: Trade price (0-1)
            size: Trade size
            token_type: Token type (YES/NO)
        """
        now = datetime.now()

        for interval in self.intervals:
            builder = self._builders[interval].get(token_type)
            if not builder:
                continue

            # Check if we need to rotate to a new candle
            interval_start = self._get_interval_start(now, interval)
            if interval_start > builder.timestamp:
                self._finalize_candle(interval, token_type)

            # Add trade to current builder
            self._builders[interval][token_type].add_trade(price, size)

    def _finalize_candle(self, interval: str, token_type: TokenType) -> None:
        """Finalize and emit a completed candlestick."""
        builder = self._builders[interval].get(token_type)
        if not builder:
            return

        # Only emit if there were trades
        if not builder.is_empty and self.on_candle_complete:
            candle = builder.to_candlestick()
            try:
                self.on_candle_complete(candle)
            except Exception as e:
                logger.error(f"Error in candle callback: {e}")

        # Create new builder
        now = datetime.now()
        interval_start = self._get_interval_start(now, interval)

        self._builders[interval][token_type] = CandleBuilder(
            market_id=self.market_id,
            interval=interval,
            token_type=token_type,
            timestamp=interval_start,
        )

    def finalize_all(self) -> List[Candlestick]:
        """
        Finalize all current candlesticks.

        Returns:
            List of finalized candlesticks
        """
        candles = []

        for interval in self.intervals:
            for token_type in [TokenType.YES, TokenType.NO]:
                builder = self._builders[interval].get(token_type)
                if builder and not builder.is_empty:
                    candles.append(builder.to_candlestick())

        return candles

    def get_current_candles(self) -> Dict[str, Dict[str, Candlestick]]:
        """
        Get current (incomplete) candlesticks.

        Returns:
            Dict of {interval: {token_type: Candlestick}}
        """
        result = {}

        for interval in self.intervals:
            result[interval] = {}
            for token_type in [TokenType.YES, TokenType.NO]:
                builder = self._builders[interval].get(token_type)
                if builder:
                    result[interval][token_type.value] = builder.to_candlestick()

        return result

    async def start_timers(self) -> None:
        """Start interval boundary timers."""
        for interval in self.intervals:
            task = asyncio.create_task(self._run_interval_timer(interval))
            self._timer_tasks[interval] = task

    async def stop_timers(self) -> None:
        """Stop all interval timers."""
        for task in self._timer_tasks.values():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._timer_tasks.clear()

    async def _run_interval_timer(self, interval: str) -> None:
        """Run timer for interval boundary detection."""
        while True:
            try:
                # Calculate time until next boundary
                next_boundary = self._get_next_boundary(interval)
                now = datetime.now()
                sleep_seconds = (next_boundary - now).total_seconds()

                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

                # Finalize candles at boundary
                for token_type in [TokenType.YES, TokenType.NO]:
                    self._finalize_candle(interval, token_type)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in interval timer: {e}")
                await asyncio.sleep(1)
