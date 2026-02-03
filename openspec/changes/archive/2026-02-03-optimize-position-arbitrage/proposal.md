## Why

Position arbitrage strategy achieves 0% win rate in comprehensive testing with 220.5% average ECR (should be <100%). Root cause: in trending markets, only losing-side limit orders fill, causing 100% loss. The example strategy (`examples/position_arbitrage.py`) contains proven improvements not yet integrated into the main strategy.

## What Changes

- **Integrate example strategy improvements**: Port ECR prediction, dynamic order sizing, and improved imbalance handling from `examples/position_arbitrage.py` to main strategy
- **Add aggressive rebalancing**: Use market orders (not just limit orders) when imbalance exceeds critical threshold (balance_ratio < 0.50)
- **Implement trend detection**: Detect one-sided price trends and stop new orders early to prevent accumulating losing-side positions
- **Add urgency-based pricing**: Dynamically adjust limit prices based on time remaining and position balance - more aggressive as market end approaches
- **Improve order fill prediction**: Pre-calculate expected fill probability before placing orders based on price distance from market

## Capabilities

### New Capabilities

- `trend-detection`: Detect one-sided price trends using moving average comparison and price momentum, triggering strategy mode switch when trend confidence exceeds threshold
- `urgency-pricing`: Time-weighted limit price calculation that becomes more aggressive as market end approaches and position imbalance increases

### Modified Capabilities

- `strategy-engine`: Extend ECR protection to include pre-order ECR projection that rejects orders worsening ECR beyond 1% tolerance (as implemented in example strategy)

## Impact

- **Code Changes**:
  - `polymoney/strategy/builtin/position_arbitrage.py`: Major refactor to integrate improvements
  - New utility modules for trend detection and urgency pricing
  
- **APIs**: No external API changes, only internal strategy behavior improvements

- **Testing**: 
  - Re-run comprehensive scenario tests with updated strategy
  - Target metrics: >50% win rate, <100% average ECR, >70% fill rate

- **Risk**: Strategy behavior changes may affect live trading users - recommend paper testing before live deployment
