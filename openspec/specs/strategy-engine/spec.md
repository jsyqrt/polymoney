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

#### Scenario: ECR calculation
- **WHEN** strategy has positions in both UP and DOWN tokens
- **THEN** system SHALL calculate ECR as total_cost / min(up_shares, down_shares)
- **AND** system SHALL project ECR after each potential order

#### Scenario: Pre-order ECR prediction
- **WHEN** strategy is about to place an order
- **THEN** system SHALL calculate projected_ecr assuming the order fills
- **AND** projected_ecr SHALL include the hypothetical order's cost and shares

#### Scenario: ECR tolerance check
- **WHEN** projected ECR exceeds current ECR by more than tolerance (default 1%)
- **THEN** system SHALL reject the order
- **AND** system SHALL log rejection with current_ecr, projected_ecr, and tolerance

#### Scenario: ECR absolute threshold check
- **WHEN** projected ECR exceeds ecr_threshold (default 105%)
- **THEN** system SHALL reject the order regardless of tolerance
- **AND** system SHALL log ECR violation with current value and threshold
- **AND** strategy SHALL enter "risk_limited" state

#### Scenario: Allow orders improving ECR
- **WHEN** projected ECR is less than or equal to current ECR plus tolerance
- **AND** projected ECR is below ecr_threshold
- **THEN** system SHALL allow the order to proceed

#### Scenario: Handle initial orders (no position yet)
- **WHEN** strategy has no positions (ECR is undefined/infinite)
- **THEN** system SHALL allow orders that establish initial position
- **AND** ECR prediction SHALL use 0.0 for the empty side

### Requirement: Market order rebalancing
The system SHALL support market orders to rebalance severely imbalanced positions.

#### Scenario: Position imbalance detection
- **WHEN** balance ratio (min/max shares) falls below threshold (default 0.70)
- **THEN** system SHALL flag position as "imbalanced"
- **AND** system SHALL identify the underweight side (UP or DOWN)

#### Scenario: Trigger market order for rebalancing
- **WHEN** position is imbalanced AND rebalancing is enabled
- **THEN** system SHALL generate market order for underweight side
- **AND** market order size SHALL be calculated to restore balance to 0.85 ratio
- **AND** system SHALL log rebalancing action with before/after projections

#### Scenario: Market order execution
- **WHEN** market order is generated
- **THEN** system SHALL set order_type to "FOK" (fill or kill)
- **AND** system SHALL use best available price (best ask for buys)
- **AND** system SHALL track market order separately from limit orders

#### Scenario: Rebalancing cooldown
- **WHEN** market rebalancing order is executed
- **THEN** system SHALL enter cooldown period (default 30 seconds)
- **AND** system SHALL not trigger another rebalancing during cooldown
- **AND** limit orders SHALL continue normally during cooldown

### Requirement: Dynamic order sizing for position rebalancing
The system SHALL dynamically increase order size for the lagging side to accelerate position rebalancing.

#### Scenario: Calculate dynamic order size for lagging side
- **WHEN** position is imbalanced (gap between sides > 10%)
- **AND** order is for the lagging side
- **THEN** order_cost SHALL be scaled up to fill the gap
- **AND** scale factor SHALL be min(5.0, gap_shares * limit_price / batch_size)
- **AND** order_cost SHALL not exceed available budget

#### Scenario: Standard order size when balanced
- **WHEN** position is balanced (gap <= 10%)
- **THEN** order_cost SHALL be standard batch_size

#### Scenario: Standard order size for leading side
- **WHEN** order is for the leading side (has more shares)
- **THEN** order_cost SHALL be standard batch_size
- **AND** order SHALL be subject to ECR and balance filters

### Requirement: Aggressive market order rebalancing
The system SHALL trigger market orders for severe position imbalance.

#### Scenario: Trigger market order for severe imbalance
- **WHEN** balance_ratio falls below severe_imbalance_threshold (default 0.50)
- **AND** rebalancing is enabled
- **THEN** system SHALL generate market order for underweight side
- **AND** market order SHALL use FOK order type
- **AND** market order size SHALL target 0.85 balance ratio

#### Scenario: Market order size limit
- **WHEN** calculating market order size for rebalancing
- **THEN** order size SHALL be capped at 10% of max position value
- **AND** this prevents excessive slippage from large market orders

### Requirement: Enhanced rebalancing configuration
The system SHALL provide additional configuration for rebalancing behavior.

#### Scenario: Configure rebalancing parameters
- **WHEN** strategy is initialized with rebalancing parameters
- **THEN** system SHALL accept the following parameters:
  - `enable_rebalancing`: boolean (default True)
  - `rebalancing_cooldown`: seconds between market orders (default 30)
  - `severe_imbalance_threshold`: balance_ratio below which to use market orders (default 0.50)
  - `market_order_size_cap`: max market order as fraction of position (default 0.10)

### Requirement: Strategy state reporting
The system SHALL provide detailed state information for analysis.

#### Scenario: Report risk metrics
- **WHEN** strategy status is queried
- **THEN** response SHALL include:
  - current_ecr: current effective cost rate
  - projected_ecr: ECR after pending orders fill
  - balance_ratio: min/max position ratio
  - risk_state: "normal", "warning" (ECR > 100%), or "limited" (ECR > threshold)

#### Scenario: Report rebalancing history
- **WHEN** strategy status is queried AND rebalancing has occurred
- **THEN** response SHALL include rebalancing_events list with:
  - timestamp of each rebalancing
  - order details (side, size, price)
  - position before and after
