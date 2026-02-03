## 1. Real Data Fetcher Implementation

- [x] 1.1 Create `RealDataFetcher` class in `polymoney/data/real_data_fetcher.py`
- [x] 1.2 Implement Gamma API market search (search by slug pattern "btc-updown-15m")
- [x] 1.3 Implement market details fetch by condition ID from CLOB API
- [x] 1.4 Implement trade history retrieval for completed markets
- [x] 1.5 Implement trade-to-candlestick aggregation with configurable intervals
- [x] 1.6 Add rate limiting with exponential backoff (1s, 2s, 4s, max 60s)
- [x] 1.7 Add file-based caching for completed market trade history

## 2. Command Line Testing

- [x] 2.1 Test Gamma API market discovery manually with curl/httpx
- [x] 2.2 Verify we can find the BTC 15-minute market from the provided URL
- [x] 2.3 Test fetching orderbook for discovered market tokens
- [x] 2.4 Test fetching trade history for a completed market

## 3. Strategy Risk Controls

- [x] 3.1 Add `calculate_ecr()` method to PositionArbitrageStrategy
- [x] 3.2 Implement ECR stop-loss check before generating orders (threshold 105%)
- [x] 3.3 Add `risk_state` tracking ("normal", "warning", "limited")
- [x] 3.4 Log ECR violations with current value and threshold

## 4. Market Order Rebalancing

- [x] 4.1 Add `calculate_balance_ratio()` method to strategy
- [x] 4.2 Implement position imbalance detection (threshold 0.70)
- [x] 4.3 Implement market order generation for underweight side
- [x] 4.4 Add rebalancing cooldown mechanism (60 seconds)
- [x] 4.5 Track rebalancing events in strategy state

## 5. Test Runner Integration

- [x] 5.1 Update `run_real_data_test.py` to use new `RealDataFetcher`
- [x] 5.2 Add CLI option to fetch market by slug pattern
- [x] 5.3 Add CLI option to backtest specific completed market by condition ID
- [x] 5.4 Integrate ECR and balance metrics into test output

## 6. Validation and Reporting

- [x] 6.1 Run backtest on at least 5 real completed BTC 15-minute markets
- [x] 6.2 Compare results with and without ECR stop-loss
- [x] 6.3 Compare results with and without market order rebalancing
- [x] 6.4 Document findings in updated test report
- [x] 6.5 Generate equity curves and performance metrics for each test

## 7. Documentation

- [x] 7.1 Update `docs/test-report-position-arbitrage.md` with real data results
- [x] 7.2 Document new strategy parameters (ecr_threshold, balance_threshold)
- [x] 7.3 Add usage examples for `RealDataFetcher` in docstrings
