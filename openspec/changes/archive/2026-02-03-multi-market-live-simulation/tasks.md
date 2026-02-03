## 1. RealDataFetcher 扩展（多币种支持）

- [x] 1.1 添加 ETH 和 SOL 市场发现方法 `find_15min_markets(coin: str)`
- [x] 1.2 实现多币种并行查询 `find_15min_markets(coins: list[str])`
- [x] 1.3 添加市场生命周期事件（market_active, market_settling, market_settled）
- [x] 1.4 实现价格流式订阅接口（轮询 orderbook）
- [ ] 1.5 单元测试：验证 ETH/SOL 市场发现

## 2. LiveSimulationRunner 核心实现

- [x] 2.1 创建 `polymoney/simulation/` 模块结构
- [x] 2.2 实现 `LiveRunner` 类基础框架（asyncio 事件循环）
- [x] 2.3 实现市场发现调度器（每 5 分钟扫描新市场）
- [x] 2.4 实现策略实例管理（每市场一个策略实例）
- [x] 2.5 实现模拟订单填充逻辑
- [x] 2.6 实现可配置运行时长解析（10h, 3d, 0 表示无限）

## 3. 指标输出与状态持久化

- [x] 3.1 实现 JSON Lines 指标输出（metrics.jsonl）
- [x] 3.2 实现状态文件原子写入（status.json）
- [x] 3.3 实现累计指标计算（PnL, ROI, Sharpe ratio, win rate）
- [x] 3.4 实现检查点和断点续跑（--resume 标志）
- [x] 3.5 实现旧状态归档（启动时移动旧 status.json）

## 4. 信号处理与优雅关闭

- [x] 4.1 实现 SIGINT/SIGTERM 信号处理
- [x] 4.2 实现优雅关闭流程（完成当前周期后退出）
- [x] 4.3 实现关闭超时强制退出（30 秒）
- [x] 4.4 实现关闭前状态保存

## 5. 状态查询 CLI 工具

- [x] 5.1 创建 `sim-status` 命令入口
- [x] 5.2 实现状态摘要显示（默认输出）
- [x] 5.3 实现历史数据查询（--history）
- [x] 5.4 实现时间范围过滤（--since）
- [x] 5.5 实现 JSON 输出模式（--json）
- [x] 5.6 实现实时尾随模式（--tail）
- [x] 5.7 实现单市场查询（--market）
- [x] 5.8 实现市场列表（--list-markets）
- [x] 5.9 实现 CSV 导出（--export）

## 6. 启动脚本与配置

- [x] 6.1 创建 `examples/run_live_simulation.py` 启动脚本
- [x] 6.2 实现命令行参数解析（--duration, --markets, --output-dir）
- [x] 6.3 实现策略参数配置（--target-cost, --ecr-threshold 等）
- [x] 6.4 实现日志配置（--log-level, --log-file）

## 7. 文档编写

- [x] 7.1 创建 `docs/live-simulation-guide.md` 操作文档
- [x] 7.2 编写启动说明（多种运行时长示例）
- [x] 7.3 编写停止说明（Ctrl+C 和 kill 命令）
- [x] 7.4 编写监控说明（sim-status 各种用法）
- [x] 7.5 编写 AI 集成说明（JSON 输出格式和解析示例）
- [x] 7.6 编写故障排除指南

## 8. 集成测试与验证

- [x] 8.1 端到端测试：启动模拟 → 运行 1 分钟 → 检查输出
- [x] 8.2 测试多币种同时监控
- [ ] 8.3 测试断点续跑功能
- [x] 8.4 测试 sim-status 各个子命令
- [ ] 8.5 验证长时间运行稳定性（至少 1 小时）
