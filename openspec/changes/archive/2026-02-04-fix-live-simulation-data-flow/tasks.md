## 1. Market Validity Filtering

- [x] 1.1 Add `_is_market_valid()` method to LiveRunner that checks:
  - Time until settlement >= min_trading_time (default 5 min)
  - Neither price < 0.05 when price_sum >= 0.98
- [x] 1.2 Call `_is_market_valid()` in `_market_scan_loop()` before creating simulation
- [x] 1.3 Add `min_trading_time` to SimulationConfig with default 300 seconds
- [x] 1.4 Add `--min-trading-time` CLI argument to run_live_simulation.py

## 2. Strategy Lifecycle Fix

- [x] 2.1 Call `sim.strategy.on_market_start()` after creating MarketSimulation
- [x] 2.2 Pass market_id and market_info dict with slug, coin, condition_id

## 3. Real-time Market Status

- [x] 3.1 Add `get_status()` method to MarketSimulation class returning position/ECR/prices
- [x] 3.2 Modify `_write_status()` to include `market_details` from all active simulations
- [x] 3.3 Update status_cli.py to display market_details when available (JSON output only)

## 4. Quick Test Mode

- [x] 4.1 Add `--quick-test` flag to run_live_simulation.py
- [x] 4.2 When enabled, set metrics-interval=10, price-interval=2, scan-interval=30
- [x] 4.3 Allow explicit interval values to override quick-test defaults
- [x] 4.4 Update default metrics-interval to 10 seconds (from 300)

## 5. Verification and Testing

- [x] 5.1 Run simulation with `--quick-test` and verify market filtering works
- [x] 5.2 Verify status.json includes market_details with position data
- [x] 5.3 Verify strategy.market_start_time is correctly set
- [x] 5.4 Verify orders are being generated and tracked
