## ADDED Requirements

### Requirement: Strategy query endpoints
The system SHALL provide REST API endpoints to query strategy information and status.

#### Scenario: List all strategies
- **WHEN** GET /strategies is called
- **THEN** system SHALL return list of all strategy instances with:
  - strategy_id
  - name
  - type
  - state (initialized, running, paused, stopped)
  - position summary

#### Scenario: Get strategy details
- **WHEN** GET /strategies/{strategy_id} is called
- **THEN** system SHALL return detailed strategy information:
  - configuration parameters
  - current positions (up/down shares, costs)
  - pending orders
  - PnL metrics (realized, unrealized, total)
  - performance statistics

#### Scenario: Strategy not found
- **WHEN** GET /strategies/{strategy_id} is called with invalid id
- **THEN** system SHALL return 404 Not Found

### Requirement: Strategy control endpoints
The system SHALL provide REST API endpoints to control strategy execution.

#### Scenario: Start strategy
- **WHEN** POST /strategies/{strategy_id}/start is called
- **THEN** system SHALL start the strategy
- **AND** system SHALL return updated strategy state

#### Scenario: Stop strategy
- **WHEN** POST /strategies/{strategy_id}/stop is called
- **THEN** system SHALL stop the strategy
- **AND** system SHALL cancel pending orders
- **AND** system SHALL return updated strategy state

#### Scenario: Pause strategy
- **WHEN** POST /strategies/{strategy_id}/pause is called
- **THEN** system SHALL pause the strategy
- **AND** system SHALL maintain existing positions
- **AND** system SHALL return updated strategy state

#### Scenario: Update strategy configuration
- **WHEN** PUT /strategies/{strategy_id}/config is called with new params
- **THEN** system SHALL validate configuration
- **AND** system SHALL update strategy parameters
- **AND** system SHALL return updated configuration

### Requirement: Market subscription endpoints
The system SHALL provide REST API endpoints to manage market subscriptions.

#### Scenario: List subscribed markets
- **WHEN** GET /markets is called
- **THEN** system SHALL return list of subscribed markets with:
  - market_id (condition_id)
  - title/question
  - current prices (up/down)
  - connection status

#### Scenario: Subscribe to market
- **WHEN** POST /markets/{market_id}/subscribe is called
- **THEN** system SHALL initiate WebSocket subscription
- **AND** system SHALL return subscription status

#### Scenario: Unsubscribe from market
- **WHEN** DELETE /markets/{market_id}/subscribe is called
- **THEN** system SHALL close WebSocket connection
- **AND** system SHALL return confirmation

### Requirement: Order query endpoints
The system SHALL provide REST API endpoints to query order information.

#### Scenario: List orders
- **WHEN** GET /orders is called with optional filters (strategy_id, market_id, status)
- **THEN** system SHALL return list of orders matching filters
- **AND** orders SHALL include: order_id, side, price, size, status, timestamps

#### Scenario: Get order details
- **WHEN** GET /orders/{order_id} is called
- **THEN** system SHALL return detailed order information
- **AND** system SHALL include fill history if partially/fully filled

### Requirement: Metrics and performance endpoints
The system SHALL provide REST API endpoints for performance metrics.

#### Scenario: Get overall metrics
- **WHEN** GET /metrics is called
- **THEN** system SHALL return aggregated metrics:
  - total PnL across all strategies
  - number of active strategies
  - number of subscribed markets
  - system uptime

#### Scenario: Get strategy metrics
- **WHEN** GET /metrics/strategies/{strategy_id} is called
- **THEN** system SHALL return strategy-specific metrics:
  - PnL history (time series)
  - win rate
  - trade count
  - average trade size

#### Scenario: Get market metrics
- **WHEN** GET /metrics/markets/{market_id} is called
- **THEN** system SHALL return market-specific metrics:
  - price history
  - volume
  - volatility

### Requirement: System administration endpoints
The system SHALL provide REST API endpoints for system administration.

#### Scenario: Health check
- **WHEN** GET /health is called
- **THEN** system SHALL return health status:
  - status (healthy, degraded, unhealthy)
  - component statuses (database, websocket, api)
  - timestamp

#### Scenario: Get logs
- **WHEN** GET /logs is called with optional filters (level, component, time_range)
- **THEN** system SHALL return recent log entries matching filters

#### Scenario: Get configuration
- **WHEN** GET /config is called
- **THEN** system SHALL return current system configuration
- **AND** sensitive values (API keys) SHALL be masked

#### Scenario: Update configuration
- **WHEN** PUT /config is called with new configuration
- **THEN** system SHALL validate configuration
- **AND** system SHALL apply configuration changes
- **AND** system SHALL return updated configuration

### Requirement: API authentication
The system SHALL support optional API authentication for security.

#### Scenario: Authenticated request
- **WHEN** request includes valid API key in header
- **THEN** system SHALL process the request

#### Scenario: Unauthenticated request when auth required
- **WHEN** request lacks API key and auth is required
- **THEN** system SHALL return 401 Unauthorized

#### Scenario: Invalid API key
- **WHEN** request includes invalid API key
- **THEN** system SHALL return 403 Forbidden

### Requirement: API documentation
The system SHALL provide auto-generated API documentation.

#### Scenario: OpenAPI spec
- **WHEN** GET /openapi.json is called
- **THEN** system SHALL return OpenAPI 3.0 specification

#### Scenario: Swagger UI
- **WHEN** GET /docs is called
- **THEN** system SHALL serve Swagger UI for interactive API exploration
