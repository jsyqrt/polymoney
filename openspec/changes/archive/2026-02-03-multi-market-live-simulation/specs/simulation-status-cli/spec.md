## ADDED Requirements

### Requirement: Status summary display
The system SHALL provide a CLI command to display simulation status summary.

#### Scenario: Display running simulation status
- **WHEN** user runs `sim-status` and simulation is running
- **THEN** system SHALL display runtime duration, markets processed, current PnL
- **AND** system SHALL display Sharpe ratio and win rate

#### Scenario: Display stopped simulation status
- **WHEN** user runs `sim-status` and simulation is not running
- **THEN** system SHALL display "Simulation not running"
- **AND** system SHALL show last run time and final metrics if available

#### Scenario: Handle missing status file
- **WHEN** user runs `sim-status` and no status.json exists
- **THEN** system SHALL display "No simulation data found"
- **AND** system SHALL exit with code 0 (not an error)

### Requirement: Historical performance query
The system SHALL provide access to historical performance data.

#### Scenario: Display historical metrics
- **WHEN** user runs `sim-status --history`
- **THEN** system SHALL display time-series of key metrics
- **AND** system SHALL show PnL trend, cumulative returns, and per-market breakdown

#### Scenario: Filter history by time range
- **WHEN** user runs `sim-status --history --since 2h`
- **THEN** system SHALL only display metrics from the last 2 hours
- **AND** system SHALL recalculate aggregate stats for the filtered period

#### Scenario: Export history to file
- **WHEN** user runs `sim-status --history --export results.csv`
- **THEN** system SHALL export historical data in CSV format
- **AND** CSV SHALL include timestamp, market_id, pnl, roi, ecr columns

### Requirement: JSON output for automation
The system SHALL support JSON output for AI and script consumption.

#### Scenario: JSON status output
- **WHEN** user runs `sim-status --json`
- **THEN** system SHALL output status as valid JSON to stdout
- **AND** output SHALL include all fields from status.json plus derived metrics

#### Scenario: JSON history output
- **WHEN** user runs `sim-status --history --json`
- **THEN** system SHALL output historical data as JSON array
- **AND** each entry SHALL include all available metrics

#### Scenario: Machine-parseable exit codes
- **WHEN** command completes
- **THEN** system SHALL return exit code 0 for success
- **AND** system SHALL return non-zero codes for errors with JSON error details

### Requirement: Live tail mode
The system SHALL support real-time monitoring of metrics updates.

#### Scenario: Tail live metrics
- **WHEN** user runs `sim-status --tail`
- **THEN** system SHALL continuously display new metrics as they are written
- **AND** system SHALL update display every time metrics file changes

#### Scenario: Tail with filter
- **WHEN** user runs `sim-status --tail --market btc`
- **THEN** system SHALL only display metrics related to BTC markets
- **AND** system SHALL still show aggregate stats across all markets

### Requirement: Market-specific queries
The system SHALL support querying individual market performance.

#### Scenario: Query specific market
- **WHEN** user runs `sim-status --market btc-updown-15m-1770106500`
- **THEN** system SHALL display detailed performance for that specific market
- **AND** system SHALL include entry price, positions, fill rate, and final PnL

#### Scenario: List all processed markets
- **WHEN** user runs `sim-status --list-markets`
- **THEN** system SHALL display all markets processed in current/last simulation
- **AND** system SHALL sort by settlement time (most recent first)
