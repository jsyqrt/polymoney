## ADDED Requirements

### Requirement: Market discovery by slug pattern
The system SHALL discover BTC 15-minute up/down markets using URL slug patterns.

#### Scenario: Find market by event slug
- **WHEN** user provides a slug pattern like "btc-updown-15m"
- **THEN** system SHALL search Gamma API for matching events
- **AND** system SHALL return list of markets with condition IDs, token IDs, and metadata

#### Scenario: List active BTC 15-minute markets
- **WHEN** user requests active BTC 15-minute markets
- **THEN** system SHALL query Gamma API with filters for active status and BTC keyword
- **AND** system SHALL return markets sorted by end time (soonest first)

#### Scenario: Get market by condition ID
- **WHEN** user provides a specific condition ID
- **THEN** system SHALL fetch market details from CLOB API
- **AND** system SHALL return market info including token IDs, resolution source, and rules

### Requirement: Historical trade data retrieval
The system SHALL fetch historical trade data for backtesting completed markets.

#### Scenario: Fetch trade history for market
- **WHEN** user requests trade history for a market condition ID
- **THEN** system SHALL fetch all trades from market start to settlement
- **AND** system SHALL return trades with timestamp, price, size, and side

#### Scenario: Convert trades to candlesticks
- **WHEN** user requests candlestick data from trade history
- **THEN** system SHALL aggregate trades into OHLCV candlesticks
- **AND** system SHALL support configurable intervals (1m, 5m, 15m)

#### Scenario: Handle markets with sparse trading
- **WHEN** market has time periods with no trades
- **THEN** system SHALL fill gaps with previous close price
- **AND** system SHALL mark synthetic candles with zero volume

### Requirement: Orderbook snapshot retrieval
The system SHALL fetch orderbook snapshots for price discovery.

#### Scenario: Get current orderbook
- **WHEN** user requests orderbook for a token ID
- **THEN** system SHALL fetch bid/ask levels from CLOB API
- **AND** system SHALL return sorted bids (highest first) and asks (lowest first)

#### Scenario: Calculate mid price
- **WHEN** orderbook is retrieved
- **THEN** system SHALL calculate mid price as (best_bid + best_ask) / 2
- **AND** system SHALL handle empty orderbooks with fallback to last trade price

### Requirement: Rate limiting and caching
The system SHALL respect API rate limits and cache responses.

#### Scenario: Rate limit exceeded
- **WHEN** API returns rate limit error (429)
- **THEN** system SHALL wait with exponential backoff (1s, 2s, 4s, max 60s)
- **AND** system SHALL retry the request up to 5 times

#### Scenario: Cache market metadata
- **WHEN** market metadata is fetched
- **THEN** system SHALL cache response for 5 minutes
- **AND** system SHALL return cached data on subsequent requests within TTL

#### Scenario: Cache trade history
- **WHEN** trade history for a completed market is fetched
- **THEN** system SHALL cache indefinitely (immutable data)
- **AND** system SHALL store cache in local file system for persistence

### Requirement: Error handling and fallbacks
The system SHALL handle API errors gracefully with fallback mechanisms.

#### Scenario: Primary API unavailable
- **WHEN** CLOB API request fails with connection error
- **THEN** system SHALL attempt Gamma API as fallback
- **AND** system SHALL log the failover for debugging

#### Scenario: Market not found
- **WHEN** requested market does not exist
- **THEN** system SHALL return clear error with suggestions
- **AND** system SHALL suggest checking market slug or condition ID format

#### Scenario: Partial data available
- **WHEN** trade history is incomplete but market is settled
- **THEN** system SHALL return available data with warning
- **AND** system SHALL include coverage percentage in response
