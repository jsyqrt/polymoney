"""
Risk management layer for live trading safety.

Provides:
- RiskManager: Circuit breakers, loss limits, exposure caps
- KillSwitch: Emergency shutdown via signal, file, or API
"""

from polymoney.risk.manager import RiskManager
from polymoney.risk.kill_switch import KillSwitch

__all__ = ["RiskManager", "KillSwitch"]
