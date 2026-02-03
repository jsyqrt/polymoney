## ADDED Requirements

### Requirement: Price momentum calculation
The system SHALL calculate price momentum to detect one-sided market trends.

#### Scenario: Calculate momentum from price history
- **WHEN** strategy has received at least N price updates (configurable, default 5)
- **THEN** system SHALL calculate momentum as `(current_price - price_N_ago) / price_N_ago`
- **AND** momentum SHALL be calculated separately for UP and DOWN tokens

#### Scenario: Insufficient price history
- **WHEN** strategy has received fewer than N price updates
- **THEN** momentum SHALL return 0.0 (neutral)
- **AND** trend detection SHALL be disabled until sufficient data

### Requirement: Trend confidence scoring
The system SHALL compute a confidence score for detected trends.

#### Scenario: Calculate trend confidence
- **WHEN** momentum is calculated
- **THEN** trend_side SHALL be "up" if momentum > 0, "down" if momentum < 0, "neutral" if 0
- **AND** confidence SHALL be `min(1.0, abs(momentum) / max_momentum_threshold)`
- **AND** max_momentum_threshold SHALL default to 0.10 (10%)

#### Scenario: High confidence trend detection
- **WHEN** confidence exceeds threshold (default 0.50)
- **THEN** system SHALL emit trend_detected event
- **AND** event SHALL include trend_side and confidence value

### Requirement: Trend-based order filtering
The system SHALL filter orders based on detected trends to prevent accumulating losing-side positions.

#### Scenario: Stop orders on losing side of strong trend
- **WHEN** trend is detected with confidence >= trend_stop_threshold (default 0.70)
- **AND** order is for the opposite side of the trend
- **THEN** system SHALL reject the order
- **AND** system SHALL log rejection reason with trend details

#### Scenario: Allow orders on winning side
- **WHEN** trend is detected
- **AND** order is for the same side as the trend
- **THEN** system SHALL allow the order (subject to other filters)

#### Scenario: Normal operation without strong trend
- **WHEN** trend confidence is below trend_stop_threshold
- **THEN** system SHALL allow orders on both sides (subject to other filters)

### Requirement: Trend detection configuration
The system SHALL provide configurable parameters for trend detection.

#### Scenario: Configure trend detection parameters
- **WHEN** strategy is initialized with trend detection parameters
- **THEN** system SHALL accept the following parameters:
  - `enable_trend_detection`: boolean (default True)
  - `momentum_window`: number of ticks for momentum calculation (default 5)
  - `trend_stop_threshold`: confidence level to stop losing-side orders (default 0.70)
  - `max_momentum_threshold`: momentum value for 100% confidence (default 0.10)

#### Scenario: Disable trend detection
- **WHEN** `enable_trend_detection` is False
- **THEN** system SHALL skip all trend calculations
- **AND** system SHALL allow all orders (subject to other filters)
