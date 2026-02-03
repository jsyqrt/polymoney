## Context

Polymoney 框架已实现以下核心组件：
- `PositionArbitrageStrategy`: 仓位套利策略，支持三阶段执行（建仓期、优化期、补板期）
- `OrderManager`: 订单管理器，支持 Paper/Live 模式
- `StrategyEngine`: 策略引擎，管理策略生命周期
- `MarketDataService`: 实时市场数据服务，WebSocket 连接
- `DataStorage`: 数据存储层，支持 candlesticks、trades、orders 等历史数据

当前缺少端到端的集成测试。需要使用**真实市场数据**验证系统功能，模拟数据无法反映真实市场行为。

## Goals / Non-Goals

**Goals:**
- 使用真实的 15 分钟 BTC 市场数据进行测试（历史回放或实时连接）
- 验证订单生成、Paper Trading 撮合、仓位更新的完整流程
- 记录交易过程数据，生成收益率曲线
- 发现并修复系统缺陷

**Non-Goals:**
- 不实现 Live Trading 测试（仅 Paper Trading）
- 不实现合成/模拟数据生成（使用真实数据）
- 不实现 UI 界面（使用命令行和数据文件输出）

## Decisions

### Decision 1: 数据来源 - 历史回放 vs 实时连接

**选择**: 支持两种模式
- **回放模式**: 从 DataStorage 读取已完成市场的历史数据进行回放
- **实时模式**: 连接 WebSocket 获取实时数据进行 Paper Trading

**理由**:
- 回放模式可重复、可控，便于调试和回归测试
- 实时模式验证真实环境下的系统行为
- 两种模式互补，覆盖不同测试场景

### Decision 2: 架构设计 - 复用现有组件

**选择**: 复用 `MarketDataService` 和 `OrderManager`

**理由**:
- 避免重复实现，测试真实的代码路径
- 回放器实现与 WebSocketManager 相同的接口
- OrderManager 的 Paper 模式已实现订单撮合逻辑

### Decision 3: 订单撮合 - 复用 OrderManager Paper 模式

**选择**: 使用 OrderManager 的 Paper Trading 模式

**理由**:
- Paper 模式已实现限价单撮合：市场价 ≤ 限价时成交
- 复用现有逻辑，确保测试与生产代码一致
- 无需重复实现撮合逻辑

### Decision 4: 数据输出格式

**选择**: JSON + CSV + 文本报告

**理由**:
- JSON: 结构化数据便于程序处理
- CSV: 收益率曲线等时序数据便于 Excel/Python 分析
- 文本报告: 可读性强，便于快速查看结果

## Risks / Trade-offs

| 风险 | 缓解措施 |
|------|----------|
| 历史数据不完整或有缺失 | 检测数据质量，跳过异常数据点 |
| 回放速度与真实时间不一致 | 支持可配置的回放速度（1x, 10x, max） |
| Paper Trading 撮合与真实市场差异 | 记录撮合假设，后续可对比真实结果 |
