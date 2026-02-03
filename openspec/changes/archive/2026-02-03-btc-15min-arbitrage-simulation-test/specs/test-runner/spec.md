## ADDED Requirements

### Requirement: Test runner executes complete market cycle with real data

测试运行器 SHALL 使用真实市场数据执行完整的 15 分钟市场周期测试，包括市场开始、价格更新、订单处理和市场结算。

#### Scenario: Run a complete market cycle with historical data replay
- **WHEN** 调用 `TestRunner.run(mode="replay", market_id="...")` 启动测试
- **THEN** 测试运行器从数据库读取指定市场的历史数据
- **THEN** 策略在每次价格更新时生成订单信号
- **THEN** 订单通过 OrderManager Paper 模式处理
- **THEN** 市场结束时根据真实结算结果计算 PnL

#### Scenario: Run with live WebSocket connection
- **WHEN** 调用 `TestRunner.run(mode="live")` 启动测试
- **THEN** 测试运行器连接 Polymarket WebSocket
- **THEN** 实时接收价格更新并传递给策略
- **THEN** 使用 Paper Trading 模式处理订单

### Requirement: Test runner integrates with existing components

测试运行器 SHALL 复用现有的 `MarketDataService` 和 `OrderManager` 组件。

#### Scenario: Strategy receives price updates via MarketDataService
- **WHEN** 数据源产生新的价格数据
- **THEN** 价格数据通过 MarketDataService 的事件机制分发
- **THEN** 策略的 `on_price_update()` 方法被调用

#### Scenario: Orders processed via OrderManager Paper mode
- **WHEN** 策略生成订单信号
- **THEN** 订单通过 OrderManager(mode="paper") 提交
- **THEN** Paper 模式根据市场价格决定是否撮合

### Requirement: Test runner supports configurable parameters

测试运行器 SHALL 支持配置测试参数。

#### Scenario: Configure replay speed
- **WHEN** 创建 TestRunner 时指定 `replay_speed=10`
- **THEN** 历史数据以 10 倍速回放

#### Scenario: Configure strategy parameters
- **WHEN** 创建 TestRunner 时指定 `strategy_params={...}`
- **THEN** 策略使用指定的参数初始化

#### Scenario: Configure position size
- **WHEN** 创建 TestRunner 时指定 `position_size=100.0`
- **THEN** 策略使用指定的仓位大小限制

### Requirement: Test runner outputs execution results

测试运行器 SHALL 输出完整的执行结果。

#### Scenario: Return test result summary
- **WHEN** 测试完成后
- **THEN** 返回包含以下信息的 TestResult 对象：
  - 最终仓位（Up/Down 份额和成本）
  - 结算结果和 PnL
  - 交易统计（订单数、成交数）
  - 关键指标（有效成本率、平衡度）
