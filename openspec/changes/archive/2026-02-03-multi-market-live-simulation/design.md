## Context

当前 Polymoney 系统支持历史数据回测，但缺乏实时模拟交易能力。现有组件包括：
- `RealDataFetcher`: 支持 BTC 15 分钟市场的数据获取
- `PositionArbitrageStrategy`: 仓位套利策略（已优化）
- `TestRunner`: 基于回放数据的测试运行器
- `PerformanceAnalytics`: 性能指标计算

用户需要一个长时间运行的模拟系统，能够：
1. 实时监控多币种（BTC/ETH/SOL）的 15 分钟市场
2. 对每个市场执行策略模拟
3. 持续收集和输出性能指标
4. 提供便捷的状态查询接口

## Goals / Non-Goals

**Goals:**
- 支持 BTC、ETH、SOL 三种币的 15 分钟预测市场实时数据采集
- 支持可配置的运行时长（小时、天、长期）
- 定期输出收益率、夏普率等核心指标到文件
- 提供 CLI 工具查询运行状态和历史收益
- 提供清晰的操作文档

**Non-Goals:**
- 不支持真实交易（仅模拟）
- 不支持除 15 分钟市场外的其他周期
- 不实现 Web UI 或 API 服务端

## Decisions

### 1. 运行器架构：事件驱动 + 周期调度

**选择**: 使用 asyncio 事件循环 + 定时任务调度

**理由**:
- asyncio 原生支持并发 IO，适合多市场数据采集
- 定时任务可精确控制市场扫描频率
- 与现有 `RealDataFetcher` 的 async 接口一致

**替代方案**: 多进程/多线程
- 否决原因：增加复杂性，IO 密集型任务无需多核并行

### 2. 市场发现机制：轮询 + 缓存

**选择**: 每 5 分钟扫描一次市场列表，缓存活跃市场

**理由**:
- 15 分钟市场生命周期约 15-20 分钟，5 分钟扫描频率足够
- 缓存减少 API 请求，避免限流

**替代方案**: WebSocket 实时推送
- 否决原因：Polymarket 未提供市场列表的 WebSocket 接口

### 3. 指标输出：JSON Lines 格式

**选择**: 使用 `.jsonl` 格式，每行一条记录

**理由**:
- 追加写入效率高，适合长时间运行
- 易于解析和增量处理
- 支持流式读取

**替代方案**: SQLite 数据库
- 否决原因：对于单机模拟，文件足够简单且便于检查

### 4. 状态持久化：JSON 状态文件

**选择**: 单一 `status.json` 文件，原子写入

**理由**:
- 简单可靠，便于 CLI 工具读取
- 原子写入避免状态损坏

**文件结构**:
```json
{
  "start_time": "2026-02-03T10:00:00Z",
  "config": {"duration_hours": 24, "markets": ["btc", "eth", "sol"]},
  "stats": {"total_trades": 150, "total_pnl": 1234.56, "sharpe": 1.8},
  "markets_processed": 42,
  "last_update": "2026-02-03T12:30:00Z"
}
```

### 5. CLI 工具设计：子命令模式

**选择**: `sim-status` 命令支持多个子命令

**命令列表**:
- `sim-status` - 显示运行状态摘要
- `sim-status --history` - 显示历史收益
- `sim-status --json` - JSON 格式输出（便于 AI 解析）

## Risks / Trade-offs

### [Risk] API 限流导致数据缺失
- **Mitigation**: 实现指数退避重试，缓存已获取数据

### [Risk] 长时间运行进程崩溃
- **Mitigation**: 状态持久化支持断点续跑，定期写入检查点

### [Risk] ETH/SOL 市场 API 格式不同
- **Mitigation**: 先在 `RealDataFetcher` 中验证 API 兼容性，失败时优雅降级

### [Trade-off] 内存占用 vs 实时性
- 选择定期刷盘而非全量内存，牺牲少量实时性换取稳定性

### [Trade-off] 简单文件 vs 数据库
- 选择文件存储，牺牲查询灵活性换取部署简单性
