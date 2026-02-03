## 1. Project Setup

- [x] 1.1 Create project structure: `polymoney/{core,data,strategy,backtest,api,cli}/`
- [x] 1.2 Create `pyproject.toml` with dependencies (py-clob-client, websockets, fastapi, uvicorn, sqlalchemy, pydantic)
- [x] 1.3 Create `requirements.txt` for pip installation
- [x] 1.4 Setup logging configuration module
- [x] 1.5 Create configuration management (load from env/yaml)

## 2. Core Data Models

- [x] 2.1 Define `PriceData` model (market_id, up_price, down_price, timestamp, spread)
- [x] 2.2 Define `Candlestick` model (market_id, interval, timestamp, OHLCV, token_type)
- [x] 2.3 Define `Order` model (order_id, strategy_id, market_id, side, token_type, price, size, status)
- [x] 2.4 Define `OrderSignal` model (side, target_price, size)
- [x] 2.5 Define `Position` model (shares, cost, avg_price)
- [x] 2.6 Define `TradeResult` model (trade_id, order_id, price, size, timestamp)
- [x] 2.7 Define market event models (MarketStart, MarketEnd, MarketSettled)

## 3. Strategy Base Class and Registry

- [x] 3.1 Implement `BaseStrategy` abstract class with required interface methods
- [x] 3.2 Implement `@register_strategy` decorator for strategy registration
- [x] 3.3 Implement strategy registry (list, lookup, instantiate by name)
- [x] 3.4 Implement strategy lifecycle states (initialized, running, paused, stopped)
- [x] 3.5 Add strategy configuration validation with Pydantic

## 4. Market Data Service

- [x] 4.1 Implement WebSocket connection manager with auto-reconnect
- [x] 4.2 Implement orderbook snapshot and incremental update processing
- [x] 4.3 Implement trade message parsing and event emission
- [x] 4.4 Implement price update event emission (best bid/ask)
- [x] 4.5 Implement market lifecycle event detection (start, end, settlement)
- [x] 4.6 Implement concurrent multi-market subscription support
- [x] 4.7 Add connection status monitoring and health reporting

## 5. Candlestick Aggregation

- [x] 5.1 Implement `CandlestickAggregator` with in-memory aggregation
- [x] 5.2 Implement multi-interval support (1m, 5m, 15m, 1h, 4h, 1d)
- [x] 5.3 Implement interval boundary detection and candlestick finalization
- [x] 5.4 Implement candlestick_complete event emission
- [x] 5.5 Add separate YES/NO token price tracking

## 6. Order Manager

- [x] 6.1 Implement `OrderManager` abstract base class
- [x] 6.2 Implement `PaperOrderManager` with simulated execution
- [x] 6.3 Add slippage simulation to PaperOrderManager
- [x] 6.4 Implement `LiveOrderManager` using py-clob-client
- [x] 6.5 Implement order status tracking (pending, filled, cancelled, rejected)
- [x] 6.6 Implement order cancellation logic
- [x] 6.7 Add order fill notification callbacks

## 7. Strategy Engine

- [x] 7.1 Implement `StrategyEngine` to manage multiple strategy instances
- [x] 7.2 Implement strategy subscription to market data events
- [x] 7.3 Implement position tracking per strategy-market
- [x] 7.4 Implement strategy start/stop/pause lifecycle management
- [x] 7.5 Add risk controls (position size limit, order rate limiting)
- [x] 7.6 Implement trading mode switching (paper/live)

## 8. Data Storage Layer

- [x] 8.1 Create SQLAlchemy models for all entities (candlesticks, orders, trades, positions)
- [x] 8.2 Implement database initialization and schema migration
- [x] 8.3 Implement candlestick storage with time-range queries
- [x] 8.4 Implement trade record storage
- [x] 8.5 Implement order record storage with status history
- [x] 8.6 Implement position snapshot storage and restoration
- [x] 8.7 Implement configuration persistence
- [x] 8.8 Add data retention policy (configurable cleanup)

## 9. API Server

- [x] 9.1 Setup FastAPI application with CORS and error handling
- [x] 9.2 Implement GET /strategies and GET /strategies/{id}
- [x] 9.3 Implement POST /strategies/{id}/start, /stop, /pause
- [x] 9.4 Implement PUT /strategies/{id}/config
- [x] 9.5 Implement GET /markets and POST /markets/{id}/subscribe
- [x] 9.6 Implement GET /orders with filters
- [x] 9.7 Implement GET /metrics and GET /metrics/strategies/{id}
- [x] 9.8 Implement GET /health and GET /logs
- [x] 9.9 Add optional API key authentication
- [x] 9.10 Verify auto-generated OpenAPI docs at /docs

## 10. Backtesting Engine

- [x] 10.1 Implement historical data loader from storage
- [x] 10.2 Implement data replay with chronological event emission
- [x] 10.3 Implement backtest order execution simulation
- [x] 10.4 Implement performance metrics calculation (PnL, win rate, max drawdown)
- [x] 10.5 Implement backtest results export (JSON, CSV)
- [x] 10.6 Add replay speed control (instant, 10x, 100x)
- [x] 10.7 Add multi-market backtest support

## 11. Built-in Strategy Migration

- [x] 11.1 Migrate `examples/position_arbitrage.py` to `polymoney/strategy/builtin/`
- [x] 11.2 Refactor to inherit from BaseStrategy
- [x] 11.3 Register with `@register_strategy("position-arbitrage")`
- [x] 11.4 Add unit tests for position arbitrage strategy

## 12. CLI Tool

- [x] 12.1 Create CLI entry point with Click/Typer
- [x] 12.2 Add `run` command to start the bot
- [x] 12.3 Add `backtest` command with date range and strategy options
- [x] 12.4 Add `list-strategies` command
- [x] 12.5 Add `status` command to query running state

## 13. Testing and Validation

- [x] 13.1 Add unit tests for core data models
- [x] 13.2 Add unit tests for strategy registry
- [x] 13.3 Add integration tests for market data service (mock WebSocket)
- [x] 13.4 Add integration tests for order managers
- [x] 13.5 Run paper trading validation with position-arbitrage strategy
- [x] 13.6 Validate API endpoints with sample requests

## 14. Documentation

- [x] 14.1 Write README.md with setup and usage instructions
- [x] 14.2 Document strategy development guide
- [x] 14.3 Document configuration options
- [x] 14.4 Add inline code documentation for public APIs
