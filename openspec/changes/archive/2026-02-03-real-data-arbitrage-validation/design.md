## Context

The position arbitrage testing framework is functionally complete, but previous tests used synthetic data because the Polymarket API integration couldn't find active BTC 15-minute markets. Testing with synthetic scenarios revealed that:
- The strategy has 0% win rate in trending markets
- Average ECR is 220.5% (far above the 100% breakeven threshold)
- Single-sided trends cause 100% loss (only losing tokens get filled)

The user has provided a working Polymarket market URL: `https://polymarket.com/event/btc-updown-15m-1770106500`, indicating these markets exist but our discovery mechanism is failing.

**Stakeholders**: Strategy developers, backtesting users
**Constraints**: Must work with Polymarket's public APIs, respect rate limits

## Goals / Non-Goals

**Goals:**
- Fix market discovery to find BTC 15-minute up/down markets
- Fetch historical price data from real markets for backtesting
- Implement ECR stop-loss to prevent runaway losses
- Implement market order rebalancing to correct position imbalance
- Validate strategy improvements with real market data
- Generate actionable backtest results

**Non-Goals:**
- Live trading (remains paper trading for validation)
- Complete strategy redesign (focus on incremental improvements)
- Support for other market types (focus on BTC 15-min)
- Real-time trading during market hours (focus on backtesting completed markets)

## Decisions

### D1: Market Discovery Approach

**Decision**: Use event slug pattern matching + Gamma API as fallback

**Rationale**: 
- The URL pattern `btc-updown-15m-{timestamp}` suggests a predictable slug format
- Gamma API (`https://gamma-api.polymarket.com`) provides market search
- py-clob-client's `get_markets()` may have pagination/filtering issues

**Alternatives considered**:
- Web scraping (fragile, TOS concerns)
- Hardcoded condition IDs (not maintainable)
- Only py-clob-client (current approach, failing)

### D2: Historical Data Source

**Decision**: Use CLOB API trade history + orderbook snapshots

**Rationale**:
- Trade history provides actual execution prices
- Orderbook depth shows market liquidity at each point
- Can reconstruct realistic candlesticks from trade data

**Alternatives considered**:
- Chainlink price feed (resolution source only, not trading data)
- Third-party data providers (cost, complexity)

### D3: ECR Stop-Loss Threshold

**Decision**: Stop placing new orders when projected ECR > 105%

**Rationale**:
- 105% gives 5% buffer for execution slippage
- ECR = total_cost / min(up_shares, down_shares)
- When ECR exceeds 100%, strategy cannot profit regardless of outcome
- Better to stop early than accumulate guaranteed losses

**Alternatives considered**:
- 100% threshold (too tight, may miss recovery opportunities)
- 110% threshold (allows too much loss accumulation)
- Dynamic threshold based on time remaining (complexity)

### D4: Market Order Rebalancing Trigger

**Decision**: Use market orders when position imbalance > 30%

**Rationale**:
- Balance ratio = min(up, down) / max(up, down)
- At 30% imbalance (balance ratio < 0.70), recovery via limit orders unlikely
- Market orders ensure execution but with higher cost
- Only for the underweight side to restore balance

**Alternatives considered**:
- 20% threshold (triggers too often, higher costs)
- 50% threshold (too late to recover)
- Time-based trigger (market at end regardless of balance)

### D5: Data Fetching Implementation

**Decision**: Create `RealDataFetcher` class in `polymoney/data/` module

**Rationale**:
- Separate from existing `MarketDataService` (which is for live streaming)
- Focuses on historical data retrieval for backtesting
- Can be used standalone or integrated with test runner

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| API rate limiting | Implement exponential backoff, cache responses |
| Market slug format changes | Use regex patterns, fallback to Gamma API search |
| Incomplete trade history | Supplement with orderbook snapshots at intervals |
| ECR stop-loss exits too early | Configurable threshold, log reasoning for analysis |
| Market orders increase costs | Only use when balance recovery is critical |
| No active markets for testing | Use recently completed markets, cache historical data |

## Open Questions

1. **Gamma API authentication**: Does market search require API key?
2. **Trade history depth**: How far back can we fetch trades for a market?
3. **Optimal batch size for market orders**: Should it be larger than limit orders?
