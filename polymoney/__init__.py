"""
Polymoney - Polymarket Trading Bot Framework

A modular, extensible framework for automated trading on Polymarket,
the leading decentralized prediction market.

Features:
    - Real-time market data via WebSocket
    - Multi-interval candlestick aggregation
    - Pluggable strategy architecture
    - Paper and live trading modes
    - Backtesting engine
    - REST API for monitoring and control
    - CLI interface

Quick Start:
    >>> from polymoney.core import BaseStrategy, register_strategy
    >>> from polymoney.core.models import PriceData, OrderSignal
    
    >>> @register_strategy("my-strategy")
    ... class MyStrategy(BaseStrategy):
    ...     # Implement your trading logic
    ...     pass

Example Usage:
    # List available strategies
    $ polymoney list-strategies
    
    # Run in paper trading mode
    $ polymoney run --mode paper
    
    # Run a backtest
    $ polymoney backtest --strategy position-arbitrage

For more information, see the documentation at:
    - README.md - Quick start guide
    - docs/strategy-development-guide.md - Strategy development
    - docs/configuration.md - Configuration reference
"""

__version__ = "0.1.0"
__author__ = "Polymoney Team"
__license__ = "MIT"

# Re-export commonly used classes for convenience
from polymoney.core import (
    BaseStrategy,
    register_strategy,
    Config,
    load_config,
    PriceData,
    OrderSignal,
    TradeSide,
    TokenType,
)

__all__ = [
    "__version__",
    "BaseStrategy",
    "register_strategy",
    "Config",
    "load_config",
    "PriceData",
    "OrderSignal",
    "TradeSide",
    "TokenType",
]
