## Why

Previous testing used synthetic data and revealed that the position arbitrage strategy has fundamental flaws in trending markets (0% win rate, average ECR 220.5%). However, the API integration for fetching real BTC 15-minute market data from Polymarket was not working (API returned no active markets). We need to:
1. Fix the Polymarket API integration to fetch real market data
2. Validate strategy behavior with actual market price movements
3. Implement suggested improvements (ECR stop-loss, market order rebalancing)
4. Generate reliable backtest results to guide strategy redesign

## What Changes

- **Fix Polymarket API market discovery**: The current `fetch_btc_15min_markets()` function fails to find markets. Implement alternative approaches (search by slug, direct condition ID lookup, or web scraping).
- **Add historical price data fetching**: Implement fetching of historical price/trade data for backtesting completed markets.
- **Implement ECR stop-loss mechanism**: Stop placing orders when predicted ECR exceeds threshold (e.g., 105%).
- **Implement market order rebalancing**: When position imbalance exceeds 20%, use market orders instead of limit orders for the lagging side.
- **Add strategy parameter optimization**: Test different parameter combinations to find optimal settings.
- **Generate comprehensive backtest report**: Document results with real data across multiple market cycles.

## Capabilities

### New Capabilities
- `real-data-fetcher`: Robust Polymarket data fetching with multiple fallback methods (slug search, condition ID lookup, historical trades API). Supports both live market monitoring and historical data retrieval for backtesting.

### Modified Capabilities
- `strategy-engine`: Add ECR stop-loss mechanism and market order rebalancing support (delta spec for new risk controls).

## Impact

- **Code**: `examples/run_real_data_test.py`, `polymoney/data/market_data_service.py`, `polymoney/strategy/builtin/position_arbitrage.py`
- **APIs**: Polymarket CLOB API (`py-clob-client`), possibly Gamma API for market discovery
- **Dependencies**: May need to add `httpx` or `aiohttp` for direct API calls if py-clob-client limitations persist
- **Testing**: New backtest results will be generated in `test_results/` directory
