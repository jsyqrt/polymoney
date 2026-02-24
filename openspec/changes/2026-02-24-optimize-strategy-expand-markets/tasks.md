## Phase 1: Fix Arbitrage Core (P0)

### 1.1 Calibrate Simulation Fill Model

- [x] 1.1.1 ~~Write analysis script~~ → Analyzed live data (59K+ trades, 7-43% fill rates) to derive calibrated parameters
- [x] 1.1.2 ~~Generate calibration lookup table~~ → Implemented inline calibrated probabilities: 2-15% base (was 5-35%), touch-fill 60-85% (was 100%)
- [x] 1.1.3 Replaced hardcoded `base_prob = 0.05 + 0.30 * proximity` with `base_prob = 0.02 + 0.13 * proximity`
- [x] 1.1.4 Replaced `price_delta` directional adjustment with `trend_ema`-based (4-tier) adjustment for robustness
- [x] 1.1.5 Changed market-touches-limit from guaranteed fill to probabilistic (60-85% based on trend EMA)
- [ ] 1.1.6 Add partial fill simulation: when orderbook depth < order size, fill proportionally (deferred — needs orderbook snapshot support)

### 1.2 Enforce Min Order Size in Simulator

- [x] 1.2.1 In `SimulatedExecutor.submit_order()`, reject orders where `order.size < config.min_order_shares`
- [x] 1.2.2 Apply Polymarket price clamping in simulator: `max(0.01, min(0.99, round(price, 2)))`
- [x] 1.2.3 Added `min_order_shares` field to `MarketExecutionConfig` (default 5.0)

### 1.3 Fix ECR Prediction

- [x] 1.3.1 Modified `_predict_effective_cost_rate()`: removed `pending_up_shares`/`pending_down_shares` from share count
- [x] 1.3.2 Pending order cost still counted (capital exposure) but shares excluded (uncertain fill rates)
- [x] 1.3.3 Added `realized_ecr` property — ECR from filled positions only
- [x] 1.3.4 Added realized ECR hard gate in `_should_place_order()`: blocks non-lagging-side orders when ECR > threshold

### 1.4 Add Hard Stop-Loss (Abandon Mechanism)

- [x] 1.4.1 Added `_should_abandon_market()`: triggers when realized ECR > 1.02 AND balance_ratio < 0.30 AND time > 50% elapsed
- [x] 1.4.2 Added `_abandon_mode` flag (distinct from `_exit_mode`)
- [x] 1.4.3 In `on_price_update()`, abandon check returns empty signals before any order generation
- [x] 1.4.4 Configurable: `abandon_ecr_threshold`, `abandon_balance_threshold`, `abandon_time_threshold`
- [x] 1.4.5 Logs abandon events with ECR, balance_ratio, position details

### 1.5 Unify Fee Model

- [x] 1.5.1 Replaced `min(p, 1-p) * 0.0312` with official `0.25 * (p*(1-p))^2` formula
- [ ] 1.5.2 Add fee-free mode for 1h/4h markets (deferred — needs per-market fee detection)
- [ ] 1.5.3 Detect fee status from market metadata (deferred — needs API integration)

## Phase 2: Expand to Fee-Free Markets (P1)

### 2.1 Multi-Timeframe Market Discovery

- [x] 2.1.1 Added `TIMEFRAMES` dict and `find_markets_by_timeframe()` to `RealDataFetcher` for 15m/1h/4h
- [x] 2.1.2 Added `timeframe` field to `MarketEventData` dataclass
- [x] 2.1.3 `_discover_market()` detects timeframe from slug pattern and passes through `market_info`
- [x] 2.1.4 Extended `_market_scan_loop()` to scan all configured timeframes (configurable via `config.timeframes`)

### 2.2 Per-Timeframe Strategy Configuration

