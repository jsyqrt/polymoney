## Why

实时模拟交易系统当前无法产生有效的交易数据。经诊断发现以下问题：
1. 模拟启动时会跟踪已接近结算的市场（如 UP: 0.001, DOWN: 0.999），没有足够时间建立平衡仓位
2. 状态文件不显示实时交易活动（订单、持仓、ECR），只能在结算后看到结果
3. 策略实例化后未调用 `on_market_start()`，导致 `market_start_time` 为 None
4. 默认 5 分钟的指标输出间隔对于测试验证来说太长

## What Changes

### 市场选择逻辑增强
- 新增市场有效性验证：只跟踪距离结算还有足够时间（如 >= 5 分钟）的市场
- 新增价格合理性检查：排除已经"决出胜负"的市场（如 price_sum ≈ 1.0 且一方 < 0.05）
- 新增市场开始时间记录：正确初始化策略的 `market_start_time`

### 实时状态跟踪
- 在 status.json 中添加每个活跃市场的实时数据：
  - 当前持仓（UP/DOWN shares 和 cost）
  - 当前 ECR 和 balance ratio
  - 已生成的订单数量
  - 当前市场价格
- 更频繁地更新状态文件（订单变化时更新，而非仅在定时器触发时）

### 指标和日志增强
- 添加命令行选项 `--metrics-interval` 支持更短的输出间隔（如 10 秒用于测试）
- 在订单生成/成交时输出 DEBUG 日志，便于诊断策略行为
- 添加 `--verbose` 选项显示更详细的策略决策过程

### 策略初始化修复
- 在创建 `MarketSimulation` 时正确调用策略的 `on_market_start()`

## Capabilities

### New Capabilities
无需新建能力，这是对现有能力的修复和增强。

### Modified Capabilities
- `live-simulation-runner`: 增加市场有效性验证、实时状态跟踪、策略正确初始化

## Impact

- **代码改动**: 
  - `polymoney/simulation/live_runner.py` - 主要修改
  - `examples/run_live_simulation.py` - 添加命令行选项
- **状态文件格式**: status.json 将包含更多实时数据（向后兼容）
- **无破坏性变更**: 所有现有功能保持不变
