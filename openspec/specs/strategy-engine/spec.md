## ADDED Requirements

### Requirement: Strategy base class
The system SHALL provide a BaseStrategy abstract class that all trading strategies MUST inherit from, defining the standard interface for strategy implementation.

#### Scenario: Strategy interface definition
- **WHEN** developer creates a new strategy
- **THEN** strategy MUST implement the following methods:
  - `on_price_update(price_data) -> List[OrderSignal]`
  - `on_market_start(market_id, market_info)`
  - `on_market_end(winner) -> Tuple[cost, pnl, outcome]`
  - `get_status() -> dict`
  - `reset()`

#### Scenario: Strategy properties
- **WHEN** strategy is instantiated
- **THEN** strategy SHALL have the following properties:
  - `strategy_id`: unique identifier
  - `name`: human-readable name
  - `strategy_type`: category (e.g., "market_maker", "directional")
  - `position_size`: maximum capital allocation
  - `params`: configurable parameters dict

### Requirement: Strategy plugin registration
The system SHALL provide a registration mechanism to discover and load strategy implementations.

#### Scenario: Register strategy with decorator
- **WHEN** strategy class is decorated with `@register_strategy("strategy-name")`
- **THEN** strategy SHALL be registered in the global strategy registry
- **AND** strategy SHALL be loadable by name

#### Scenario: List registered strategies
- **WHEN** user requests list of available strategies
- **THEN** system SHALL return all registered strategy names and their metadata

#### Scenario: Instantiate strategy by name
- **WHEN** user requests to create strategy instance by name
- **THEN** system SHALL lookup strategy class in registry
- **AND** system SHALL instantiate with provided configuration

### Requirement: Trading mode support
The system SHALL support two trading modes: paper (simulated) and live (real orders), with identical strategy interface.

#### Scenario: Paper trading mode
- **WHEN** strategy is configured with mode="paper"
- **THEN** all orders SHALL be simulated locally
- **AND** order fills SHALL be based on market prices without actual execution
- **AND** no real funds SHALL be at risk

#### Scenario: Live trading mode
- **WHEN** strategy is configured with mode="live"
- **THEN** orders SHALL be submitted to Polymarket via py-clob-client
- **AND** order status SHALL be tracked from exchange responses
- **AND** real funds SHALL be used

#### Scenario: Mode switching
- **WHEN** user changes strategy trading mode
- **THEN** system SHALL preserve strategy state (positions, history)
- **AND** system SHALL switch order execution backend

### Requirement: Order management
The system SHALL manage order lifecycle including creation, submission, tracking, and cancellation.

#### Scenario: Create limit order
- **WHEN** strategy returns OrderSignal with price and size
- **THEN** system SHALL create order with:
  - unique order_id
  - side (buy/sell)
  - token_id (YES/NO)
  - price (0.01-0.99)
  - size (shares)
  - order_type (GTC, FOK, GTD)

#### Scenario: Order submission
- **WHEN** order is created
- **THEN** system SHALL submit to OrderManager (paper or live)
- **AND** system SHALL track order status (pending, filled, cancelled, rejected)

#### Scenario: Order cancellation
- **WHEN** strategy requests order cancellation
- **THEN** system SHALL cancel order via OrderManager
- **AND** system SHALL update order status to cancelled

#### Scenario: Order fill notification
- **WHEN** order is filled (fully or partially)
- **THEN** system SHALL notify strategy via callback
- **AND** system SHALL update position tracking

### Requirement: Position management
The system SHALL track positions for each strategy-market combination.

#### Scenario: Position tracking
- **WHEN** orders are filled
- **THEN** system SHALL update position with:
  - shares held (YES and NO separately)
  - average cost basis
  - unrealized PnL (based on current prices)
  - realized PnL (from closed positions)

#### Scenario: Position query
- **WHEN** strategy or API requests position info
- **THEN** system SHALL return current position state for the strategy

### Requirement: Strategy lifecycle management
The system SHALL manage strategy lifecycle states: initialized, running, paused, stopped.

#### Scenario: Strategy initialization
- **WHEN** strategy is created with configuration
- **THEN** system SHALL call strategy.reset()
- **AND** system SHALL set state to "initialized"

#### Scenario: Strategy start
- **WHEN** user starts a strategy
- **THEN** system SHALL subscribe strategy to market data events
- **AND** system SHALL set state to "running"
- **AND** strategy SHALL begin processing price updates

#### Scenario: Strategy pause
- **WHEN** user pauses a strategy
- **THEN** system SHALL stop processing new price updates
- **AND** system SHALL maintain existing positions and orders
- **AND** system SHALL set state to "paused"

#### Scenario: Strategy stop
- **WHEN** user stops a strategy
- **THEN** system SHALL cancel all pending orders
- **AND** system SHALL unsubscribe from market data
- **AND** system SHALL set state to "stopped"

### Requirement: Risk controls
The system SHALL enforce risk controls to limit potential losses.

#### Scenario: Position size limit
- **WHEN** order would exceed strategy's position_size limit
- **THEN** system SHALL reject or reduce the order
- **AND** system SHALL log the risk limit violation

#### Scenario: Order rate limiting
- **WHEN** strategy generates orders exceeding rate limit (e.g., 10/second)
- **THEN** system SHALL queue excess orders
- **AND** system SHALL warn about rate limiting
