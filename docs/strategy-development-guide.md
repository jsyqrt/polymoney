# Strategy Development Guide

This guide explains how to create custom trading strategies for the Polymoney framework.

## Overview

Strategies in Polymoney are modular components that:
- Receive real-time market data
- Generate order signals based on trading logic
- Track positions and performance metrics
- Handle market lifecycle events

## Strategy Architecture

### Base Class

All strategies inherit from `BaseStrategy`:

```python
from polymoney.core import BaseStrategy, register_strategy
from polymoney.core.models import (
    PriceData, OrderSignal, TradeSide, TokenType,
    StrategyState, PositionPair
)
```

### Required Methods

Every strategy must implement these abstract methods:

| Method | Purpose | Returns |
|--------|---------|---------|
| `strategy_type` | Property identifying strategy category | `str` |
| `on_price_update()` | Handle price changes, generate signals | `List[OrderSignal]` |
| `on_market_start()` | Initialize for a new market | `None` |
| `on_market_end()` | Handle settlement, report results | `Tuple[float, float, str]` |
| `get_status()` | Return current state for monitoring | `Dict[str, Any]` |

## Creating a Strategy

### Step 1: Define Your Strategy Class

```python
from typing import Any, Dict, List, Optional, Tuple
from polymoney.core import BaseStrategy, register_strategy
from polymoney.core.models import (
    PriceData, OrderSignal, TradeSide, TokenType
)

@register_strategy("my-strategy")
class MyStrategy(BaseStrategy):
    """
    A custom trading strategy.
    
    This strategy implements [describe your approach].
    """
    
    @property
    def strategy_type(self) -> str:
        """Return strategy category."""
        return "custom"  # e.g., "market_maker", "directional", "arbitrage"
```

### Step 2: Implement Price Updates

The `on_price_update` method is called for every price tick:

```python
def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
    """
    Generate order signals based on current prices.
    
    Args:
        price_data: Contains market_id, up_price, down_price, spread
        
    Returns:
        List of OrderSignal objects (empty list = no action)
    """
    signals = []
    
    # Access current prices
    yes_price = price_data.up_price   # Price for YES outcome
    no_price = price_data.down_price  # Price for NO outcome
    mid = price_data.mid_price        # Calculated midpoint
    
    # Access current position
    position = self.get_position(price_data.market_id)
    yes_shares = position.up.shares
    no_shares = position.down.shares
    
    # Check if within position limits
    if position.total_cost < self.position_size:
        # Example: buy YES if price is below threshold
        if yes_price < 0.40:
            signals.append(OrderSignal(
                side=TradeSide.BUY,
                token_type=TokenType.YES,
                target_price=yes_price,
                size=10.0,  # Number of shares
            ))
    
    return signals
```

### Step 3: Handle Market Events

```python
def on_market_start(self, market_id: str, market_info: Dict[str, Any]) -> None:
    """
    Called when subscribing to a market.
    
    Args:
        market_id: Unique market identifier
        market_info: Metadata (title, token_ids, etc.)
    """
    # Reset any per-market state
    self.reset()
    
    # Store market info if needed
    self._market_info = market_info
    
    # Access strategy parameters
    threshold = self.params.get("threshold", 0.40)

def on_market_end(self, market_id: str, winner: Optional[str]) -> Tuple[float, float, str]:
    """
    Called when market settles.
    
    Args:
        market_id: Market identifier
        winner: "yes"/"no" or None if unknown
        
    Returns:
        (total_cost, profit_loss, outcome)
    """
    position = self.get_position(market_id)
    cost = position.total_cost
    
    # Calculate PnL based on outcome
    if winner == "yes":
        pnl = position.up.shares - cost  # YES pays $1 per share
    elif winner == "no":
        pnl = position.down.shares - cost
    else:
        pnl = 0.0  # Unknown outcome
    
    return cost, pnl, winner or "unknown"
```

### Step 4: Implement Status Reporting

```python
def get_status(self) -> Dict[str, Any]:
    """
    Return strategy status for monitoring.
    
    Returns:
        Dictionary with status information
    """
    return {
        "strategy_id": self.strategy_id,
        "name": self.name,
        "type": self.strategy_type,
        "state": self.state.value,
        "position_size": self.position_size,
        "params": self.params,
        "positions": {
            market_id: {
                "yes_shares": pos.up.shares,
                "yes_cost": pos.up.cost,
                "no_shares": pos.down.shares,
                "no_cost": pos.down.cost,
                "total_cost": pos.total_cost,
            }
            for market_id, pos in self._positions.items()
        },
        "metrics": self.metrics.model_dump(),
    }
```

## Strategy Registration

The `@register_strategy` decorator registers your strategy:

```python
@register_strategy("my-strategy")  # Use kebab-case names
class MyStrategy(BaseStrategy):
    ...
```

After registration:
- Strategy appears in `polymoney list-strategies`
- Can be created via API: `POST /strategies` with `strategy_type: "my-strategy"`
- Can be used in backtests: `polymoney backtest --strategy my-strategy`

## Strategy Parameters

Pass custom parameters during creation:

```python
# Via CLI/config
params:
  threshold: 0.40
  max_orders: 5

# Access in strategy
def on_price_update(self, price_data: PriceData):
    threshold = self.params.get("threshold", 0.40)
    max_orders = self.params.get("max_orders", 5)
```

## Position Management

### Position Tracking

The framework automatically tracks positions:

```python
# Get position for a market
position = self.get_position(market_id)

# Access position details
position.up.shares      # YES shares held
position.up.cost        # Total cost paid for YES
position.up.avg_price   # Average entry price for YES

position.down.shares    # NO shares held
position.down.cost      # Total cost paid for NO
position.down.avg_price # Average entry price for NO

position.total_cost     # Combined cost (YES + NO)
```

