## Context

Deep diagnostic analysis of the PolyMoney trading system revealed a fundamental simulation-to-live gap:

**Paper trading**: 1,107 markets, +$4,806.91 PnL, +5.3% ROI, ~70% fill rate
**Live trading**: 161 markets, -$40.51 PnL, -3.7% ROI, 7-43% fill rate

The largest live losses ($35, $25, $17) all show extreme ECR (2734, 7.84, 2.05) and severe position imbalance (balance_ratio near 0 or 1), caused by one-sided fills in trending markets.

Additionally, the system only trades 15-minute markets which charge 1.56% taker fees, while identical 1-hour and 4-hour markets have zero fees and longer windows that naturally improve fill balance.

## Goals / Non-Goals

**Goals:**
- Make simulation fill rates match live fill rates within 20% relative error
- Prevent single-market losses exceeding $10 via hard stop-loss
- Expand to 1h and 4h crypto markets for zero-fee trading
- Integrate Binance real-time prices as directional signal for late-game enhancement
- Achieve live ROI > 0% consistently

**Non-Goals:**
- Full ML prediction model (Phase 3 only adds simple delta-based signal)
- Sports market integration (separate future change)
- Cross-platform arbitrage (Kalshi etc.)
- Changing from maker-first to taker-first execution

## Decisions

### Decision 1: Calibrate Fill Model from Live Data

**Choice**: Extract fill statistics from `trades.jsonl` and `results.jsonl` (59K+ trades, 2000+ markets) to derive empirical fill probability parameters, replacing hardcoded values in `SimulatedExecutor.check_fills()`.

**Approach**:
- Group historical orders by `(price_diff_bucket, spread_bucket, trend_direction, order_size_bucket)`
- Compute empirical `P(fill | features)` for each bucket
- Replace `base_prob = 0.05 + 0.30 * proximity` with calibrated lookup
- Enable the existing but unused `_simulate_fill()` depth-based model when orderbook snapshots are available
- Add partial fill simulation (currently all-or-nothing)

**Rationale**: The current probabilistic model uses arbitrary parameters (5%-40% base probability) that produce ~70% fill rates while live shows 7-43%. Empirical calibration from 59K real trades provides ground truth.

**Alternatives Considered**:
- Keep current model but reduce probabilities: Would still be uncalibrated, fragile
- Use only depth-based model: Orderbook snapshots aren't always available

### Decision 2: ECR Prediction Using Only Realized Fills

**Choice**: Modify `_predict_effective_cost_rate()` to exclude pending orders from share count. Only count `up_position.shares` and `down_position.shares` (already filled).

**Current problem**:
```python
# Current: counts pending orders as if they'll fill
pending_up_shares = sum(o.shares for o in self.pending_orders if o.side == "up")
up = self.up_position.shares + pending_up_shares
```

**New approach**:
```python
# Fixed: only count realized fills
up = self.up_position.shares
down = self.down_position.shares
# Pending orders contribute to cost exposure but NOT to hedged share count
```

**Rationale**: In live trading with 7-43% fill rates, counting pending orders as filled makes ECR appear much better than reality. This causes the strategy to keep ordering when it should stop.

### Decision 3: Hard Stop-Loss Mechanism

**Choice**: When realized ECR > 1.02 and balance_ratio < 0.3 with more than 50% of market duration elapsed, enter "abandon" mode:
1. Cancel all pending orders immediately
2. Do not place any new orders
3. Log the abandonment for analysis
4. Optionally sell the overweight side if ECR > 1.10 (market sell at best available price)

**Rationale**: The largest losses ($35, $25) occur when the strategy keeps trying to "catch up" on the lagging side in a market that has already moved decisively. Cutting losses early limits downside to 1-2% of position instead of 30%+.

### Decision 4: Multi-Timeframe Market Support

**Choice**: Extend `MarketDataProvider` market discovery to match 1h and 4h slug patterns. Add a `TimeframeConfig` dataclass that maps market duration to strategy parameters.

