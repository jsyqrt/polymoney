## ADDED Requirements

### Requirement: Replayer loads historical data from database

数据回放器 SHALL 从 DataStorage 读取指定市场的完整历史数据。

#### Scenario: Load candlestick data for a completed market
- **WHEN** 调用 `MarketDataReplayer.load(market_id="...")` 加载市场数据
- **THEN** 从 candlesticks 表读取该市场的所有 K 线数据
- **THEN** 数据按时间戳排序

#### Scenario: Load trade data for detailed replay
- **WHEN** 调用 `MarketDataReplayer.load(market_id="...", include_trades=True)`
- **THEN** 同时加载 trades 表中的交易记录
- **THEN** 可用于更精细的价格重建

### Requirement: Replayer generates PriceData events

数据回放器 SHALL 将历史数据转换为 `PriceData` 事件流。

#### Scenario: Convert candlestick to PriceData
- **WHEN** 回放 K 线数据
- **THEN** 每根 K 线的收盘价生成一个 PriceData 事件
- **THEN** PriceData 包含 up_price 和 down_price（满足 up + down = 1.0）

#### Scenario: Emit events in chronological order
- **WHEN** 回放器开始回放
- **THEN** 事件按照原始时间戳顺序发出

### Requirement: Replayer supports configurable replay speed

数据回放器 SHALL 支持可配置的回放速度。

#### Scenario: Real-time speed replay
- **WHEN** 设置 `replay_speed=1`
- **THEN** 事件间隔与原始时间间隔一致

#### Scenario: Fast replay
- **WHEN** 设置 `replay_speed=10`
- **THEN** 事件间隔为原始时间间隔的 1/10

#### Scenario: Maximum speed replay
- **WHEN** 设置 `replay_speed=0` (或 "max")
- **THEN** 尽快发出所有事件，不等待

### Requirement: Replayer provides market lifecycle events

数据回放器 SHALL 生成市场生命周期事件。

#### Scenario: Emit market start event
- **WHEN** 回放开始时
- **THEN** 发出 MarketStart 事件，包含市场信息

#### Scenario: Emit market end event with settlement
- **WHEN** 回放结束时
- **THEN** 发出 MarketEnd 事件
- **THEN** 发出 MarketSettled 事件，包含真实的结算结果（winner）

### Requirement: Replayer compatible with MarketDataService interface

数据回放器 SHALL 实现与 WebSocketManager 相同的回调接口，可无缝替换。

#### Scenario: Use replayer as data source for MarketDataService
- **WHEN** 将 MarketDataReplayer 注入 MarketDataService
- **THEN** MarketDataService 正常工作
- **THEN** 策略无需感知数据来自回放还是实时连接
