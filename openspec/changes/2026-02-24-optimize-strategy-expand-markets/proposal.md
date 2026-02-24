## Why

The position arbitrage strategy shows +5.3% ROI in paper trading but **-3.7% ROI in live trading** across 161 markets ($40.51 total loss). Root causes identified through deep diagnostic analysis:

1. **Simulation fill model is systematically optimistic**: Paper fill rate ~70% vs live 7-43%. The simulated executor grants 100% fills when market price touches limit, ignores orderbook depth, and uses uncalibrated probabilistic parameters.
2. **ECR prediction assumes all pending orders fill**: `_predict_effective_cost_rate()` counts pending order shares at limit price, but live fill rates are far lower, making risk checks ineffective.
3. **No effective stop-loss**: Single-market losses reach $35+ with ECR values of 2734 or infinity (one-sided positions).
4. **15-minute-only scope misses zero-fee markets**: 1-hour and 4-hour UP/DOWN markets have identical Chainlink oracle mechanics but zero taker fees (vs 1.56% on 15-min markets), representing ~60% higher net profit potential.
5. **No external price signals**: The system lacks Binance/Hyperliquid real-time price feeds that could provide directional alpha, especially in the latter half of longer-duration markets.

## What Changes

### Phase 1: Fix Arbitrage Core (P0)
- **Calibrate simulation fill model**: Use live trading data (59K+ trades) to derive realistic fill probability parameters; enable orderbook depth model in `check_fills()`
- **Fix ECR prediction**: Only use filled positions (not pending orders) for risk calculations
- **Add hard stop-loss**: When realized ECR > 1.0 with no recovery path, stop ordering and optionally sell losing side
- **Unify batch_size behavior**: Apply Polymarket min_order_shares constraint in simulated executor

### Phase 2: Expand to Fee-Free Markets (P1)
- **Add 1-hour and 4-hour market support**: Extend market discovery to match `*-updown-1h-*` and `*-updown-4h-*` slugs
- **Add per-timeframe strategy parameters**: Phase durations, batch ratios, and urgency thresholds scaled to market duration
- **Integrate Binance WebSocket price feed**: Real-time BTC/ETH/SOL price stream as external signal source

### Phase 3: Hybrid Prediction Strategy (P2)
- **Late-game directional signal**: Use Binance price delta vs epoch start price to detect direction in final 30% of market duration
- **Maker-only directional orders**: When signal confidence is high, add position on predicted winning side via maker limit orders

## Capabilities

### New Capabilities
- `binance-price-feed`: Real-time Binance WebSocket price stream for BTC, ETH, SOL providing sub-second price updates independent of Polymarket
- `multi-timeframe-support`: Strategy parameter profiles for 5min, 15min, 1h, and 4h market durations with automatic detection and configuration

### Modified Capabilities
- `strategy-engine`: ECR prediction uses only realized fills; hard stop-loss mechanism; directional signal integration
- `simulated-executor`: Fill model calibrated from live data; orderbook depth integration; min_order_shares enforcement
- `market-data-provider`: Extended market discovery for 1h/4h slugs; Binance price delta tracking per epoch

## Impact

- **Code Changes**:
  - `polymoney/execution/simulated.py`: Calibrated fill model, depth integration, min order enforcement
  - `polymoney/strategy/builtin/position_arbitrage.py`: ECR fix, stop-loss, multi-timeframe params, directional signal
  - `polymoney/data/market_data_provider.py`: 1h/4h market discovery
  - `polymoney/data/binance_feed.py` (new): Binance WebSocket client
  - `polymoney/runner.py`: Per-timeframe config routing, Binance feed lifecycle
  - `config/strategy_defaults.yaml`: Multi-timeframe parameter profiles

- **APIs**: No external API changes. New internal callback for Binance price updates.

- **Testing**:
  - Re-run paper simulation with calibrated fill model; target: paper ROI within 2% of live ROI
  - Live test on 1h markets first (lower risk, zero fees)
  - Target metrics: live ROI > 0%, fill rate deviation < 20% between paper and live

- **Risk**: Strategy behavior changes affect live trading. Phased rollout: paper validation → 1h live → 15min live.
