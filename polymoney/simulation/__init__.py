"""
Simulation module for live paper trading.

This module provides:
- LiveRunner: Real-time multi-market simulation runner
- StatusCLI: Command-line status query tool
- Metrics output and state persistence
"""

from polymoney.simulation.live_runner import (
    LiveRunner,
    SimulationConfig,
    SimulationStats,
    MarketResult,
)
from polymoney.simulation.status_cli import SimStatusCLI

__all__ = [
    "LiveRunner",
    "SimulationConfig",
    "SimulationStats",
    "MarketResult",
    "SimStatusCLI",
]
