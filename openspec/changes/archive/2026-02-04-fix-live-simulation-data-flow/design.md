## Context

当前的 `LiveRunner` 实现存在以下问题：

1. **市场选择问题**: `_market_scan_loop` 会发现所有未关闭的市场，包括已经接近结算（价格已经显示出明确赢家）的市场。策略在这类市场上无法建立平衡仓位。

2. **策略初始化问题**: `MarketSimulation.__init__` 创建策略实例后没有调用 `on_market_start()`，导致 `market_start_time` 为 None。

3. **状态跟踪问题**: `status.json` 只包含聚合统计，没有实时的市场级数据（持仓、ECR 等），使得调试和监控困难。

4. **反馈延迟问题**: 默认 5 分钟的 metrics 输出间隔太长，无法及时发现问题。

## Goals / Non-Goals

**Goals:**
- 确保模拟只跟踪有足够交易时间的市场
- 提供实时的市场级状态数据
- 正确初始化策略生命周期
- 支持更快的反馈周期用于测试

**Non-Goals:**
- 不修改策略逻辑本身
- 不添加新的策略类型
- 不改变数据存储架构

## Decisions

### 决策 1: 市场有效性验证

**选择**: 在 `_start_market_simulation` 中添加市场有效性检查

**逻辑**:
1. 检查市场距离结算时间 >= `min_trading_time`（默认 5 分钟）
2. 检查价格状态：如果 `up_price < 0.05 或 down_price < 0.05`，且 `price_sum > 0.98`，说明结果已定，跳过
3. 新增配置参数 `min_trading_time` 控制最小交易时间

**理由**:
- 避免浪费资源跟踪无法有效交易的市场
- 集中在验证阶段，不影响后续流程
- 可配置以适应不同场景（如测试时可设置更短时间）

**替代方案**:
- 在策略层面处理：增加复杂度，且每个策略都要处理
- 完全不处理：导致产生无意义的模拟数据

### 决策 2: 实时状态跟踪

**选择**: 在 `_write_status` 中添加 `market_details` 字段

**结构**:
```json
{
  "market_details": {
    "btc-updown-15m-xxx": {
      "coin": "btc",
      "up_shares": 50.2,
      "down_shares": 48.5,
      "up_cost": 25.1,
      "down_cost": 24.2,
      "ecr": 0.96,
      "balance_ratio": 0.97,
      "orders_submitted": 15,
      "orders_filled": 12,
      "current_up_price": 0.52,
      "current_down_price": 0.48,
      "started_at": "2026-02-04T11:00:00"
    }
  }
}
```

**理由**:
- 提供完整的市场级视图
- 便于 AI 和监控工具解析
- 向后兼容：新增字段不影响现有消费者

### 决策 3: 策略生命周期修复

**选择**: 在 `_start_market_simulation` 中调用 `sim.strategy.on_market_start()`

**实现**:
```python
sim = MarketSimulation(market, self.config)
sim.strategy.on_market_start(
    market_id=slug,
    market_info={
        "slug": slug,
        "coin": event.coin,
        "condition_id": event.condition_id,
    }
)
self._active_sims[slug] = sim
```

**理由**:
- 确保策略正确初始化
- `market_start_time` 被正确设置
- 市场阶段计算准确

### 决策 4: 可配置的输出间隔

**选择**: 支持通过命令行设置更短的 metrics 间隔

**实现**:
- 已有 `--metrics-interval` 参数
- 默认值从 300 秒降低到 10 秒（对于测试友好）
- 添加 `--quick-test` 模式：自动设置 `metrics-interval=10`, `price-interval=2`, `scan-interval=30`

**理由**:
- 测试时需要快速反馈
- 生产环境可以使用较长间隔节省资源

## Risks / Trade-offs

### 风险 1: 市场过滤过于严格
**风险**: 过滤逻辑可能错误地排除有效市场
**缓解**: 
- 记录被跳过的市场及原因
- 提供 `--no-market-filter` 选项禁用过滤（用于调试）

### 风险 2: 状态文件变大
**风险**: 包含市场详情后，status.json 文件变大
**缓解**: 
- 通常只有 3-5 个活跃市场
- 每个市场详情约 500 bytes
- 总增量约 2KB，可接受

### 风险 3: 更频繁的文件写入
**风险**: 更短的输出间隔导致更多 I/O
**缓解**:
- 使用原子写入避免损坏
- 测试模式才使用短间隔
- 生产环境保持 5 分钟间隔

## Implementation Notes

修改文件清单：
1. `polymoney/simulation/live_runner.py`:
   - `_start_market_simulation`: 添加有效性检查和策略初始化
   - `_write_status`: 添加 market_details
   - `MarketSimulation`: 添加 `get_status()` 方法获取实时数据

2. `examples/run_live_simulation.py`:
   - 添加 `--quick-test` 选项
   - 更新默认值说明

3. `polymoney/simulation/status_cli.py`:
   - 解析和显示新的 market_details 字段
