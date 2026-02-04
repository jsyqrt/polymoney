## ADDED Requirements

### Requirement: Multi-market real-time monitoring
The system SHALL continuously monitor multiple 15-minute prediction markets across different cryptocurrencies.

#### Scenario: Start monitoring multiple coins
- **WHEN** user starts the simulation with markets ["btc", "eth", "sol"]
- **THEN** system SHALL discover active 15-minute markets for each coin
- **AND** system SHALL begin monitoring all discovered markets concurrently

#### Scenario: Discover new markets during runtime
- **WHEN** a new 15-minute market becomes active during simulation
- **THEN** system SHALL detect the market within 5 minutes
- **AND** system SHALL automatically add it to the monitoring pool

#### Scenario: Handle market settlement
- **WHEN** a monitored market reaches settlement time
- **THEN** system SHALL record the final result
- **AND** system SHALL remove the market from active monitoring

### Requirement: Configurable runtime duration
The system SHALL support configurable simulation duration with multiple units.

#### Scenario: Run for specified hours
- **WHEN** user starts simulation with `--duration 10h`
- **THEN** system SHALL run for exactly 10 hours
- **AND** system SHALL gracefully shutdown after duration expires

#### Scenario: Run for specified days
- **WHEN** user starts simulation with `--duration 3d`
- **THEN** system SHALL run for exactly 3 days (72 hours)
- **AND** system SHALL survive system sleep/wake cycles during runtime

#### Scenario: Run indefinitely
- **WHEN** user starts simulation with `--duration 0` or no duration flag
- **THEN** system SHALL run indefinitely until manual stop
- **AND** system SHALL respond to SIGINT/SIGTERM for graceful shutdown

### Requirement: Market validity filtering
The system SHALL validate markets before starting simulation to ensure sufficient trading opportunity.

#### Scenario: Skip market near settlement
- **WHEN** a market is discovered with less than 5 minutes until settlement
- **THEN** system SHALL skip this market and log the reason
- **AND** system SHALL not create a simulation instance for this market

#### Scenario: Skip market with decided outcome
- **WHEN** a market is discovered with one outcome priced below 0.05 and price_sum >= 0.98
- **THEN** system SHALL skip this market as outcome is effectively decided
- **AND** system SHALL log the market slug and price condition

#### Scenario: Accept valid market
- **WHEN** a market is discovered with >= 5 minutes until settlement and balanced prices
- **THEN** system SHALL start simulation for this market
- **AND** system SHALL call strategy's on_market_start() method

#### Scenario: Configurable minimum trading time
- **WHEN** user specifies `--min-trading-time 180` (3 minutes)
- **THEN** system SHALL use 180 seconds as the threshold for market validity
- **AND** system SHALL accept markets with >= 180 seconds until settlement

### Requirement: Strategy execution on markets
The system SHALL execute the position arbitrage strategy on each monitored market.

#### Scenario: Execute strategy on new market
- **WHEN** a new 15-minute market is detected and passes validity check
- **THEN** system SHALL instantiate a new strategy instance with configured parameters
- **AND** system SHALL call on_market_start() to initialize strategy
- **AND** system SHALL begin generating simulated orders based on market data

#### Scenario: Process strategy signals
- **WHEN** strategy generates an order signal
- **THEN** system SHALL simulate order fill based on current market conditions
- **AND** system SHALL update position tracking with simulated fill
- **AND** system SHALL increment order counters in market details

#### Scenario: Handle strategy errors
- **WHEN** strategy execution throws an exception
- **THEN** system SHALL log the error with market context
- **AND** system SHALL continue processing other markets

### Requirement: Real-time market status tracking
The system SHALL track and expose real-time status for each active market.

#### Scenario: Status includes market details
- **WHEN** status.json is written during simulation
- **THEN** system SHALL include a `market_details` object
- **AND** each active market SHALL have an entry with position, cost, ECR, and price data

#### Scenario: Market detail fields
- **WHEN** a market entry is written to market_details
- **THEN** entry SHALL include: coin, up_shares, down_shares, up_cost, down_cost, ecr, balance_ratio, orders_submitted, orders_filled, current_up_price, current_down_price, started_at

#### Scenario: Update on order activity
- **WHEN** an order is generated or filled in simulation
- **THEN** system SHALL update the market details in next status write
- **AND** order counts SHALL reflect cumulative totals

### Requirement: Strategy lifecycle management
The system SHALL properly initialize and finalize strategy lifecycle.

#### Scenario: Initialize strategy on market start
- **WHEN** a new market simulation is started
- **THEN** system SHALL call strategy.on_market_start() with market_id and market_info
- **AND** strategy.market_start_time SHALL be set to current time

#### Scenario: Finalize strategy on market end
- **WHEN** a market reaches settlement
- **THEN** system SHALL call strategy.on_market_end() with winner information
- **AND** system SHALL record final position and PnL

### Requirement: Periodic metrics output
The system SHALL periodically output performance metrics to files.

#### Scenario: Output metrics at configurable interval
- **WHEN** simulation is running and output interval (default 10 seconds for test, 300 seconds for production) elapses
- **THEN** system SHALL append current metrics to the metrics file
- **AND** metrics SHALL include timestamp, cumulative PnL, ROI, Sharpe ratio

#### Scenario: Metrics file format
- **WHEN** system writes metrics to file
- **THEN** system SHALL use JSON Lines format (one JSON object per line)
- **AND** each line SHALL be parseable independently

#### Scenario: Handle disk write failures
- **WHEN** metrics write fails due to disk error
- **THEN** system SHALL log error and retry after 30 seconds
- **AND** system SHALL NOT stop simulation due to write failures

### Requirement: Quick test mode
The system SHALL support a quick test mode for rapid validation.

#### Scenario: Enable quick test mode
- **WHEN** user starts simulation with `--quick-test`
- **THEN** system SHALL set metrics-interval to 10 seconds
- **AND** system SHALL set price-interval to 2 seconds
- **AND** system SHALL set scan-interval to 30 seconds

#### Scenario: Quick test overrides
- **WHEN** user specifies `--quick-test` along with explicit interval values
- **THEN** explicit values SHALL override quick-test defaults

### Requirement: State persistence and recovery
The system SHALL persist state for crash recovery and status queries.

#### Scenario: Write state checkpoint
- **WHEN** metrics are updated
- **THEN** system SHALL atomically write current state to status.json
- **AND** state SHALL include start time, config, cumulative stats, and last update time

#### Scenario: Resume from checkpoint
- **WHEN** user starts simulation with `--resume` flag
- **THEN** system SHALL load previous state from status.json
- **AND** system SHALL continue accumulating metrics from previous run

#### Scenario: Start fresh with existing state
- **WHEN** user starts simulation without `--resume` flag and status.json exists
- **THEN** system SHALL archive previous state to timestamped file
- **AND** system SHALL start fresh with zero metrics

### Requirement: Graceful shutdown
The system SHALL support graceful shutdown with state preservation.

#### Scenario: SIGINT signal received
- **WHEN** user presses Ctrl+C or sends SIGINT
- **THEN** system SHALL complete current market processing cycle
- **AND** system SHALL write final state before exit

#### Scenario: SIGTERM signal received
- **WHEN** system receives SIGTERM signal
- **THEN** system SHALL initiate graceful shutdown within 30 seconds
- **AND** system SHALL forcefully terminate if shutdown takes longer
