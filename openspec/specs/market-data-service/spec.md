## ADDED Requirements

### Requirement: WebSocket connection management
The system SHALL manage WebSocket connections to Polymarket CLOB API for multiple markets concurrently. Each market subscription SHALL maintain its own connection with automatic reconnection on failure.

#### Scenario: Subscribe to a market
- **WHEN** user requests subscription to a market by condition_id
- **THEN** system establishes WebSocket connection to Polymarket and begins receiving real-time data

#### Scenario: Automatic reconnection on disconnect
- **WHEN** WebSocket connection is lost unexpectedly
- **THEN** system SHALL attempt reconnection with exponential backoff (initial 1s, max 60s)
- **AND** system SHALL emit a connection_lost event to notify strategies

#### Scenario: Concurrent market subscriptions
- **WHEN** user subscribes to multiple markets (up to 50)
- **THEN** system SHALL maintain separate connections for each market
- **AND** data from each market SHALL be processed independently

### Requirement: Orderbook data processing
The system SHALL process orderbook snapshots and incremental updates from WebSocket feed, maintaining an accurate local orderbook state.

#### Scenario: Initial orderbook snapshot
- **WHEN** WebSocket connection is established
- **THEN** system SHALL receive and store the full orderbook snapshot
- **AND** system SHALL emit an orderbook_snapshot event with bids and asks

#### Scenario: Incremental orderbook update
- **WHEN** orderbook change message is received
- **THEN** system SHALL update local orderbook state
- **AND** system SHALL emit an orderbook_update event with the changes

### Requirement: Trade data processing
The system SHALL process trade messages from WebSocket feed, emitting trade events for each executed trade.

#### Scenario: Trade message received
- **WHEN** trade message is received from WebSocket
- **THEN** system SHALL parse trade data (price, size, timestamp, side)
- **AND** system SHALL emit a trade event to all subscribed listeners

### Requirement: Candlestick aggregation
The system SHALL aggregate trade data into candlestick (OHLCV) format for multiple time intervals: 1m, 5m, 15m, 1h, 4h, 1d.

#### Scenario: Candlestick creation from trades
- **WHEN** trades are received within a time interval
- **THEN** system SHALL aggregate into OHLCV format (open, high, low, close, volume)
- **AND** system SHALL track both YES and NO token prices separately

#### Scenario: Candlestick interval completion
- **WHEN** a time interval ends (e.g., minute boundary for 1m candles)
- **THEN** system SHALL finalize the current candlestick
- **AND** system SHALL persist the completed candlestick to storage
- **AND** system SHALL emit a candlestick_complete event

#### Scenario: Multiple interval support
- **WHEN** system is configured with intervals [1m, 5m, 15m, 1h]
- **THEN** system SHALL maintain separate candlestick aggregators for each interval
- **AND** all intervals SHALL be updated from the same trade stream

### Requirement: Price data events
The system SHALL emit standardized price update events that strategies can subscribe to.

#### Scenario: Price update event
- **WHEN** orderbook or trade data changes the best bid/ask prices
- **THEN** system SHALL emit a price_update event with:
  - market_id
  - up_price (YES token best price)
  - down_price (NO token best price)
  - timestamp
  - spread

### Requirement: Market lifecycle events
The system SHALL track and emit market lifecycle events (market start, market end, settlement).

#### Scenario: Market start detection
- **WHEN** a subscribed market becomes active
- **THEN** system SHALL emit a market_start event with market metadata

#### Scenario: Market end detection
- **WHEN** a subscribed market closes for trading
- **THEN** system SHALL emit a market_end event
- **AND** system SHALL include the winning outcome if available

#### Scenario: Market settlement
- **WHEN** market settlement result is available
- **THEN** system SHALL emit a market_settled event with winner (up/down)
