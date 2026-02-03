## ADDED Requirements

### Requirement: Time-based urgency calculation
The system SHALL calculate urgency factor based on time remaining in market.

#### Scenario: Calculate time urgency
- **WHEN** market duration and elapsed time are known
- **THEN** time_urgency SHALL be `elapsed_time / market_duration`
- **AND** time_urgency SHALL range from 0.0 (start) to 1.0 (end)

#### Scenario: Handle unknown market duration
- **WHEN** market duration is not known
- **THEN** time_urgency SHALL default to 0.0
- **AND** urgency pricing SHALL be effectively disabled

### Requirement: Imbalance-based urgency calculation
The system SHALL calculate urgency factor based on position imbalance.

#### Scenario: Calculate imbalance urgency
- **WHEN** strategy has positions in both UP and DOWN tokens
- **THEN** imbalance_urgency SHALL be `1.0 - balance_ratio`
- **AND** balance_ratio is `min(up_shares, down_shares) / max(up_shares, down_shares)`

#### Scenario: Handle single-sided position
- **WHEN** strategy has position in only one side (balance_ratio = 0)
- **THEN** imbalance_urgency SHALL be 1.0 (maximum)

#### Scenario: Handle no position
- **WHEN** strategy has no positions
- **THEN** imbalance_urgency SHALL be 0.0

### Requirement: Combined urgency scoring
The system SHALL combine time and imbalance urgency into final urgency score.

#### Scenario: Calculate combined urgency
- **WHEN** time_urgency and imbalance_urgency are available
- **THEN** combined_urgency SHALL be `max(time_urgency, imbalance_urgency * urgency_weight)`
- **AND** urgency_weight SHALL default to 1.5 (imbalance matters more)

#### Scenario: Cap urgency at maximum
- **WHEN** combined_urgency calculation exceeds 1.0
- **THEN** combined_urgency SHALL be capped at 1.0

### Requirement: Urgency-adjusted limit pricing
The system SHALL adjust limit prices based on urgency to increase fill probability.

#### Scenario: Calculate urgency-adjusted limit price
- **WHEN** generating limit order for a side
- **THEN** system SHALL calculate base_offset from target_cost
- **AND** adjusted_offset SHALL be `base_offset * (1.0 - combined_urgency * urgency_price_factor)`
- **AND** urgency_price_factor SHALL default to 0.5 (reduce offset by up to 50%)
- **AND** limit_price SHALL be `market_price * (1.0 + adjusted_offset)` (capped at market_price)

#### Scenario: Maximum urgency pricing
- **WHEN** combined_urgency is 1.0
- **AND** urgency_price_factor is 0.5
- **THEN** limit price offset SHALL be reduced by 50%
- **AND** limit price SHALL be closer to market price

#### Scenario: Zero urgency pricing
- **WHEN** combined_urgency is 0.0
- **THEN** limit price SHALL use standard calculation (no adjustment)

### Requirement: Lagging side priority pricing
The system SHALL apply additional price improvement for the lagging (underweight) side.

#### Scenario: Boost lagging side limit price
- **WHEN** position is imbalanced (balance_ratio < 0.90)
- **AND** order is for the lagging side
- **THEN** limit price SHALL be boosted toward market price
- **AND** boost amount SHALL scale with imbalance severity

#### Scenario: Apply lagging side boost with urgency
- **WHEN** lagging side boost is applied
- **AND** urgency is high (>0.70)
- **THEN** lagging side limit price MAY equal market price (immediate fill attempt)

### Requirement: Urgency pricing configuration
The system SHALL provide configurable parameters for urgency pricing.

#### Scenario: Configure urgency pricing parameters
- **WHEN** strategy is initialized with urgency pricing parameters
- **THEN** system SHALL accept the following parameters:
  - `enable_urgency_pricing`: boolean (default True)
  - `urgency_weight`: weight for imbalance vs time (default 1.5)
  - `urgency_price_factor`: max offset reduction (default 0.5)
  - `market_duration`: total market duration in seconds (default 900 for 15min)
