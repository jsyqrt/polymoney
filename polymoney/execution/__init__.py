"""
Execution layer - order execution abstraction for paper and live trading.

Provides:
- OrderExecutor ABC with submit/cancel/check_fills interface
- SimulatedExecutor for paper trading with depth-based fill simulation
- LiveExecutor for real order execution via Polymarket CLOB
- FillManager for real-time fill tracking and position reconciliation
"""

from polymoney.execution.executor import (
    ExecutionOrder,
    FillEvent,
    OrderExecutor,
    OrderResult,
    OrderResultStatus,
)

__all__ = [
    "ExecutionOrder",
    "FillEvent",
    "OrderExecutor",
    "OrderResult",
    "OrderResultStatus",
]
