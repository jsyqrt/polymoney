"""
Simulation module — shared types, configuration, and status tools.

Provides:
- SimulationConfig: Unified configuration for paper and live trading
- SimulationStats: Aggregate statistics for a trading session
- MarketResult: Per-market settlement results
- SimStatusCLI: Command-line status query tool
"""

from polymoney.simulation.live_runner import (
    SimulationConfig,
    SimulationStats,
    MarketResult,
)
from polymoney.simulation.status_cli import SimStatusCLI

__all__ = [
    "SimulationConfig",
    "SimulationStats",
    "MarketResult",
    "SimStatusCLI",
]
