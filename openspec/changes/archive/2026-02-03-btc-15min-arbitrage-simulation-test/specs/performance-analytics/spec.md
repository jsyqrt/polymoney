## ADDED Requirements

### Requirement: Analytics tracks equity curve over time

分析模块 SHALL 记录测试过程中的权益曲线（equity curve）。

#### Scenario: Equity curve records position value at each tick
- **WHEN** 每次价格更新时
- **THEN** 记录当前时间戳、仓位市值、已花费成本
- **THEN** 权益曲线可导出为 CSV 格式

#### Scenario: Equity curve shows unrealized and realized PnL
- **WHEN** 测试进行中
- **THEN** 权益曲线包含未实现 PnL（基于当前市场价）
- **WHEN** 测试结束后
- **THEN** 权益曲线包含最终实现 PnL（基于真实结算价格）

### Requirement: Analytics generates trade log

分析模块 SHALL 记录所有交易明细。

#### Scenario: Trade log records order submissions
- **WHEN** 策略提交订单
- **THEN** 记录订单类型、方向、限价、数量、时间戳

#### Scenario: Trade log records order fills
- **WHEN** 订单成交
- **THEN** 记录成交价格、成交数量、成交时间

### Requirement: Analytics computes key metrics

分析模块 SHALL 计算关键绩效指标。

#### Scenario: Compute effective cost rate
- **WHEN** 测试结束后
- **THEN** 计算有效成本率 = 总成本 / min(Up 份额, Down 份额)

#### Scenario: Compute position balance ratio
- **WHEN** 测试结束后
- **THEN** 计算仓位平衡度 = min(Up, Down) / max(Up, Down)

#### Scenario: Compute total PnL and ROI
- **WHEN** 测试结束后
- **THEN** 计算总 PnL = 结算价值 - 总成本
- **THEN** 计算 ROI = 总 PnL / 总成本

#### Scenario: Compute win rate across multiple markets
- **WHEN** 运行多轮测试后
- **THEN** 计算胜率 = 盈利市场数 / 总市场数

### Requirement: Analytics generates summary report

分析模块 SHALL 生成可读的摘要报告。

#### Scenario: Summary report includes key statistics
- **WHEN** 调用 `generate_report()` 方法
- **THEN** 生成包含以下内容的文本报告：
  - 测试配置（数据来源、回放速度）
  - 最终仓位（Up/Down 份额和成本）
  - 关键指标（有效成本率、平衡度、PnL、ROI）
  - 交易统计（订单数、成交率、平均成交价）

### Requirement: Analytics supports data export

分析模块 SHALL 支持将数据导出为多种格式。

#### Scenario: Export equity curve to CSV
- **WHEN** 调用 `export_equity_curve("equity.csv")`
- **THEN** 生成包含时间戳和权益值的 CSV 文件

#### Scenario: Export trade log to JSON
- **WHEN** 调用 `export_trades("trades.json")`
- **THEN** 生成包含所有交易记录的 JSON 文件

#### Scenario: Export metrics to JSON
- **WHEN** 调用 `export_metrics("metrics.json")`
- **THEN** 生成包含所有计算指标的 JSON 文件

### Requirement: Analytics supports visualization

分析模块 SHALL 支持生成可视化图表（如果 matplotlib 可用）。

#### Scenario: Plot equity curve
- **WHEN** 调用 `plot_equity_curve("equity.png")`
- **THEN** 生成权益曲线图表
- **THEN** 图表显示时间 vs 仓位市值

#### Scenario: Plot position balance over time
- **WHEN** 调用 `plot_balance("balance.png")`
- **THEN** 生成仓位平衡度变化图
