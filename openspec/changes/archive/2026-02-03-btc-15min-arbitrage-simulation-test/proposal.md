## Why

当前 Polymoney 框架已实现核心功能（WebSocket 数据获取、策略引擎、订单管理、回测引擎等），但缺少端到端的集成测试验证。需要通过 15 分钟 BTC 涨跌预测市场的仓位套利策略，使用**真实市场数据**进行 Paper Trading 测试，验证系统功能正确性，发现并修复潜在问题，同时收集性能数据评估策略效果。

## What Changes

- 新增测试运行器，支持完整的 15 分钟市场周期真实数据测试
- 新增真实市场数据回放器，从数据库或 API 获取历史数据进行回放
- 新增收益率曲线记录与分析工具
- 新增测试结果分析报告生成
- 修复测试过程中发现的系统缺陷

## Capabilities

### New Capabilities

- `test-runner`: 端到端测试运行器，支持连接真实 WebSocket 或回放历史数据进行 Paper Trading 测试
- `market-data-replayer`: 真实市场数据回放器，从数据库读取已完成市场的历史数据进行回放测试
- `performance-analytics`: 收益率曲线记录、交易统计、性能分析与报告生成

### Modified Capabilities

<!-- 无需修改现有 spec 的行为要求 -->

## Impact

- **代码影响**: 新增 `polymoney/testing/` 模块（测试运行器、数据回放器、分析工具）
- **测试影响**: 新增集成测试用例，验证策略引擎与订单管理器使用真实数据的协作
- **数据依赖**: 需要已有的 15 分钟 BTC 市场历史数据（candlesticks, trades 表）
- **文档影响**: 生成测试报告，包含收益率曲线、交易明细、策略效果分析
