# Live Simulation Guide

本文档说明如何使用 Polymoney 的实时模拟交易系统，对 BTC、ETH、SOL 的 15 分钟预测市场执行仓位套利策略。

## 目录

1. [快速开始](#快速开始)
2. [启动选项](#启动选项)
3. [监控状态](#监控状态)
4. [停止模拟](#停止模拟)
5. [输出文件](#输出文件)
6. [AI 集成](#ai-集成)
7. [故障排除](#故障排除)

---

## 快速开始

### 1. 安装依赖

```bash
cd /path/to/polymoney
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 运行模拟

```bash
# 运行 10 小时
python examples/run_live_simulation.py --duration 10h

# 运行 3 天
python examples/run_live_simulation.py --duration 3d

# 无限期运行（直到 Ctrl+C）
python examples/run_live_simulation.py
```

### 3. 检查状态

```bash
# 查看状态摘要
python -m polymoney.simulation.status_cli

# JSON 格式（便于 AI 解析）
python -m polymoney.simulation.status_cli --json
```

---

## 启动选项

### 运行时长

| 参数 | 说明 | 示例 |
|------|------|------|
| `--duration 0` | 无限期运行 | 默认 |
| `--duration 10h` | 运行 10 小时 | `--duration 10h` |
| `--duration 3d` | 运行 3 天 | `--duration 3d` |
| `--duration 30m` | 运行 30 分钟 | `--duration 30m` |

### 市场选择

```bash
# 仅监控 BTC
python examples/run_live_simulation.py --markets btc

# 监控 BTC 和 ETH
python examples/run_live_simulation.py --markets btc,eth

# 监控所有支持的币种（默认）
python examples/run_live_simulation.py --markets btc,eth,sol
```

### 策略参数

```bash
python examples/run_live_simulation.py \
    --target-cost 0.98 \
    --batch-size 100 \
    --ecr-threshold 1.05
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--target-cost` | 0.98 | 目标成本率 |
| `--batch-size` | 100.0 | 订单批量（美元） |
| `--ecr-threshold` | 1.05 | ECR 止损阈值 |
| `--disable-ecr-stoploss` | - | 禁用 ECR 止损 |
| `--disable-rebalancing` | - | 禁用市价单再平衡 |
| `--disable-trend-detection` | - | 禁用趋势检测 |

### 其他选项

```bash
# 自定义输出目录
python examples/run_live_simulation.py --output-dir ./my_results

# 从上次状态恢复
python examples/run_live_simulation.py --resume

# 设置日志级别
python examples/run_live_simulation.py --log-level DEBUG

# 输出日志到文件
python examples/run_live_simulation.py --log-file sim.log
```

---

## 监控状态

### 基本状态查询

```bash
# 显示状态摘要
python -m polymoney.simulation.status_cli

# 输出示例：
# ============================================================
# SIMULATION STATUS
# ============================================================
# 
# Status: RUNNING
# Started: 2026-02-03T10:00:00
# 
# Performance:
#   Markets Processed: 42
#   Win Rate: 76.2%
#   Total PnL: $1,234.56
#   Total ROI: 3.45%
#   Sharpe Ratio: 1.82
```

### JSON 输出（便于自动化）

```bash
python -m polymoney.simulation.status_cli --json
```

输出格式：
```json
{
  "running": true,
  "start_time": "2026-02-03T10:00:00",
  "stats": {
    "markets_processed": 42,
    "win_rate": 0.762,
    "total_pnl": 1234.56,
    "sharpe_ratio": 1.82
  },
  "active_markets": ["btc-updown-15m-1770129900"]
}
```

### 历史数据查询

```bash
# 显示历史记录
python -m polymoney.simulation.status_cli --history

# 仅显示最近 2 小时
python -m polymoney.simulation.status_cli --history --since 2h

# 导出为 CSV
python -m polymoney.simulation.status_cli --history --export results.csv
```

### 实时监控

```bash
# 实时跟踪指标更新
python -m polymoney.simulation.status_cli --tail
```

### 单市场查询

```bash
# 查看特定市场详情
python -m polymoney.simulation.status_cli --market btc-updown-15m-1770129900

# 列出所有已处理市场
python -m polymoney.simulation.status_cli --list-markets
```

---

## 停止模拟

### 方法 1：Ctrl+C（推荐）

在运行模拟的终端按 `Ctrl+C`，系统会：
1. 完成当前市场处理周期
2. 保存最终状态到 `status.json`
3. 输出最终指标
4. 优雅退出

### 方法 2：发送 SIGTERM

```bash
# 找到进程 ID
ps aux | grep run_live_simulation

# 发送终止信号
kill <PID>
```

### 方法 3：强制终止

```bash
# 强制终止（不推荐，可能丢失状态）
kill -9 <PID>
```

---

## 输出文件

所有输出默认保存在 `simulation_results/` 目录：

### status.json

当前状态快照，每次指标更新时刷新。

```json
{
  "running": true,
  "start_time": "2026-02-03T10:00:00",
  "config": {
    "coins": ["btc", "eth", "sol"],
    "duration_seconds": 36000
  },
  "stats": {
    "markets_processed": 42,
    "win_rate": 0.762,
    "total_pnl": 1234.56,
    "sharpe_ratio": 1.82
  },
  "active_markets": ["btc-updown-15m-1770129900"]
}
```

### metrics.jsonl

时间序列指标，每 5 分钟追加一行：

```json
{"timestamp": "2026-02-03T10:05:00", "total_pnl": 100.0, "sharpe_ratio": 0.5, ...}
{"timestamp": "2026-02-03T10:10:00", "total_pnl": 150.0, "sharpe_ratio": 0.8, ...}
```

### results.jsonl

每个市场的最终结果：

```json
{"slug": "btc-updown-15m-1770129000", "winner": "up", "pnl": 50.0, "roi": 0.05, ...}
{"slug": "eth-updown-15m-1770129000", "winner": "down", "pnl": -20.0, "roi": -0.02, ...}
```

---

## AI 集成

### 检查运行状态

```bash
python -m polymoney.simulation.status_cli --json
```

**解析返回值：**
- `running: true` → 模拟正在运行
- `running: false` → 模拟已停止

### 获取性能指标

```bash
python -m polymoney.simulation.status_cli --json | jq '.stats'
```

**关键指标：**
- `total_pnl`: 总盈亏（美元）
- `total_roi`: 总回报率
- `sharpe_ratio`: 夏普率
- `win_rate`: 胜率

### 获取历史数据

```bash
python -m polymoney.simulation.status_cli --history --json
```

### 示例：AI 监控脚本

```python
import json
import subprocess

def check_simulation_status():
    """检查模拟状态并返回关键指标。"""
    result = subprocess.run(
        ["python", "-m", "polymoney.simulation.status_cli", "--json"],
        capture_output=True,
        text=True,
    )
    
    if result.returncode != 0:
        return {"error": "Failed to get status"}
    
    status = json.loads(result.stdout)
    stats = status.get("stats", {})
    
    return {
        "running": status.get("running", False),
        "pnl": stats.get("total_pnl", 0),
        "roi": stats.get("total_roi", 0),
        "sharpe": stats.get("sharpe_ratio", 0),
        "markets": stats.get("markets_processed", 0),
    }

# 使用示例
status = check_simulation_status()
print(f"PnL: ${status['pnl']:.2f}, Sharpe: {status['sharpe']:.2f}")
```

---

## 故障排除

### 问题：API 请求失败

**症状：** 日志显示 `Request failed` 或 `Rate limited`

**解决方案：**
1. 检查网络连接
2. 等待几分钟后重试（API 限流）
3. 检查 Polymarket API 状态

### 问题：没有发现市场

**症状：** `No active markets found`

**解决方案：**
1. 15 分钟市场可能在交易间隙
2. 等待下一个 15 分钟周期开始
3. 检查 Polymarket 网站确认市场存在

### 问题：模拟崩溃

**症状：** 进程意外退出

**解决方案：**
1. 使用 `--resume` 从上次状态恢复
2. 检查日志文件 `--log-file sim.log`
3. 增加日志级别 `--log-level DEBUG`

### 问题：状态文件损坏

**症状：** `status.json` 无法解析

**解决方案：**
1. 删除 `status.json`
2. 重新启动模拟（不使用 `--resume`）
3. 检查 `archive/` 目录中的历史状态

### 问题：内存使用过高

**症状：** 长时间运行后内存增长

**解决方案：**
1. 定期重启模拟
2. 使用 `--duration` 设置固定运行时长
3. 减少 `--metrics-interval` 降低数据量

---

## 最佳实践

1. **使用固定时长**：建议设置 `--duration`，避免无限运行
2. **定期检查状态**：使用 `sim-status` 定期监控
3. **保留日志**：使用 `--log-file` 保存日志便于排查问题
4. **测试环境**：先用短时间（如 30 分钟）测试确认工作正常
5. **备份数据**：定期备份 `simulation_results/` 目录

---

*最后更新：2026-02-03*
