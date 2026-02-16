"""
KillSwitch - Emergency shutdown mechanism.

Provides multiple triggers for immediate shutdown:
1. File-based: Touch a file to trigger shutdown
2. Signal-based: SIGUSR1 triggers emergency stop
3. Programmatic: Direct call from RiskManager or external API

When activated:
- Cancels all pending orders
- Writes emergency state
- Stops the TradingRunner
"""

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from polymoney.core.logging import get_logger

logger = get_logger("risk.kill_switch")


class KillSwitch:
    """
    Emergency stop mechanism for live trading.
    
    Usage:
        ks = KillSwitch(kill_file=Path("KILL_SWITCH"))
        ks.on_kill = my_shutdown_handler
        await ks.start_monitoring()
        
        # Manual trigger
        ks.activate(reason="Manual kill")
    """

    def __init__(
        self,
        kill_file: Path = Path("KILL_SWITCH"),
        check_interval: float = 2.0,
    ):
        self.kill_file = kill_file
        self.check_interval = check_interval
        self._activated = False
        self._activation_time: Optional[float] = None
        self._reason: str = ""
        self._monitor_task: Optional[asyncio.Task] = None

        # Callback: async function to call on activation
        self.on_kill: Optional[Callable[[str], Awaitable[None]]] = None

    @property
    def is_activated(self) -> bool:
        return self._activated

    def activate(self, reason: str = "Manual kill switch") -> None:
        """
        Activate the kill switch.
        
        This is non-blocking — the actual shutdown is handled asynchronously
        via the on_kill callback.
        """
        if self._activated:
            return

        self._activated = True
        self._activation_time = time.time()
        self._reason = reason

        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")

        # Trigger callback
        if self.on_kill:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.on_kill(reason))
            except RuntimeError:
                pass

    async def start_monitoring(self) -> None:
        """Start monitoring for kill switch triggers."""
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        # Register SIGUSR1 if available (Unix only)
        try:
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(
                signal.SIGUSR1,
                lambda: self.activate("SIGUSR1 signal received"),
            )
            logger.info("KillSwitch monitoring started (file + SIGUSR1)")
        except (NotImplementedError, AttributeError, ValueError):
            logger.info("KillSwitch monitoring started (file-based only)")

    async def stop_monitoring(self) -> None:
        """Stop the monitoring loop."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._monitor_task = None

    def get_status(self) -> Dict[str, Any]:
        """Get kill switch status."""
        return {
            "activated": self._activated,
            "reason": self._reason,
            "activation_time": self._activation_time,
            "kill_file": str(self.kill_file),
            "kill_file_exists": self.kill_file.exists(),
        }

    async def _monitor_loop(self) -> None:
        """Monitor for kill switch file."""
        while True:
            try:
                if self.kill_file.exists():
                    # Read reason from file if present
                    try:
                        reason = self.kill_file.read_text().strip()
                        if not reason:
                            reason = "Kill switch file detected"
                    except Exception:
                        reason = "Kill switch file detected"

                    self.activate(reason)
                    # Remove the file so it doesn't re-trigger after restart
                    try:
                        self.kill_file.unlink()
                    except Exception:
                        pass
                    return

                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Error in kill switch monitor: {e}")
                await asyncio.sleep(self.check_interval)
