"""
RiskManager - Circuit breakers and safety limits for live trading.

Provides pre-trade and post-trade risk checks:
- Daily loss limit
- Per-market loss limit
- Consecutive loss circuit breaker
- Total exposure cap
- API rate limiting

The RiskManager is consulted before every order submission and after
every market finalization. If any limit is breached, it can:
- Block new orders
- Trigger a pause period
- Request a full shutdown via KillSwitch
"""

import time
from collections import deque
from typing import Any, Callable, Dict, Optional

from polymoney.core.logging import get_logger

logger = get_logger("risk.manager")


class RiskManager:
    """
    Risk management with circuit breakers.
    
    Usage:
        rm = RiskManager(daily_loss_limit=50.0)
        
        # Before placing an order
        if rm.can_trade(market_id="btc-...", order_cost=5.0):
            # proceed
        
        # After a market settles
        rm.record_result(market_id="btc-...", pnl=-2.50)
    """

    def __init__(
        self,
        daily_loss_limit: float = 50.0,
        per_market_loss_limit: float = 10.0,
        consecutive_loss_pause: int = 5,
        pause_duration_seconds: float = 1800.0,
        max_total_exposure: float = 500.0,
        api_rate_limit: int = 10,
    ):
        # Limits
        self.daily_loss_limit = daily_loss_limit
        self.per_market_loss_limit = per_market_loss_limit
        self.consecutive_loss_pause = consecutive_loss_pause
        self.pause_duration = pause_duration_seconds
        self.max_total_exposure = max_total_exposure
        self.api_rate_limit = api_rate_limit

        # State
        self._daily_pnl: float = 0.0
        self._daily_reset_time: float = self._next_midnight()
        self._consecutive_losses: int = 0
        self._paused_until: float = 0.0
        self._total_exposure: float = 0.0
        self._market_pnl: Dict[str, float] = {}

        # API rate limiter
        self._api_timestamps: deque = deque()

        # Kill switch callback (set by TradingRunner)
        self.on_kill_requested: Optional[Callable] = None

        # Tracking
        self._blocked_count: int = 0
        self._pause_count: int = 0

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def can_trade(
        self,
        market_id: str = "",
        order_cost: float = 0.0,
    ) -> bool:
        """
        Check if trading is allowed.
        
        Returns False if any risk limit would be breached.
        """
        self._check_daily_reset()

        # Check pause
        if time.time() < self._paused_until:
            remaining = self._paused_until - time.time()
            logger.debug(
                f"Trading paused: {remaining:.0f}s remaining "
                f"(consecutive losses: {self._consecutive_losses})"
            )
            self._blocked_count += 1
            return False

        # Check daily loss limit
        if self._daily_pnl <= -self.daily_loss_limit:
            logger.warning(
                f"Daily loss limit reached: ${self._daily_pnl:.2f} "
                f"(limit: -${self.daily_loss_limit:.2f})"
            )
            self._blocked_count += 1
            return False

        # Check total exposure
        if self._total_exposure + order_cost > self.max_total_exposure:
            logger.debug(
                f"Exposure limit: ${self._total_exposure:.2f} + "
                f"${order_cost:.2f} > ${self.max_total_exposure:.2f}"
            )
            self._blocked_count += 1
            return False

        return True

    def check_api_rate(self) -> bool:
        """
        Check if an API call is allowed under rate limits.
        
        Uses a sliding window of 1 second.
        """
        now = time.time()
        # Remove timestamps older than 1 second
        while self._api_timestamps and self._api_timestamps[0] < now - 1.0:
            self._api_timestamps.popleft()

        if len(self._api_timestamps) >= self.api_rate_limit:
            return False

        self._api_timestamps.append(now)
        return True

    # ------------------------------------------------------------------
    # Post-trade updates
    # ------------------------------------------------------------------

    def record_result(self, market_id: str, pnl: float) -> None:
        """Record a market result for risk tracking."""
        self._check_daily_reset()

        self._daily_pnl += pnl
        self._market_pnl[market_id] = self._market_pnl.get(market_id, 0) + pnl

        if pnl < 0:
            self._consecutive_losses += 1
            logger.info(
                f"Loss recorded: ${pnl:.2f} for {market_id} "
                f"(daily: ${self._daily_pnl:.2f}, "
                f"consecutive: {self._consecutive_losses})"
            )

            # Check consecutive loss circuit breaker
            if self._consecutive_losses >= self.consecutive_loss_pause:
                self._paused_until = time.time() + self.pause_duration
                self._pause_count += 1
                logger.warning(
                    f"Circuit breaker: {self._consecutive_losses} consecutive losses. "
                    f"Pausing for {self.pause_duration:.0f}s"
                )
        else:
            self._consecutive_losses = 0

        # Check if daily loss limit was just breached
        if self._daily_pnl <= -self.daily_loss_limit:
            logger.warning(
                f"DAILY LOSS LIMIT BREACHED: ${self._daily_pnl:.2f}. "
                f"No new trades will be placed today."
            )

    def update_exposure(self, total_exposure: float) -> None:
        """Update total exposure (called by TradingRunner with fill_manager data)."""
        self._total_exposure = total_exposure

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current risk status."""
        self._check_daily_reset()
        now = time.time()
        return {
            "daily_pnl": self._daily_pnl,
            "daily_loss_limit": self.daily_loss_limit,
            "daily_remaining": self.daily_loss_limit + self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "is_paused": now < self._paused_until,
            "pause_remaining": max(0, self._paused_until - now),
            "total_exposure": self._total_exposure,
            "max_exposure": self.max_total_exposure,
            "blocked_count": self._blocked_count,
            "pause_count": self._pause_count,
        }

    @property
    def is_paused(self) -> bool:
        return time.time() < self._paused_until

    @property
    def daily_limit_reached(self) -> bool:
        self._check_daily_reset()
        return self._daily_pnl <= -self.daily_loss_limit

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_daily_reset(self) -> None:
        """Reset daily counters at midnight."""
        now = time.time()
        if now >= self._daily_reset_time:
            logger.info(
                f"Daily reset: PnL was ${self._daily_pnl:.2f}, "
                f"blocked {self._blocked_count} orders"
            )
            self._daily_pnl = 0.0
            self._blocked_count = 0
            self._daily_reset_time = self._next_midnight()

    @staticmethod
    def _next_midnight() -> float:
        """Get timestamp for next midnight UTC."""
        import datetime as dt
        now = dt.datetime.utcnow()
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow += dt.timedelta(days=1)
        return tomorrow.timestamp()
