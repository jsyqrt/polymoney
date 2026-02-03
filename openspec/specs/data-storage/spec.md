## ADDED Requirements

### Requirement: Candlestick data storage
The system SHALL persist candlestick (OHLCV) data for historical analysis and backtesting.

#### Scenario: Store candlestick
- **WHEN** candlestick interval completes
- **THEN** system SHALL persist candlestick with:
  - market_id
  - interval (1m, 5m, 15m, 1h, 4h, 1d)
  - timestamp (interval start time)
  - open, high, low, close prices
  - volume
  - token_type (YES/NO)

#### Scenario: Query candlesticks by time range
- **WHEN** query is made with market_id, interval, start_time, end_time
- **THEN** system SHALL return candlesticks within the time range
- **AND** candlesticks SHALL be ordered by timestamp ascending

#### Scenario: Query latest candlesticks
- **WHEN** query is made with market_id, interval, limit
- **THEN** system SHALL return the N most recent candlesticks

#### Scenario: Candlestick data retention
- **WHEN** candlestick data exceeds retention period (configurable, default 30 days)
- **THEN** system MAY archive or delete old data based on configuration

### Requirement: Trade record storage
The system SHALL persist trade execution records for audit and analysis.

#### Scenario: Store trade record
- **WHEN** order is filled (fully or partially)
- **THEN** system SHALL persist trade record with:
  - trade_id
  - order_id
  - strategy_id
  - market_id
  - side (buy/sell)
  - token_type (YES/NO)
  - price
  - size
  - timestamp
  - mode (paper/live)

#### Scenario: Query trades by strategy
- **WHEN** query is made with strategy_id and optional time range
- **THEN** system SHALL return trades for that strategy

#### Scenario: Query trades by market
- **WHEN** query is made with market_id and optional time range
- **THEN** system SHALL return trades for that market

### Requirement: Order record storage
The system SHALL persist order records including status history.

#### Scenario: Store order
- **WHEN** order is created
- **THEN** system SHALL persist order with:
  - order_id
  - strategy_id
  - market_id
  - side, token_type, price, size
  - order_type (GTC, FOK, GTD)
  - status (pending, filled, cancelled, rejected)
  - created_at, updated_at
  - mode (paper/live)

#### Scenario: Update order status
- **WHEN** order status changes
- **THEN** system SHALL update order record
- **AND** system SHALL record status change timestamp

#### Scenario: Query orders with filters
- **WHEN** query is made with filters (strategy_id, market_id, status, time_range)
- **THEN** system SHALL return orders matching all filters

### Requirement: Position snapshot storage
The system SHALL persist position snapshots for tracking and recovery.

#### Scenario: Store position snapshot
- **WHEN** position changes significantly or on scheduled interval
- **THEN** system SHALL persist snapshot with:
  - strategy_id
  - market_id
  - timestamp
  - up_shares, up_cost, up_avg_price
  - down_shares, down_cost, down_avg_price
  - unrealized_pnl, realized_pnl

#### Scenario: Query position history
- **WHEN** query is made with strategy_id and time range
- **THEN** system SHALL return position snapshots over time

#### Scenario: Restore position from snapshot
- **WHEN** strategy is restarted after crash
- **THEN** system SHALL load latest position snapshot
- **AND** strategy SHALL resume with restored position state

### Requirement: Performance metrics storage
The system SHALL persist performance metrics for analysis.

#### Scenario: Store daily metrics
- **WHEN** trading day ends
- **THEN** system SHALL persist metrics with:
  - strategy_id
  - date
  - pnl (daily)
  - trade_count
  - win_count, loss_count
  - max_drawdown

#### Scenario: Query metrics time series
- **WHEN** query is made with strategy_id and date range
- **THEN** system SHALL return daily metrics for analysis

### Requirement: Configuration storage
The system SHALL persist configuration for strategies and system settings.

#### Scenario: Store strategy configuration
- **WHEN** strategy is created or configuration is updated
- **THEN** system SHALL persist configuration with:
  - strategy_id
  - strategy_type
  - parameters (JSON)
  - trading_mode
  - updated_at

#### Scenario: Load strategy configuration
- **WHEN** system starts or strategy is requested
- **THEN** system SHALL load persisted configuration

#### Scenario: Store system configuration
- **WHEN** system configuration is updated
- **THEN** system SHALL persist configuration
- **AND** sensitive values SHALL be encrypted

### Requirement: Database schema management
The system SHALL manage database schema with migrations.

#### Scenario: Initial schema creation
- **WHEN** system starts with empty database
- **THEN** system SHALL create all required tables

#### Scenario: Schema migration
- **WHEN** system starts with outdated schema
- **THEN** system SHALL apply pending migrations
- **AND** system SHALL preserve existing data

### Requirement: Data export
The system SHALL support data export for external analysis.

#### Scenario: Export candlesticks to CSV
- **WHEN** export is requested with market_id, interval, date range
- **THEN** system SHALL generate CSV file with candlestick data

#### Scenario: Export trades to CSV
- **WHEN** export is requested with strategy_id and date range
- **THEN** system SHALL generate CSV file with trade records

#### Scenario: Export metrics to JSON
- **WHEN** export is requested with strategy_id and date range
- **THEN** system SHALL generate JSON file with performance metrics
