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

### Requirement: Strategy execution on markets
The system SHALL execute the position arbitrage strategy on each monitored market.

#### Scenario: Execute strategy on new market
- **WHEN** a new 15-minute market is detected
- **THEN** system SHALL instantiate a new strategy instance with configured parameters
- **AND** system SHALL begin generating simulated orders based on market data

#### Scenario: Process strategy signals
- **WHEN** strategy generates an order signal
- **THEN** system SHALL simulate order fill based on current market conditions
- **AND** system SHALL update position tracking with simulated fill

#### Scenario: Handle strategy errors
- **WHEN** strategy execution throws an exception
- **THEN** system SHALL log the error with market context
- **AND** system SHALL continue processing other markets

### Requirement: Periodic metrics output
The system SHALL periodically output performance metrics to files.

#### Scenario: Output metrics at configurable interval
- **WHEN** simulation is running and output interval (default 5 minutes) elapses
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
