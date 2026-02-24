# Live Simulation & Trading Guide

本文档说明如何使用 Polymoney 的实时交易系统，对 BTC、ETH、SOL 的 15 分钟预测市场执行仓位套利策略。

## 目录

1. [快速开始](#快速开始)
2. [运行模式](#运行模式)
3. [启动选项](#启动选项)
4. [监控状态](#监控状态)
5. [停止交易](#停止交易)
6. [输出文件](#输出文件)
7. [安全机制](#安全机制)
8. [AI 集成](#ai-集成)
9. [故障排除](#故障排除)

---

## 快速开始

### 1. 安装依赖

```bash
cd /path/to/polymoney
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境（实盘必须）

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env，填入你的配置
# 纸上交易不需要 private key，实盘交易必须配置
```

### 3. 运行

```bash
# 纸上交易（默认）
python scripts/run_trading.py

# 纸上交易，运行 10 小时
python scripts/run_trading.py -d 10h

# 实盘交易（需要 POLYMARKET_PRIVATE_KEY）
python scripts/run_trading.py --live
```

### 4. 检查状态

```bash
# 查看状态摘要
python -m polymoney.simulation.status_cli

# JSON 格式（便于 AI 解析）
python -m polymoney.simulation.status_cli --json
```

---

## 运行模式

系统使用统一的 `scripts/run_trading.py` 入口，基于模块化 `TradingRunner` 架构：

```bash
# 纸上交易 — SimulatedExecutor（深度模型 + Spread 概率）
python scripts/run_trading.py

# 实盘交易 — LiveExecutor（真实 CLOB 订单）
python scripts/run_trading.py --live
```

**架构组件：**

| 组件 | 说明 |
|------|------|
| `TradingRunner` | 顶层编排器 |
| `MarketDataProvider` | 数据层（REST + WebSocket + 订单簿） |
| `MarketContext` | 轻量市场上下文（策略 + 状态） |
| `OrderExecutor` | 执行抽象层（SimulatedExecutor / LiveExecutor） |
| `FillManager` | 成交跟踪 + 仓位对账 |
| `RiskManager` | 风控熔断（每日亏损/连续亏损/持仓上限） |
| `KillSwitch` | 紧急停止机制 |
| `AlertManager` | Webhook 告警（Discord 等） |

---

## 启动选项

以下选项适用于 `run` 命令（默认命令）：

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
python scripts/run_trading.py -m btc

# 监控 BTC 和 ETH
python scripts/run_trading.py -m btc,eth

# 监控所有支持的币种（默认）
python scripts/run_trading.py -m btc,eth,sol
```

### 多时间框架（NEW）

```bash
# 仅交易 15 分钟市场（默认，向后兼容）
python scripts/run_trading.py

# 交易 1 小时市场（零 taker 手续费）
python scripts/run_trading.py --timeframes 1h

# 同时交易 15 分钟和 1 小时市场
python scripts/run_trading.py --timeframes 15m,1h

# 交易所有支持的时间框架
python scripts/run_trading.py --timeframes 15m,1h,4h
```

| 时间框架 | 市场时长 | Taker 手续费 | 套利优势 |
|---------|---------|-------------|---------|
| `15m` | 15 分钟 | 最高 1.56% | 高频，流动性好 |
| `1h` | 1 小时 | 0%（零手续费）| 更多时间平衡仓位 |
| `4h` | 4 小时 | 0%（零手续费）| 最多时间，低频 |

策略参数会根据时间框架自动调整（phase 时长、订单超时等）。

### Binance 价格信号（NEW）

```bash
# 启用 Binance 实时价格信号
python scripts/run_trading.py --enable-binance-feed

# 启用 Binance 但禁用方向性信号
python scripts/run_trading.py --enable-binance-feed --disable-directional-signal
```

Binance 价格信号在市场后 30% 时间段内激活：
- 比较 Binance 实时价格与市场开始时的价格
- 当差值 > 0.3% 时生成方向性信号
- 将预测赢方的限价调近市价（提高成交率）
- 仍使用 maker 限价单（0% 手续费）

### 策略参数

```bash
python scripts/run_trading.py \
    --target-cost 0.96 \
    --batch-ratio 0.001 \
    --position-size 100.0 \
    --ecr-threshold 1.05
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--target-cost` | 0.96 | 目标成本率（UP_limit + DOWN_limit） |
| `--batch-ratio` | 0.001 | 订单大小 = position_size × batch_ratio |
| `--position-size` | 100.0 | 每市场最大投入（美元） |
| `--ecr-threshold` | 1.05 | ECR 止损阈值 |
| `--disable-ecr-stoploss` | - | 禁用 ECR 止损 |
| `--disable-rebalancing` | - | 禁用市价单再平衡 |
| `--disable-trend-detection` | - | 禁用趋势检测 |
| `--timeframes` | 15m | 交易的时间框架（逗号分隔：15m,1h,4h） |
| `--enable-binance-feed` | - | 启用 Binance 实时价格信号 |
| `--disable-directional-signal` | - | 禁用方向性信号 |

### 其他选项

```bash
# 自定义输出目录
python scripts/run_trading.py -o ./my_results

# 从上次状态恢复
python scripts/run_trading.py --resume

# 设置日志级别
python scripts/run_trading.py --log-level DEBUG

# 使用 YAML 配置文件
python scripts/run_trading.py --config config/strategy_defaults.yaml
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
  "mode": "paper",
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

## 停止交易

### 方法 1：Ctrl+C（推荐）

在运行终端按 `Ctrl+C`，系统会：
1. 取消所有挂单
2. 保存最终状态到 `status.json`
3. 输出最终指标
4. 优雅退出

### 方法 2：Kill Switch 文件

```bash
# 创建 kill switch 文件触发紧急停止
echo "Manual stop" > simulation_results/KILL_SWITCH

# 系统会自动检测文件、取消所有挂单并退出
```

### 方法 3：发送信号

```bash
# 优雅停止
kill <PID>

# Unix: 触发 KillSwitch
kill -USR1 <PID>

# 强制终止（不推荐，可能丢失状态）
kill -9 <PID>
```

---

## 输出文件

所有输出默认保存在 `simulation_results/` 目录：

| 文件 | 说明 |
|------|------|
| `status.json` | 当前状态快照（原子写入，每次指标更新刷新） |
| `metrics.jsonl` | 时间序列指标，周期性追加 |
| `results.jsonl` | 每个市场的最终结算结果 |
| `trades.jsonl` | 每笔成交记录（新架构 TradingRunner） |
| `fill_calibration.jsonl` | 成交校准数据（概率 vs 实际） |
| `archive/` | 历史状态备份 |
| `KILL_SWITCH` | Kill switch 触发文件（自动删除） |

### status.json 示例

```json
{
  "running": true,
  "mode": "paper",
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
  "active_markets": ["btc-updown-15m-1770129900"],
  "market_details": { ... }
}
```

---

## 安全机制

### RiskManager（风控熔断）

在 `.env` 或环境变量中配置：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `RISK_DAILY_LOSS_LIMIT` | 50.0 | 每日最大亏损（USD） |
| `RISK_PER_MARKET_LOSS_LIMIT` | 10.0 | 单市场最大亏损（USD） |
| `RISK_CONSECUTIVE_LOSS_PAUSE` | 5 | 连续亏损 N 次后暂停 |
| `RISK_PAUSE_DURATION_SECONDS` | 1800 | 暂停时长（默认 30 分钟） |
| `RISK_MAX_TOTAL_EXPOSURE` | 500.0 | 总持仓上限（USD） |

### KillSwitch（紧急停止）

触发方式：
1. **文件触发**：创建 `simulation_results/KILL_SWITCH` 文件
2. **信号触发**：`kill -USR1 <PID>`
3. **编程触发**：`kill_switch.activate("reason")`

触发后系统自动：取消所有挂单 → 保存状态 → 退出

### AlertManager（告警）

配置 Discord/Telegram webhook 接收告警：

```bash
# .env
ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
ALERT_ENABLE_ALERTS=true
```

告警事件：每日亏损接近限额、市场结算（显著 PnL）、熔断触发、Kill Switch 激活。

---

## AI 集成

### 检查运行状态

```bash
python -m polymoney.simulation.status_cli --json
```

**解析返回值：**
- `running: true` → 交易正在运行
- `running: false` → 已停止
- `mode: "paper"` / `"live"` → 当前运行模式

### 获取性能指标

```bash
python -m polymoney.simulation.status_cli --json | jq '.stats'
```

**关键指标：**
- `total_pnl`: 总盈亏（美元）
- `total_roi`: 总回报率
- `sharpe_ratio`: 夏普率
- `win_rate`: 胜率

### 示例：AI 监控脚本

```python
import json
import subprocess

def check_simulation_status():
    """检查交易状态并返回关键指标。"""
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
        "mode": status.get("mode", "unknown"),
        "pnl": stats.get("total_pnl", 0),
        "roi": stats.get("total_roi", 0),
        "sharpe": stats.get("sharpe_ratio", 0),
        "markets": stats.get("markets_processed", 0),
    }

status = check_simulation_status()
print(f"Mode: {status['mode']}, PnL: ${status['pnl']:.2f}, Sharpe: {status['sharpe']:.2f}")
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

### 问题：实盘模式启动失败

**症状：** `RuntimeError: Live trading requires POLYMARKET_PRIVATE_KEY`

**解决方案：**
1. 确认 `.env` 文件存在且包含 `POLYMARKET_PRIVATE_KEY`
2. 确认 `py-clob-client` 已安装：`pip install py-clob-client`
3. 确认钱包有足够的 Polygon USDC 余额

### 问题：模拟崩溃

**症状：** 进程意外退出

**解决方案：**
1. 使用 `--resume` 从上次状态恢复
2. 增加日志级别 `--log-level DEBUG`
3. 检查输出目录中的 `status.json` 和日志

### 问题：状态文件损坏

**症状：** `status.json` 无法解析

**解决方案：**
1. 删除 `status.json`
2. 重新启动（不使用 `--resume`）
3. 检查 `archive/` 目录中的历史状态

### 问题：实盘亏损但纸上交易盈利

**症状：** 模拟交易显示盈利（ECR < 1.0，高胜率），但实盘交易频繁亏损（ECR > 1.0）

**根本原因（2026-02-23 诊断）：**

1. **订单生命周期不一致**：`SimulatedExecutor` 在 30 秒后超时取消订单、市场偏移 20% 后取消陈旧订单；`LiveExecutor` 之前缺少这些机制，订单会无限期挂着。
2. **不对称成交**：模拟的概率成交模型对双方给予接近均等的成交概率，但真实市场是定向的 — 上涨时 DOWN 容易成交（价格下降到限价），UP 难以成交（价格远离限价），导致单边持仓。
3. **价格偏离后仍持有订单**：没有超时/陈旧检测，资金被锁定在远离市场价的订单中，市场回摆时以不利价格成交。
4. **紧急定价过于激进**：`urgency_price_factor=0.50` 将限价推得太接近市场价，减少了安全余量。

**已修复：**
- `LiveExecutor` 现在实现了与 `SimulatedExecutor` 一致的超时（30s）和陈旧订单（20%偏移）取消
- 降低 `urgency_price_factor` 从 0.50 → 0.25，保持更大的折扣余量
- 收紧 `sequential_max_gap_ratio` 从 0.30 → 0.20，更早阻止不平衡

**监控建议：**
- 实盘运行时关注日志中的 `Live order timeout` 和 `Live order stale` 事件
- 检查每个市场的 ECR（应 < 1.0）和 balance_ratio（应 > 0.70）
- 如果 ECR 持续 > 1.0，检查是否一侧成交率远高于另一侧

---

## 模拟与实盘差异说明

纸上交易（SimulatedExecutor）和实盘交易（LiveExecutor）的关键差异：

| 特性 | 纸上交易 | 实盘交易 |
|------|---------|---------|
| 成交模型 | 校准概率（2-15%基础，触价60-85%）+ EMA趋势惩罚 | 真实 CLOB 挂单撮合 |
| 最小订单 | 强制 min_order_shares=5（匹配 Polymarket） | Polymarket API 强制 |
| 价格精度 | 自动 clamping 0.01~0.99 + round(2) | Polymarket API 强制 |
| 订单超时 | 30 秒自动取消 | 30 秒自动取消 |
| 陈旧检测 | 市场偏移 >20% 取消 | 市场偏移 >20% 取消 |
| 成交对称性 | EMA 趋势惩罚模拟不对称成交 | 受市场方向影响，一侧倾向成交 |
| 手续费 | 官方公式 0.25*(p*(1-p))^2（15m/5m），零费（1h/4h） | maker 0% / taker 最高 1.56% |
| 成交验证 | 即时 | get_order() + get_trades() 双重确认 |
| ECR 预测 | 仅基于已成交仓位（不含 pending） | 同上 |
| 硬止损 | 弃市模式（ECR>1.02 + 不平衡 + 过半时间） | 同上 |

**注意：** 纸上交易的盈利能力应被视为策略的**理论上限**，实盘表现通常低于此值。差距主要来源于不对称成交和市场微观结构。

---

## 不对称成交（Asymmetric Fills）

### 问题本质

在二元预测市场中，仓位套利需要同时买入 UP 和 DOWN 代币，使总成本 < $1。但在有方向性趋势的市场中：

- **上涨趋势**：DOWN 代币价格下降，我们的 DOWN 限价单容易成交（市场价朝向限价移动）；UP 代币价格上升，我们的 UP 限价单很难成交（市场价远离限价）
- **结果**：单侧持仓积累 → 无对冲 → 结算时亏损

这是实盘亏损的**头号原因**。

### 三层防护体系

系统从三个层面协同解决不对称成交问题：

#### 层 1：模拟器真实性（SimulatedExecutor）

模拟器使用**双层方向性惩罚模型**，使回测结果更贴近真实成交：

| 层次 | 机制 | 作用 |
|------|------|------|
| 瞬时层 (Layer 1) | 单 tick 价格变化 `price_delta` | 捕捉突发波动 |
| 趋势层 (Layer 2) | 价格变化的 EMA（α=0.15） | 捕捉持续方向偏移 |

- 趋势 EMA > +0.003：惩罚系数 = `max(0.10, 1.0 - ema * 15.0)`
- 趋势 EMA < -0.003：成交概率提升（市场朝向限价移动）

这确保了回测中如果一侧趋势性上涨，该侧的买入限价单成交概率会持续走低，从而暴露出不对称成交风险。

#### 层 2：成交率自适应定价（Strategy）

策略层实时追踪每侧的成交率 EMA，动态调整限价分配：

```
成交率偏斜 = (UP成交率 - DOWN成交率) / (UP成交率 + DOWN成交率)
```

- **偏斜 > 0**（UP 容易成交）：收紧 DOWN 限价（更接近市价），放宽 UP 限价（更大折扣）
- **偏斜 < 0**（DOWN 容易成交）：反向调整

关键约束：**价格偏斜不改变总成本**（up_limit + down_limit 不变），只是重新分配折扣。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enable_fill_rate_skew` | true | 启用成交率自适应定价 |
| `fill_rate_ema_alpha` | 0.15 | 成交率 EMA 平滑因子 |
| `fill_rate_skew_max` | 0.40 | 最大偏斜幅度 |

#### 层 3：动态顺序门控（Strategy）

`sequential_max_gap_ratio` 控制两侧允许的最大不平衡。在检测到成交不对称时**自动收紧**：

```
effective_gap_ratio = base_ratio × max(0.25, 1.0 - fill_skew)
```

| 成交偏斜 | 有效间隔比 | 效果 |
|----------|-----------|------|
| 0%（均衡） | 20% | 正常门控 |
| 20%（轻微偏斜） | 16% | 稍微收紧 |
| 40%（严重偏斜） | 12% | 大幅收紧，几乎阻止领先侧下单 |

### 配合机制

以上三层与已有的以下机制协同：

- **ECR 预测**：每个订单下单前计算"如果成交后 ECR 会是多少"，拒绝会恶化 ECR 的订单
- **ECR-aware counterpart cap**：一侧已成交后，限制另一侧限价 ≤ effective_target - avg_filled_price
- **超时 + 陈旧取消**：30s 超时 + 20% 市场偏移取消（LiveExecutor 和 SimulatedExecutor 一致）
- **卖出再平衡**：当不平衡严重时，主动卖出多余侧的仓位恢复平衡

---

## 最佳实践

1. **先用纸上交易验证**：先运行 `trade --mode paper` 确认策略表现
2. **实盘从小额开始**：`--position-size 10` 开始，逐步放量
3. **使用固定时长**：建议设置 `--duration`，避免无限运行
4. **配置告警**：设置 Discord webhook 获取实时通知
5. **定期检查状态**：使用 `status_cli` 监控
6. **备份数据**：定期备份 `simulation_results/` 目录
7. **保留日志**：使用 `--log-level INFO` 保存足够的调试信息
8. **监控 ECR**：实盘运行时，确保每个市场的 ECR < 1.0
9. **检查成交对称性**：如果一侧持续成交而另一侧不成交，说明市场趋势强烈，策略应自动收紧（sequential ordering）

---

*最后更新：2026-02-24*
