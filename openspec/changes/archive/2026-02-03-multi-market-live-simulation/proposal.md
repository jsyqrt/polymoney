## Why

当前系统只支持对历史数据的回测，无法实时监控多个市场并进行模拟交易。需要一个实时模拟系统来验证仓位套利策略在真实市场条件下的表现，收集长期运行数据（如10小时、3天或更长），并提供便捷的状态查询接口供人工或AI监控。

## What Changes

- 新增多市场实时数据采集器，支持 BTC、ETH、SOL 的 15 分钟预测市场
- 新增实时模拟交易运行器，支持可配置的运行时长
- 定期输出收益率、夏普率等指标到文件
- 提供命令行状态查询工具，支持 AI 或人工检查运行状态和历史收益
- 编写完整的操作文档，包括启动、停止和监控说明

## Capabilities

### New Capabilities

- `live-simulation-runner`: 实时模拟交易运行器，管理多市场的实时数据采集和策略执行，支持可配置运行时长、定期指标输出
- `simulation-status-cli`: 状态查询命令行工具，提供运行状态、当前和历史收益数据的查询接口

### Modified Capabilities

- `real-data-fetcher`: 扩展支持 ETH 和 SOL 的 15 分钟市场数据获取

## Impact

- **新增文件**：
  - `polymoney/simulation/live_runner.py` - 实时模拟运行器
  - `polymoney/simulation/status_cli.py` - 状态查询命令
  - `examples/run_live_simulation.py` - 启动脚本
  - `docs/live-simulation-guide.md` - 操作文档
- **修改文件**：
  - `polymoney/data/real_data_fetcher.py` - 支持多币种市场
- **输出文件**：
  - `simulation_results/` - 模拟数据和指标输出目录
- **依赖**：无新增外部依赖
