## ADDED Requirements

### Requirement: Historical data replay
The system SHALL replay historical candlestick and trade data to simulate market conditions for strategy testing.

#### Scenario: Load historical data
- **WHEN** backtest is configured with date range and market_id
- **THEN** system SHALL load candlestick data from storage
- **AND** system SHALL prepare data for sequential replay

#### Scenario: Data replay execution
- **WHEN** backtest is started
- **THEN** system SHALL emit price_update events in chronological order
- **AND** system SHALL respect original timestamps for time-based logic
- **AND** strategy SHALL receive events identical to live trading

#### Scenario: Replay speed control
- **WHEN** backtest is configured with speed multiplier
- **THEN** system SHALL adjust replay speed (e.g., 10x, 100x, or instant)
- **AND** system SHALL maintain event ordering regardless of speed

### Requirement: Strategy code reuse
The system SHALL allow the same strategy code to run in both backtest and live modes without modification.

#### Scenario: Strategy compatibility
- **WHEN** strategy inherits from BaseStrategy
- **THEN** strategy SHALL work identically in backtest mode
- **AND** no code changes SHALL be required for backtesting

#### Scenario: Backtest order execution
- **WHEN** strategy generates orders during backtest
- **THEN** system SHALL simulate order execution using historical prices
- **AND** system SHALL apply configurable slippage model

### Requirement: Performance metrics calculation
The system SHALL calculate comprehensive performance metrics for backtest results.

#### Scenario: PnL calculation
- **WHEN** backtest completes
- **THEN** system SHALL calculate:
  - Total PnL (realized + unrealized)
  - PnL per market
  - PnL time series

#### Scenario: Win rate calculation
- **WHEN** backtest completes
- **THEN** system SHALL calculate:
  - Win rate (profitable trades / total trades)
  - Average win size
  - Average loss size
  - Win/loss ratio

#### Scenario: Risk metrics calculation
- **WHEN** backtest completes
- **THEN** system SHALL calculate:
  - Maximum drawdown (peak-to-trough decline)
  - Sharpe ratio (if applicable)
  - Sortino ratio
  - Calmar ratio

#### Scenario: Trade statistics
- **WHEN** backtest completes
- **THEN** system SHALL calculate:
  - Total number of trades
  - Average trade duration
  - Largest winning trade
  - Largest losing trade

### Requirement: Backtest configuration
The system SHALL support configurable backtest parameters.

#### Scenario: Date range configuration
- **WHEN** user specifies start_date and end_date
- **THEN** backtest SHALL only use data within that range

#### Scenario: Initial capital configuration
- **WHEN** user specifies initial_capital
- **THEN** backtest SHALL start with that capital amount
- **AND** position sizing SHALL respect capital limits

#### Scenario: Slippage configuration
- **WHEN** user specifies slippage model (fixed, percentage, or custom)
- **THEN** backtest SHALL apply slippage to simulated fills

#### Scenario: Commission configuration
- **WHEN** user specifies commission rate
- **THEN** backtest SHALL deduct commissions from PnL calculations

### Requirement: Backtest results export
The system SHALL export backtest results in structured format.

#### Scenario: Results summary export
- **WHEN** backtest completes
- **THEN** system SHALL generate results summary with all metrics
- **AND** results SHALL be exportable as JSON

#### Scenario: Trade log export
- **WHEN** backtest completes
- **THEN** system SHALL provide detailed trade log
- **AND** trade log SHALL include entry/exit prices, timestamps, PnL per trade

#### Scenario: Equity curve export
- **WHEN** backtest completes
- **THEN** system SHALL provide equity curve data (portfolio value over time)
- **AND** data SHALL be suitable for charting

### Requirement: Multi-market backtest
The system SHALL support backtesting strategies across multiple markets simultaneously.

#### Scenario: Multi-market configuration
- **WHEN** backtest is configured with multiple market_ids
- **THEN** system SHALL replay data from all markets
- **AND** events SHALL be merged in chronological order

#### Scenario: Cross-market strategy testing
- **WHEN** strategy operates on multiple markets
- **THEN** backtest SHALL provide synchronized data feeds
- **AND** portfolio-level metrics SHALL aggregate across all markets
