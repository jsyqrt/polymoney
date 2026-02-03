## 1. 项目结构设置

- [x] 1.1 创建 `polymoney/testing/` 目录结构
- [x] 1.2 确认 matplotlib 依赖已安装（用于可视化）

## 2. 真实市场数据回放器实现

- [x] 2.1 实现 `MarketDataReplayer` 类，从 DataStorage 读取历史数据
- [x] 2.2 实现 K 线数据转 PriceData 的转换逻辑
- [x] 2.3 实现可配置回放速度（1x, 10x, max）
- [x] 2.4 实现市场生命周期事件（MarketStart, MarketEnd, MarketSettled）
- [x] 2.5 实现与 WebSocketManager 兼容的回调接口

## 3. 测试运行器实现

- [x] 3.1 实现 `TestRunner` 核心类
- [x] 3.2 实现回放模式（使用 MarketDataReplayer）
- [x] 3.3 实现实时模式（使用 MarketDataService WebSocket）
- [x] 3.4 集成 OrderManager Paper 模式进行订单处理
- [x] 3.5 实现策略参数配置
- [x] 3.6 实现 TestResult 结果对象

## 4. 性能分析模块实现

- [x] 4.1 实现 `PerformanceAnalytics` 核心类
- [x] 4.2 实现权益曲线（equity curve）记录
- [x] 4.3 实现交易日志记录（订单提交、成交）
- [x] 4.4 实现关键指标计算（有效成本率、平衡度、PnL、ROI）
- [x] 4.5 实现摘要报告生成（文本格式）
- [x] 4.6 实现数据导出功能（CSV、JSON）
- [x] 4.7 实现可视化图表生成（可选）

## 5. 数据准备与验证

- [x] 5.1 查询可用的已完成 15 分钟 BTC 市场数据
- [x] 5.2 验证数据完整性和质量
- [x] 5.3 准备测试用的市场 ID 列表

## 6. 集成测试与验证

- [x] 6.1 编写 `MarketDataReplayer` 单元测试
- [x] 6.2 编写 `TestRunner` 单元测试
- [x] 6.3 编写 `PerformanceAnalytics` 单元测试
- [x] 6.4 编写端到端集成测试

## 7. 真实数据测试执行

- [x] 7.1 运行单个市场的回放测试
- [x] 7.2 运行多个市场的批量回放测试
- [x] 7.3 记录发现的问题并修复
- [x] 7.4 验证修复后的系统行为正确性

## 8. 数据分析与报告

- [x] 8.1 收集多轮测试的收益率曲线数据
- [x] 8.2 生成测试报告（包含关键指标）
- [x] 8.3 分析策略效果和合理性
- [x] 8.4 总结测试结论和改进建议