### Order Fill Handling

Override `on_order_filled` for custom fill handling:

```python
def on_order_filled(self, trade: TradeResult) -> None:
    """Called when an order fills."""
    # Default implementation updates positions
    super().on_order_filled(trade)
    
    # Custom logic
    self.log_trade(trade)
```

## State Management

### Strategy States

```python
from polymoney.core.models import StrategyState

# Available states
StrategyState.INITIALIZED  # Created but not started
StrategyState.RUNNING      # Actively trading
StrategyState.PAUSED       # Temporarily stopped
StrategyState.STOPPED      # Fully stopped
```

### State Transitions

```python
# Check state
if self.is_running:
    # Process signals
    
# State is managed by StrategyEngine
# Don't modify directly in strategy code
```

## Testing Strategies

### Unit Tests

```python
import pytest
from my_strategy import MyStrategy
from polymoney.core.models import PriceData, TradeSide

def test_generates_buy_signal():
    strategy = MyStrategy(
        strategy_id="test",
        name="test",
        position_size=100,
    )
    
    price_data = PriceData(
        market_id="test-market",
        up_price=0.35,
        down_price=0.65,
    )
    
    signals = strategy.on_price_update(price_data)
    
    assert len(signals) == 1
    assert signals[0].side == TradeSide.BUY
```

### Backtesting

```bash
# Run backtest
polymoney backtest \
    --strategy my-strategy \
    --start 2024-01-01 \
    --end 2024-01-31 \
    --params '{"threshold": 0.35}'
```

## Best Practices

### 1. Parameter Validation

```python
def __init__(self, **kwargs):
    super().__init__(**kwargs)
    
    # Validate parameters
    self.threshold = self.params.get("threshold", 0.40)
    if not 0 < self.threshold < 1:
        raise ValueError("threshold must be between 0 and 1")
```

### 2. Position Size Limits

```python
def on_price_update(self, price_data: PriceData):
    position = self.get_position(price_data.market_id)
    
    # Respect position limits
    available = self.position_size - position.total_cost
    if available <= 0:
        return []  # At limit, no new orders
```

### 3. Defensive Coding

```python
def on_price_update(self, price_data: PriceData):
    try:
        return self._generate_signals(price_data)
    except Exception as e:
        # Log but don't crash
        self.logger.error(f"Signal generation failed: {e}")
        return []
```

### 4. Logging

```python
from polymoney.core.logging import get_logger

class MyStrategy(BaseStrategy):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = get_logger(f"strategy.{self.strategy_id}")
    
    def on_price_update(self, price_data: PriceData):
        self.logger.debug(f"Price update: {price_data.up_price:.4f}")
```

## Example: Complete Strategy

```python
from typing import Any, Dict, List, Optional, Tuple
from polymoney.core import BaseStrategy, register_strategy
from polymoney.core.logging import get_logger
from polymoney.core.models import (
    PriceData, OrderSignal, TradeSide, TokenType
)


@register_strategy("threshold-buyer")
class ThresholdBuyerStrategy(BaseStrategy):
    """
    Simple strategy that buys YES when price is below threshold.
    
    Parameters:
        threshold (float): Buy when YES price is below this (default: 0.40)
        order_size (float): Shares per order (default: 10.0)
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.logger = get_logger(f"strategy.{self.strategy_id}")
        
        # Extract and validate parameters
        self.threshold = self.params.get("threshold", 0.40)
        self.order_size = self.params.get("order_size", 10.0)
        
        if not 0 < self.threshold < 1:
            raise ValueError("threshold must be between 0 and 1")
    
    @property
    def strategy_type(self) -> str:
        return "directional"
    
    def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
        signals = []
        position = self.get_position(price_data.market_id)
        
        # Check position limits
        available = self.position_size - position.total_cost
        if available < self.order_size * price_data.up_price:
            return []
        
        # Generate signal if price below threshold
        if price_data.up_price < self.threshold:
            signals.append(OrderSignal(
                side=TradeSide.BUY,
                token_type=TokenType.YES,
                target_price=price_data.up_price,
                size=self.order_size,
            ))
            self.logger.info(
                f"Buy signal: {self.order_size} YES @ {price_data.up_price:.4f}"
            )
        
        return signals
    
    def on_market_start(self, market_id: str, market_info: Dict[str, Any]) -> None:
        self.logger.info(f"Market started: {market_info.get('title', market_id)}")
        self.reset()
    
    def on_market_end(self, market_id: str, winner: Optional[str]) -> Tuple[float, float, str]:
        position = self.get_position(market_id)
        cost = position.total_cost
        
        if winner == "yes":
            pnl = position.up.shares - cost
        elif winner == "no":
            pnl = position.down.shares - cost
        else:
            pnl = 0.0
        
        self.logger.info(f"Market ended: cost={cost:.2f}, pnl={pnl:.2f}")
        return cost, pnl, winner or "unknown"
    
    def get_status(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "type": self.strategy_type,
            "state": self.state.value,
            "threshold": self.threshold,
            "order_size": self.order_size,
            "positions": {
                mid: {"yes": p.up.shares, "no": p.down.shares}
                for mid, p in self._positions.items()
            },
        }
```

## File Location

Place strategy files in:
- `polymoney/strategy/builtin/` - For strategies included with the framework
- Custom location and import in your configuration

Remember to import your strategy module so registration occurs:

```python
# In polymoney/strategy/__init__.py or your app startup
from polymoney.strategy.builtin import position_arbitrage
from my_strategies import threshold_buyer  # Your custom strategies
```
