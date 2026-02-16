"""
AlertManager - Webhook-based alerting for trading events.

Supports Discord and generic webhook endpoints. Sends alerts for:
- Trading errors and exceptions
- Daily loss limit approaching/breached
- Kill switch activation
- Market settlements with significant PnL
- Circuit breaker activations

Alerts are sent asynchronously and failures are logged but never
block the trading loop.
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from polymoney.core.logging import get_logger

logger = get_logger("monitoring.alerts")


class AlertManager:
    """
    Webhook-based alert manager.
    
    Usage:
        am = AlertManager(webhook_url="https://discord.com/api/webhooks/...")
        await am.send_alert(
            title="Daily Loss Warning",
            message="Daily PnL: -$40.00 (limit: $50.00)",
            level="warning"
        )
    """

    LEVEL_EMOJI = {
        "info": "ℹ️",
        "warning": "⚠️",
        "error": "🚨",
        "critical": "🔴",
    }

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        enabled: bool = True,
        rate_limit_seconds: float = 60.0,
    ):
        self.webhook_url = webhook_url
        self.enabled = enabled and webhook_url is not None
        self.rate_limit_seconds = rate_limit_seconds

        # Rate limiting per alert key
        self._last_sent: Dict[str, float] = {}

        # Queue for batching
        self._queue: List[Dict[str, Any]] = []

    async def send_alert(
        self,
        title: str,
        message: str,
        level: str = "info",
        key: Optional[str] = None,
        fields: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Send an alert via webhook.
        
        Args:
            title: Alert title
            message: Alert body
            level: info, warning, error, critical
            key: Rate-limiting key (same key won't re-send within rate_limit_seconds)
            fields: Optional key-value fields to include
            
        Returns:
            True if sent, False if skipped or failed
        """
        if not self.enabled:
            return False

        # Rate limiting
        if key:
            last = self._last_sent.get(key, 0)
            if time.time() - last < self.rate_limit_seconds:
                return False
            self._last_sent[key] = time.time()

        emoji = self.LEVEL_EMOJI.get(level, "")
        
        try:
            if self._is_discord_webhook():
                return await self._send_discord(title, message, level, emoji, fields)
            else:
                return await self._send_generic(title, message, level, fields)
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
            return False

    async def alert_loss_warning(
        self, daily_pnl: float, daily_limit: float
    ) -> bool:
        """Send alert when daily loss is approaching limit."""
        pct = abs(daily_pnl) / daily_limit * 100
        return await self.send_alert(
            title="Daily Loss Warning",
            message=f"Daily PnL: ${daily_pnl:.2f} ({pct:.0f}% of limit)",
            level="warning",
            key="daily_loss_warning",
            fields={
                "Daily PnL": f"${daily_pnl:.2f}",
                "Limit": f"-${daily_limit:.2f}",
            },
        )

    async def alert_kill_switch(self, reason: str) -> bool:
        """Send alert when kill switch is activated."""
        return await self.send_alert(
            title="KILL SWITCH ACTIVATED",
            message=reason,
            level="critical",
            key="kill_switch",
        )

    async def alert_settlement(
        self, slug: str, winner: str, pnl: float, roi: float
    ) -> bool:
        """Send alert for significant settlements."""
        level = "info" if pnl >= 0 else "warning"
        return await self.send_alert(
            title=f"Market Settled: {slug}",
            message=f"Winner: {winner.upper()}, PnL: ${pnl:.2f}, ROI: {roi*100:.1f}%",
            level=level,
            fields={
                "Market": slug,
                "Winner": winner.upper(),
                "PnL": f"${pnl:.2f}",
                "ROI": f"{roi*100:.1f}%",
            },
        )

    async def alert_circuit_breaker(
        self, consecutive_losses: int, pause_seconds: float
    ) -> bool:
        """Send alert when circuit breaker trips."""
        return await self.send_alert(
            title="Circuit Breaker Tripped",
            message=(
                f"{consecutive_losses} consecutive losses. "
                f"Trading paused for {pause_seconds/60:.0f} minutes."
            ),
            level="error",
            key="circuit_breaker",
        )

    async def alert_error(self, component: str, error: str) -> bool:
        """Send alert for trading errors."""
        return await self.send_alert(
            title=f"Error: {component}",
            message=error,
            level="error",
            key=f"error_{component}",
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_discord_webhook(self) -> bool:
        return self.webhook_url and "discord.com" in self.webhook_url

    async def _send_discord(
        self,
        title: str,
        message: str,
        level: str,
        emoji: str,
        fields: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Send alert via Discord webhook."""
        import aiohttp

        color_map = {
            "info": 0x3498DB,
            "warning": 0xF39C12,
            "error": 0xE74C3C,
            "critical": 0xFF0000,
        }

        embed = {
            "title": f"{emoji} {title}",
            "description": message,
            "color": color_map.get(level, 0x95A5A6),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        if fields:
            embed["fields"] = [
                {"name": k, "value": v, "inline": True}
                for k, v in fields.items()
            ]

        payload = {"embeds": [embed]}

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    logger.debug(f"Discord alert sent: {title}")
                    return True
                else:
                    body = await resp.text()
                    logger.warning(
                        f"Discord alert failed ({resp.status}): {body}"
                    )
                    return False

    async def _send_generic(
        self,
        title: str,
        message: str,
        level: str,
        fields: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Send alert via generic webhook (POST JSON)."""
        import aiohttp

        payload = {
            "title": title,
            "message": message,
            "level": level,
            "timestamp": time.time(),
        }
        if fields:
            payload["fields"] = fields

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201, 204):
                    logger.debug(f"Webhook alert sent: {title}")
                    return True
                else:
                    logger.warning(f"Webhook alert failed ({resp.status})")
                    return False
