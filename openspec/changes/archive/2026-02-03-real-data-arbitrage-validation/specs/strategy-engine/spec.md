## MODIFIED Requirements

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

#### Scenario: ECR stop-loss trigger
- **WHEN** projected effective cost rate exceeds threshold (default 105%)
- **THEN** system SHALL stop generating new buy orders
- **AND** system SHALL log ECR violation with current value and threshold
- **AND** strategy SHALL enter "risk_limited" state

#### Scenario: ECR calculation
- **WHEN** strategy has positions in both UP and DOWN tokens
- **THEN** system SHALL calculate ECR as total_cost / min(up_shares, down_shares)
- **AND** system SHALL project ECR after each potential order

## ADDED Requirements

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
- **THEN** system SHALL enter cooldown period (default 60 seconds)
- **AND** system SHALL not trigger another rebalancing during cooldown
- **AND** limit orders SHALL continue normally during cooldown

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
