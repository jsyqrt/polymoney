"""
Built-in strategies module.

Import this module to register all built-in strategies.
"""

from .position_arbitrage import PositionArbitrageStrategy
from .position_arbitrage_v2 import PositionArbitrageV2

__all__ = ["PositionArbitrageStrategy", "PositionArbitrageV2"]
