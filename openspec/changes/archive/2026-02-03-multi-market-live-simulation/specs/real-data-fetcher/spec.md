## MODIFIED Requirements

### Requirement: Market discovery by slug pattern
The system SHALL discover 15-minute up/down markets for multiple cryptocurrencies using URL slug patterns.

#### Scenario: Find BTC market by event slug
- **WHEN** user provides a slug pattern like "btc-updown-15m"
- **THEN** system SHALL search Gamma API for matching events
- **AND** system SHALL return list of markets with condition IDs, token IDs, and metadata

#### Scenario: Find ETH market by event slug
- **WHEN** user provides a slug pattern like "eth-updown-15m"
- **THEN** system SHALL search Gamma API for matching ETH events
- **AND** system SHALL return markets in the same format as BTC markets

#### Scenario: Find SOL market by event slug
- **WHEN** user provides a slug pattern like "sol-updown-15m"
- **THEN** system SHALL search Gamma API for matching SOL events
- **AND** system SHALL return markets in the same format as BTC markets

#### Scenario: List active 15-minute markets for multiple coins
- **WHEN** user requests active 15-minute markets with coins=["btc", "eth", "sol"]
- **THEN** system SHALL query Gamma API for all specified coins
- **AND** system SHALL return combined list sorted by end time (soonest first)

#### Scenario: Get market by condition ID
- **WHEN** user provides a specific condition ID
- **THEN** system SHALL fetch market details from CLOB API
- **AND** system SHALL return market info including token IDs, resolution source, and rules

## ADDED Requirements

### Requirement: Multi-coin market discovery
The system SHALL support discovering markets for ETH and SOL in addition to BTC.

#### Scenario: Discover ETH 15-minute markets
- **WHEN** user calls `find_15min_markets(coin="eth")`
- **THEN** system SHALL search for "eth-updown-15m" pattern in Gamma API
- **AND** system SHALL return markets with UP/DOWN token pairs

#### Scenario: Discover SOL 15-minute markets
- **WHEN** user calls `find_15min_markets(coin="sol")`
- **THEN** system SHALL search for "sol-updown-15m" pattern in Gamma API
- **AND** system SHALL return markets with UP/DOWN token pairs

#### Scenario: Discover markets for all supported coins
- **WHEN** user calls `find_15min_markets(coins=["btc", "eth", "sol"])`
- **THEN** system SHALL query all coins in parallel
- **AND** system SHALL return combined results tagged with coin type

### Requirement: Real-time price streaming
The system SHALL support real-time price updates for active markets.

#### Scenario: Subscribe to market price updates
- **WHEN** user subscribes to price updates for a market token ID
- **THEN** system SHALL poll orderbook at configurable interval (default 5 seconds)
- **AND** system SHALL emit price update events with mid price and spread

#### Scenario: Handle price update errors
- **WHEN** orderbook fetch fails during price streaming
- **THEN** system SHALL retry with exponential backoff
- **AND** system SHALL emit stale price warning after 30 seconds without update

#### Scenario: Unsubscribe from price updates
- **WHEN** user unsubscribes from a market
- **THEN** system SHALL stop polling for that market
- **AND** system SHALL clean up resources associated with the subscription

### Requirement: Market lifecycle events
The system SHALL emit events for market state changes.

#### Scenario: Market becomes active
- **WHEN** a new 15-minute market is created and becomes tradeable
- **THEN** system SHALL emit market_active event with market details
- **AND** event SHALL include estimated settlement time

#### Scenario: Market approaching settlement
- **WHEN** market is within 2 minutes of settlement
- **THEN** system SHALL emit market_settling event
- **AND** system SHALL continue providing price updates until settlement

#### Scenario: Market settles
- **WHEN** market settlement is confirmed by API
- **THEN** system SHALL emit market_settled event with winning outcome
- **AND** system SHALL stop price streaming for that market