**Parameter scaling**:
```
                    15min     1hour     4hour
phase1_end          300s      1200s     3600s
phase2_end          600s      2400s     10800s
market_duration     900s      3600s     14400s
order_timeout       30s       60s       120s
batch_ratio         0.001     0.0005    0.0003
min_trading_time    300s      600s      1800s
```

**Rationale**: 1h and 4h markets have zero taker fees and longer windows. The same arbitrage logic applies but needs proportionally scaled timing parameters.

### Decision 5: Binance WebSocket Price Feed

**Choice**: Create `BinancePriceFeed` class that connects to Binance WebSocket (`wss://stream.binance.com:9443/ws`) subscribing to `btcusdt@trade`, `ethusdt@trade`, `solusdt@trade` streams. Track per-epoch start price and compute real-time delta.

**Integration point**: `MarketDataProvider` receives Binance price callbacks and stores `(epoch_start_price, current_price, delta)` per coin per market epoch. Strategy can query this via `market_info` context.

**Late-game signal**: When `time_remaining < 0.3 * market_duration` and `|binance_delta| > 0.003` (0.3%), emit a directional confidence score. Strategy uses this to bias limit prices toward the predicted winning side (still as maker orders, 0% fee).

**Rationale**: Binance price leads Chainlink by 100ms-3s. In the final minutes of a market, this signal has high predictive value. Using maker orders avoids the 1.56% taker fee that would eat the edge.

### Decision 6: Unified Min Order Size in Simulation

**Choice**: Add `min_order_shares` enforcement in `SimulatedExecutor.submit_order()`. Reject orders where `order.size < market_state.config.min_order_shares`.

**Rationale**: Polymarket enforces minimum 5 shares per order. Simulated executor accepts any size, causing paper trading to submit many tiny orders that would be rejected in live. This inflates paper fill counts.

## Component Design

### New: `polymoney/data/binance_feed.py`

```
BinancePriceFeed
├── __init__(coins: List[str])
├── async start()           # Connect WS, subscribe to trade streams
├── async stop()            # Disconnect
├── get_price(coin) → float # Latest Binance price
├── get_epoch_delta(coin, epoch_start_time) → float  # (current - start) / start
├── on_price_update: Callback[(coin, price, timestamp)]
└── _epoch_start_prices: Dict[str, Dict[int, float]]  # coin → {epoch_ts: start_price}
```

### Modified: `polymoney/execution/simulated.py`

- `check_fills()`: Replace hardcoded probability with calibrated lookup table
- `check_fills()`: Call `_simulate_fill()` when orderbook snapshot available (currently dead code)
- `submit_order()`: Enforce `min_order_shares` from market config
- New: `_calibrated_fill_probability(price_diff, spread, trend, size) → float`
- New: Partial fill logic (fill fraction based on depth when snapshot available)

### Modified: `polymoney/strategy/builtin/position_arbitrage.py`

- `_predict_effective_cost_rate()`: Remove pending order shares from calculation
- New: `_should_abandon_market() → bool`: Hard stop-loss check
- New: `_get_directional_signal(market_info) → Optional[Tuple[str, float]]`: Query Binance delta
- `on_price_update()`: Check abandon condition before generating orders
- `on_price_update()`: In late-game phase, apply directional bias to limit prices
- `__init__()`: Accept `TimeframeConfig` for multi-timeframe parameter scaling

### Modified: `polymoney/data/market_data_provider.py`

- `_on_market_event()`: Match 1h/4h slug patterns in addition to 15min
- New: Track epoch start timestamps for Binance delta calculation
- New: Pass `binance_delta` in `market_info` dict to strategy

### Modified: `polymoney/runner.py`

- `_setup_market()`: Detect timeframe from slug, apply `TimeframeConfig`
- `run()`: Start/stop `BinancePriceFeed` lifecycle
- Pass Binance price context through to strategy via market_info

### Modified: `config/strategy_defaults.yaml`

- Add `timeframes:` section with 15min/1h/4h parameter profiles
- Add `binance:` section with WebSocket config
- Add `stop_loss:` section with abandon thresholds
