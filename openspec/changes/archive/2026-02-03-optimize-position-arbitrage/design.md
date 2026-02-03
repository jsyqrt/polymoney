## Context

The position arbitrage strategy was designed to profit by buying both UP and DOWN tokens with total cost < $1. In theory, this guarantees profit regardless of outcome. However, comprehensive testing revealed critical flaws:

**Current State:**
- 0% win rate across 15 test scenarios
- 220.5% average ECR (effective cost rate) - should be <100%
- In trending markets, only losing-side orders fill
- Example strategy (`examples/position_arbitrage.py`) has proven improvements not yet integrated

**Key Insight from Example Strategy:**
The example implements ECR prediction before ordering - it calculates what ECR would be AFTER a hypothetical order fills and rejects orders that would worsen the situation. This prevents the death spiral where the strategy keeps buying losing-side tokens.

## Goals / Non-Goals

**Goals:**
- Integrate proven improvements from example strategy (ECR prediction, dynamic sizing)
- Achieve >50% win rate in comprehensive scenario testing
- Maintain average ECR below 100%
- Detect and respond to one-sided trends before positions become unrecoverable
- Increase urgency as market end approaches

**Non-Goals:**
- Predicting market direction (strategy remains market-neutral in intent)
- Real-time ML-based price prediction
- Cross-market arbitrage (focus on single market optimization)
- Changing the fundamental limit order approach

## Decisions

### Decision 1: Port Example Strategy's ECR Protection

**Choice**: Integrate `_predict_effective_cost_rate()` and strict ECR tolerance checks

**Rationale**: 
- Example strategy calculates ECR before placing orders
- Rejects orders that would worsen ECR by >1%
- Prevents accumulating losing-side positions
- Already proven effective in the codebase

**Alternatives Considered**:
- Post-hoc ECR stop-loss (current approach): Too late, damage already done
- No ECR protection: Leads to catastrophic losses in trending markets

### Decision 2: Trend Detection via Price Momentum

**Choice**: Implement simple momentum-based trend detection using price change over sliding window

**Algorithm**:
```
momentum = (current_price - price_N_ticks_ago) / price_N_ticks_ago
if abs(momentum) > threshold (e.g., 5%):
    trend_side = "up" if momentum > 0 else "down"
    confidence = min(1.0, abs(momentum) / 0.10)  # 10% = full confidence
```

**Rationale**:
- Simple to implement and understand
- No external dependencies
- Can be tuned with threshold parameter
- Triggers early warning before positions become unrecoverable

**Alternatives Considered**:
- Moving average crossover: More complex, requires more data
- Order book imbalance: Requires additional data not available in backtesting
- Machine learning: Overkill for this use case, hard to backtest

### Decision 3: Urgency-Based Limit Pricing

**Choice**: Scale limit price offset based on time remaining and position imbalance

**Formula**:
```
base_offset = target_cost / price_sum - 1  # Standard offset
urgency = (1 - time_remaining / market_duration) * imbalance_factor
adjusted_offset = base_offset * (1 - urgency * 0.5)  # Reduce offset by up to 50%
limit_price = market_price * (1 + adjusted_offset)
```

**Rationale**:
- More aggressive pricing as market end approaches
- Prioritizes filling over optimal price near settlement
- Already partially implemented in example (imbalance-based adjustment)

**Alternatives Considered**:
- Fixed time-based phases (current): Too rigid, doesn't adapt to position state
- Market orders only near end: Higher slippage risk

### Decision 4: Aggressive Market Order Rebalancing

**Choice**: Trigger market order when balance_ratio < 0.50 (severe imbalance)

**Rationale**:
- Current threshold (0.70) may be too conservative
- Severe imbalance (0.50) indicates high risk of total loss
- Market order fills immediately, more reliable than limit orders
- Already implemented but disabled by default

**Configuration**:
- `enable_rebalancing`: True (change default)
- `rebalancing_trigger`: 0.50 (new threshold for market orders)
- `rebalancing_cooldown`: 30s (reduce from 60s for faster response)

### Decision 5: Dynamic Order Sizing for Lagging Side

**Choice**: Scale order size up to 5x batch_size for lagging side during imbalance

**Implementation** (from example):
```python
if is_imbalanced and gap_shares > 0:
    desired_cost = gap_shares * primary_limit
    order_cost = min(available, max(self.batch_size, min(self.batch_size * 5, desired_cost)))
```

**Rationale**:
- Faster position rebalancing
- Already proven in example strategy
- Self-limiting (capped at 5x and available budget)

## Risks / Trade-offs

### Risk 1: Trend Detection False Positives
- **Risk**: Detecting "trend" that reverses, causing premature order stops
- **Mitigation**: Use confidence threshold, require sustained momentum over multiple ticks

### Risk 2: Market Order Slippage
- **Risk**: Market orders for rebalancing may fill at unfavorable prices
- **Mitigation**: Cap market order size at 10% of position, use FOK order type

### Risk 3: Reduced Fill Rate
- **Risk**: More aggressive ECR filtering may reduce total position size
- **Mitigation**: Accept lower fill rate if it means avoiding catastrophic losses

### Risk 4: Strategy Complexity
- **Risk**: More decision logic increases debugging difficulty
- **Mitigation**: Comprehensive logging at each decision point, clear state tracking

## Open Questions

1. **Optimal momentum threshold**: Need to backtest different values (3%, 5%, 10%)
2. **Market order size limit**: What's the right cap for rebalancing orders?
3. **Urgency curve shape**: Linear vs exponential urgency scaling near market end?