- [x] 2.2.1 Defined `TIMEFRAME_DEFAULTS` in `TradingRunner` with phase1/phase2/duration overrides per timeframe
- [x] 2.2.2 Added default profiles in `strategy_defaults.yaml` for 15m/1h/4h
- [x] 2.2.3 `_on_market_discovered()` applies `TimeframeConfig` overrides to strategy instance via `_get_timeframe_overrides()`
- [ ] 2.2.4 ~~In `TradingRunner._setup_market()`~~ → order_timeout/stale_threshold from YAML (deferred — needs MarketExecutionConfig extension)

### 2.3 Concurrent Market Budget Management

- [ ] 2.3.1 Review `max_concurrent_markets` for mixed-timeframe operation (deferred — operational config)
- [ ] 2.3.2 Add per-timeframe position_size config (deferred — operational config)
- [ ] 2.3.3 Add per-timeframe concurrent market logging (deferred — operational config)

## Phase 3: Binance Price Feed Integration (P2)

### 3.1 Binance WebSocket Client

- [x] 3.1.1 Created `polymoney/data/binance_feed.py` with `BinancePriceFeed` class
- [x] 3.1.2 WebSocket connection to `wss://stream.binance.com:9443/ws` with combined `@trade` streams
- [x] 3.1.3 Reconnection logic with exponential backoff (1s → 60s max)
- [x] 3.1.4 `get_price(coin)` returns latest Binance price
- [x] 3.1.5 Epoch tracking: `record_epoch_start()` / `get_epoch_delta()` for per-market delta calculation
- [x] 3.1.6 Pending epoch handling: fills start price from first trade when Binance not yet connected
- [ ] 3.1.7 Heartbeat/stall detection (deferred — low priority)

### 3.2 Integration with Market Data Provider

- [x] 3.2.1 Runner starts/stops `BinancePriceFeed` alongside data provider (gated by `config.enable_binance_feed`)
- [x] 3.2.2 On market discovery, `record_epoch_start(coin, epoch_ts)` called via `_parse_epoch_from_slug()`
- [x] 3.2.3 On each price update, `binance_delta` injected into strategy via `ctx.strategy.binance_delta`
- [x] 3.2.4 `binance_price` also injected for potential future use
- [x] 3.2.5 Graceful degradation: `binance_delta = None` when feed unavailable → signal skipped

### 3.3 Late-Game Directional Signal in Strategy

- [x] 3.3.1 Added `_get_directional_signal()` → returns `("up"|"down", confidence)` or None
- [x] 3.3.2 Signal activates when `time_remaining < 30%` and `|binance_delta| > 0.3%`; confidence scales to 1.0 at 1% delta
- [x] 3.3.3 In `_calculate_limit_prices()`, directional bias redistributes discount (up to 30% of total offset)
- [x] 3.3.4 Pair cost ceiling enforced after directional adjustment → no additional cost
- [x] 3.3.5 Config: `enable_directional_signal`, `directional_min_delta`, `directional_late_game_ratio`
- [x] 3.3.6 Logs first activation per market with delta, confidence values

## Phase 4: Validation and Deployment

### 4.1 Paper Simulation Validation

- [ ] 4.1.1 Run calibrated paper simulation for 24h on 15min markets
- [ ] 4.1.2 Run paper simulation on 1h markets for 24h
- [ ] 4.1.3 Compare paper vs live fill rate deviation (target < 20%)

### 4.2 Live Deployment (Staged)

- [ ] 4.2.1 Deploy to live on 1h markets first for 48h observation
- [ ] 4.2.2 Monitor key metrics: fill rate, ECR, balance_ratio, abandon rate
- [ ] 4.2.3 If 1h live ROI > 0%, extend to 4h markets
- [ ] 4.2.4 Apply calibrated parameters to 15min live trading

### 4.3 Documentation

- [ ] 4.3.1 Update analysis report with calibration methodology
- [ ] 4.3.2 Update live-simulation guide with multi-timeframe config
- [x] 4.3.3 Updated `strategy_defaults.yaml` with timeframe profiles and stop-loss section
