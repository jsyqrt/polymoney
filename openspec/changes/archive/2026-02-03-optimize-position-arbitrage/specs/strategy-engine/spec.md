## MODIFIED Requirements

### Requirement: ECR stop-loss trigger
The system SHALL implement pre-order ECR prediction to reject orders that would worsen effective cost rate beyond acceptable tolerance.

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

## ADDED Requirements

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

#### Scenario: Market order cooldown
- **WHEN** market rebalancing order is executed
- **THEN** system SHALL enter cooldown period (default 30 seconds)
- **AND** no additional market orders SHALL be generated during cooldown

#### Scenario: Market order size limit
- **WHEN** calculating market order size for rebalancing
- **THEN** order size SHALL be capped at 10% of max position value
- **AND** this prevents excessive slippage from large market orders

### Requirement: Enhanced rebalancing configuration
The system SHALL provide additional configuration for rebalancing behavior.

#### Scenario: Configure rebalancing parameters
- **WHEN** strategy is initialized with rebalancing parameters
- **THEN** system SHALL accept the following parameters:
  - `enable_rebalancing`: boolean (default True, changed from False)
  - `rebalancing_cooldown`: seconds between market orders (default 30)
  - `severe_imbalance_threshold`: balance_ratio below which to use market orders (default 0.50)
  - `market_order_size_cap`: max market order as fraction of position (default 0.10)
