# Position Arbitrage 策略技术文档

**项目**: PolyMoney
**策略**: Position Arbitrage (position-arbitrage)
**更新日期**: 2026-02-14

---

## 目录

1. [策略核心原理](#1-策略核心原理)
2. [系统架构](#2-系统架构)
3. [策略机制详解](#3-策略机制详解)
4. [成交模拟模型](#4-成交模拟模型)
5. [风控体系](#5-风控体系)
6. [配置参数参考](#6-配置参数参考)
7. [成交模型合理性评估](#7-成交模型合理性评估)
8. [已知局限与优化方向](#8-已知局限与优化方向)

---

## 1. 策略核心原理

### 1.1 二元市场结构

Polymarket 的 15 分钟 UP/DOWN 市场是二元期权结构：

- 两个 Token：**UP** 和 **DOWN**
- 结算时获胜方 = $1.00，失败方 = $0.00
- 任意时刻 `UP_price + DOWN_price ≈ $1.00`

### 1.2 套利不等式

同时持有两侧，若**总成本 < 对冲仓位**则无论结果均盈利。

**有效成本率（ECR）**：

$$
ECR = \frac{UP_{cost} + DOWN_{cost}}{\min(UP_{shares}, DOWN_{shares})}
$$

- **ECR < 1.0** → 保证盈利
- **ECR = 1.0** → 盈亏平衡
- **ECR > 1.0** → 存在亏损风险

### 1.3 限价单实现低成本买入

市价之和 ≈ 1.0，直接市价买入不盈利。策略通过限价单以折扣价挂单：

$$
UP_{limit} + DOWN_{limit} = target\_cost = 0.96
$$

利润空间 = 4%。在偏斜市场中使用**比例分配**确保两侧获得相等份额：

```
示例：UP=0.70, DOWN=0.30, pair_budget=$0.20
  UP_cost  = $0.20 × 0.70/1.00 = $0.14  → 0.14/0.672 = 0.208 shares
  DOWN_cost = $0.20 × 0.30/1.00 = $0.06  → 0.06/0.288 = 0.208 shares
  hedged = 0.208, ECR = $0.20 / 0.208 = 0.96 ✓
```

等金额分配（各 $0.10）则产生不等份额（UP=0.149, DOWN=0.347），ECR=0.20/0.149=1.34。

---

## 2. 系统架构

### 2.1 组件关系

```
CLI (scripts/run_trading.py)
  └── TradingRunner
        ├── 市场扫描循环 (HTTP, 每60s)
        │     └── RealDataFetcher → Polymarket REST API
        ├── WebSocket 价格流 (实时)
        │     └── WebSocketManager → Polymarket WS
        ├── 结算检测循环 (每5s, 3次确认)
        ├── Orderbook 刷新循环 (HTTP, 每15s)
        └── 指标输出循环 (每1s)
              └── status.json / metrics.jsonl / results.jsonl

LiveRunner 管理多个 MarketSimulation 实例：
  MarketSimulation
    ├── PositionArbitrageStrategy (策略逻辑)
    │     ├── TrendDetector (时间窗口动量)
    │     ├── InternalPosition × 2 (UP/DOWN)
    │     └── LimitOrder[] (内部挂单跟踪)
    ├── OrderbookSnapshot × 2 (UP/DOWN 深度缓存)
    └── pending_orders[] (Runner 侧挂单跟踪)
```

### 2.2 数据流

```
Polymarket WS
  │
  ├── token_price 事件 → 垃圾过滤(spread>50%) → 更新 MarketSimulation 价格
  ├── token_trade 事件 → 更新价格
  └── token_orderbook 事件 → 解析为 OrderbookSnapshot → 缓存

价格更新触发：
  1. WS 预热门控（两侧都需确认真实价格）
  2. Tick 冷却（≥1秒间隔才生成新订单）
  3. strategy.on_price_update() → 返回 OrderSignal[]
  4. 每个 signal → _submit_order() → 深度成交模拟
  5. _check_fills() → 检查 pending 订单成交/超时/陈旧
  6. _sync_pending_orders() → 清理幽灵订单
```

### 2.3 单市场生命周期

```
HTTP 扫描发现 → 验证（时间充足 + 偏斜 ≤ 75%）→ 创建 MarketSimulation
  → 订阅 WS + 拉取初始 Orderbook → WS 预热（等待双侧确认）
  → 正常交易（Phase 1→2→3）→ 价格检测结算（3次确认）
  → finalize() 计算 PnL → 取消 WS 订阅 → 记录结果
```

---

## 3. 策略机制详解

### 3.1 三阶段交易模型

| 阶段 | 时间 | 行为 | ECR 规则 |
|------|------|------|----------|
| Phase 1 | 0 ~ 300s | 激进建仓，双侧下单 | 渐进式上限 1.5→1.1（`_get_phase1_ecr_limit`） |
| Phase 2 | 300s ~ 600s | 稳健优化，ECR 保护 | 不恶化超过 current_ecr + 2% |
| Phase 3 | 600s ~ 结算 | 仅改善 ECR 的订单 | ECR < 1.0 时停止下单保护利润 |

### 3.2 限价计算（`_calculate_limit_prices`）

限价总和恒等于 `target_cost`，差别在于折扣如何在两侧间分配：

**对称模式**（`max_price ≤ trend_patience_threshold`，默认 65%）：

```
scale = target_cost / price_sum
up_limit = up_price × scale
down_limit = down_price × scale
```

两侧获得相同百分比折扣。另外：
- 趋势检测激活时（confidence > 30%），轻微偏移向趋势方向
- 紧迫度定价激活时，根据时间/失衡紧迫度缩小限价与市价距离

**趋势跟随模式**（`trend_patience_threshold < max_price ≤ max_skew_threshold`）：

```
skew_intensity = (max_price - 0.65) / (0.85 - 0.65)  // 0→1
dominant_share = 0.50 - 0.30 × skew_intensity         // 50%→20% 折扣份额

趋势侧: 小折扣 → 紧限价 → 微回调即成交
逆势侧: 大折扣 → 宽限价 → 等大幅回调
```

示例（UP=70%, target=0.96）：

| 模式 | UP 限价 | UP 折扣 | DOWN 限价 | DOWN 折扣 |
|------|---------|---------|-----------|-----------|
| 对称 | 0.672 | 4.0% | 0.288 | 4.0% |
| 趋势跟随 | 0.692 | 1.2% | 0.268 | 10.7% |

**完全停止**（`max_price > max_skew_threshold`，默认 85%）：
不生成任何新限价单。已有仓位的再平衡不受影响。

### 3.3 订单生成循环（`on_price_update`）

每次价格更新时，按以下步骤处理：

```
1. 验证价格有效性（>0, sum ≈ 1.0）
2. 更新趋势检测器
3. 更新风险状态 + 阶段转换日志
4. ECR 平衡规则：ECR > threshold 且失衡 > 5% → 设置 _ecr_recovery_side
5. 再平衡检查（双侧都有仓位 + balance < 50% + 冷却已过）
6. 偏斜保护（85% 阈值 + 5% 迟滞带）
7. 计算限价和可用预算
8. 循环生成订单（最多 max_orders_per_tick=2 个）：
   - 确定主侧（落后）和副侧（领先）
   - 按价格比例分配订单金额（pair_budget × cost_ratio）
   - 每侧最多 max_pending_per_side=2 个挂单
   - ECR recovery 约束：只允许少数侧
   - _should_place_order() 最终验证
```

### 3.4 ECR 保护（`_should_place_order` 及子方法）

订单级最终守门函数，已拆分为 8 个聚焦子方法，按优先级链式调用：

```
_should_place_order(side, limit_price, order_cost)
  │
  ├── 1. _check_order_basics() ─── 价格/金额有效性
  │     → limit_price ≤ 0, order_cost ≤ 0, limit > market → False
  │
  ├── 2. _check_imbalance_guard() ─── 超重侧硬护卫
  │     → overweight > 3× → 阻止超重侧
  │
  ├── 3. _check_initial_position() ─── 无持仓首单
  │     → 模拟双边 balanced_ecr > Phase1 上限? → False
  │     → predicted_ecr ≥ 阶段阈值? → False
  │
  ├── 4. _check_recovery_mode() ─── 严重失衡恢复
  │     → balance < 50% + 少数侧 → True
  │     → balance < 50% + 多数侧 → False
  │     → 不适用 → None (继续)
  │
  ├── 5. _check_recovery_exemption() ─── balance rule 预批准
  │     → Phase 3 + 已盈利 → False
  │     → predicted ≤ current + 2% → True
  │     → 不适用 → None (继续)
  │
  ├── 6. _check_ecr_protection() ─── 阶段性 ECR 保护
  │     → Phase 1: predicted ≥ 渐进上限 → False
  │     → Phase 2-3: 恶化超 2% 或 ≥ 100% → False
  │
  ├── 7. _check_phase3() ─── 结算保护
  │     → ECR < 1.0 → 锁定利润
  │     → predicted ≥ current → 拒绝
  │     → 非落后侧 → 拒绝
  │
  └── 8. _check_imbalance_preference() ─── Phase 1-2 偏好
        → 失衡 > 10%: 落后侧允许, 趋势方向允许, 其他暂停
        → 平衡: 双侧允许
```

### 3.5 再平衡机制（`_generate_rebalancing_order`）

当 `balance_ratio < severe_imbalance_threshold`（0.50）时触发：

- 使用**激进限价单**（市价 -0.2%），作为 maker 享受 **0% 费用**
  - 旧方案：市价 +1% 的 taker 单，需付最高 1.56% 费用
  - 新方案：微折扣限价单，在 micro-dip 时成交（通常 1-3 秒）
  - 若不立即成交，进入 pending 队列，与普通订单一样管理
- 目标恢复到 85% 平衡
- 最小 `min_order_shares`（5 shares）保证满足交易所最小订单要求
- 订单大小受 `market_order_size_cap`（10%）限制
- 经过 ECR 预测检查，拒绝会恶化 ECR 的再平衡
- 执行和拒绝都有冷却期（`rebalancing_cooldown`=30s）
- 订单加入策略 pending 列表，预算和 ECR 预测正确计入

---

## 4. 成交模拟模型

### 4.1 三层降级架构

```
_simulate_fill_with_depth(side, size, limit_price, market_price)
│
├── 有有效 OrderbookSnapshot（< 60s）?
│   ├── 订单簿与市价偏离 > 15%? → 放弃（陈旧），fallback
│   └── simulate_buy_fill() 逐档遍历 ask 侧
│       ├── 全部成交 → return (avg_price, filled_size)
│       ├── 部分成交 → return (avg_price, partial_size)
│       └── 无可成交档位 → return None (→ pending)
│
├── 有 WS/orderbook spread 数据?
│   └── spread_tolerance = max(real_spread, 0.5%)
│
└── 无任何数据 → spread_tolerance = 3% (保守 fallback)

Spread 模型判定：
  limit ≥ market → 市价成交(taker) + 微滑点 + taker费用
  price_diff ≤ spread_tolerance → 概率性成交（20%~90%）
  否则 → pending
```

### 4.1.1 Spread 模型概率性成交

无深度数据时，限价单不再 100% 即时成交，而是按概率判定：

```
proximity = 1 - (price_diff / spread_tolerance)   // 0（边缘）→ 1（市价处）
base_probability = 0.20 + 0.70 × proximity        // 20%（边缘）→ 90%（市价处）
size_penalty = max(0.5, 1.0 - (size-50) × 0.002)  // > 50 shares 有折损
fill_probability = base_probability × size_penalty
```

这避免了旧模型"spread 内全成交"的过度乐观。

### 4.1.2 Taker 费用模型

Polymarket 15 分钟加密市场的费用结构：

| 角色 | 费用 | 说明 |
|------|------|------|
| **Maker**（限价单挂在簿上） | **0%** | 另外获得每日 20% 费用返佣 |
| **Taker**（吃单穿越价差） | 最高 **1.56%** | 有效费率随价格变化，50¢ 处最高 |

费率公式近似（误差 < 0.1%）：

```
effective_fee_rate(p) = 1.56% × (4 × p × (1-p)) ^ 1.8
```

| 价格 | 有效费率 | 100 shares 费用 |
|------|----------|-----------------|
| $0.10 | 0.20% | $0.02 |
| $0.30 | 1.10% | $0.33 |
| $0.50 | 1.56% | $0.78 |
| $0.70 | 1.10% | $0.77 |
| $0.90 | 0.20% | $0.18 |

**策略影响**：
- 正常限价单（maker）：**无费用**，ECR 不受影响
- 再平衡单（maker，市价 -0.2%）：**无费用**，与正常限价单相同
- Taker 费用仅在 `limit_price ≥ market_price` 时触发（`_execute_fill` 的 `is_taker` 标记）
- 当前策略所有订单均为 maker，不产生 taker 费用

### 4.2 Orderbook 深度模拟

`OrderbookSnapshot.simulate_buy_fill()` 的关键行为：

1. **逐档遍历**：从最低 ask 价格开始，逐档消耗流动性直到填满订单或超过 limit_price
2. **深度消耗（deplete=True）**：每次成交后从快照中移除已消耗的流动性，防止同一深度被重复使用
3. **部分成交**：当流动性不足时，已成交部分立即执行，剩余转为 pending
4. **微噪声**：±0.05% 随机价格偏移模拟真实市场噪声

### 4.3 Orderbook 数据来源

| 优先级 | 来源 | 频率 | 说明 |
|--------|------|------|------|
| 1 | WebSocket `token_orderbook` | 实时推送 | 最精确，直接更新缓存 |
| 2 | HTTP `get_orderbook()` | 每 15s 轮询 | WS 缺失/过期时补充 |
| 3 | Spread 模型 | 每次价格更新 | 无订单簿时的最终兜底 |

### 4.4 防护机制

| 机制 | 说明 |
|------|------|
| WS 垃圾过滤 | spread > 50% 的数据在源头丢弃 |
| WS 预热门控 | 两侧都需确认有效 WS 价格后才允许生成订单 |
| Tick 冷却 | 订单生成间隔 ≥ 1 秒（WS 每秒 20+ 更新） |
| 深度陈旧检测 | 订单簿 best_ask 与 WS 价格偏离 > 15% 时放弃深度模型 |
| 深度消耗 | 成交后扣减流动性，防止同一深度被重复消耗 |
| Stale 订单清理 | 市价偏离 > 20% 时撤单 |
| 超时撤单 | 挂单 > 30s 自动取消释放预算 |

### 4.5 持仓同步

系统维护两套 pending 记录，通过唯一订单 ID 同步：

| 位置 | 用途 |
|------|------|
| `MarketSimulation.pending_orders` (Runner) | 成交引擎判定 |
| `Strategy.pending_orders` (策略) | ECR 预测 + 预算计算 |

`_link_order_id()` 将 Runner 的唯一 ID 同步到策略侧。`_sync_pending_orders()` 在每次价格更新后强制对齐，清理幽灵订单。

---

## 5. 风控体系

### 5.1 防御层级总览

```
市场入场 → _is_market_valid()
├── Layer 0: 入场偏斜过滤 (max_price > 75% → 拒绝进入)
├── 时间检查 (< 5min 剩余 → 跳过)
└── 结果已决 (min_price < 5% → 跳过)

WS 数据 → _on_ws_event()
├── Layer 0.5: 垃圾数据过滤 (spread > 50%)

价格更新 → on_price_update()
├── Layer 1: 价格有效性 (sum ∈ [0.9, 1.1], price > 0)
├── Layer 2: ECR 平衡规则 (ECR > threshold → 仅少数侧)
├── Layer 3: 再平衡检查 (balance < 50%, 双冷却)
├── Layer 4: 市场偏斜保护 (85%, 5% 迟滞带)
├── Layer 5: 预算检查 (available < min_order_cost)
└── 订单生成循环 (max 2/tick, max 2 pending/side)
    └── Layer 6: _should_place_order()
        ├── 仓位失衡护卫 (> 3× → 阻止超重侧)
        ├── 严重失衡恢复 (balance < 50% → 仅少数侧)
        ├── ECR 预测检查 (各阶段不同规则)
        └── Phase 3 利润保护 + 聚焦滞后侧
```

### 5.2 趋势检测（`TrendDetector`）

基于时间窗口的动量计算：

```
momentum = (current_price - price_at_window_start) / price_at_window_start
confidence = min(1.0, |momentum| / max_momentum_threshold)
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `momentum_window_seconds` | 30s | 动量计算时间窗口 |
| `max_momentum_threshold` | 0.10 | 100% 置信度动量值 |
| `trend_stop_threshold` | 0.70 | 置信度阈值（影响限价调整幅度） |

**作用方式**：趋势检测仅影响限价分配（`_calculate_limit_prices`），不直接阻止订单。在对称模式下，趋势方向限价略微上调以提高成交率。

### 5.3 紧迫度定价

```
time_urgency = elapsed / total_duration               (0→1)
imbalance_urgency = 1 - balance_ratio                  (0→1)
combined = max(time_urgency, imbalance_urgency × 1.5)  (cap 1.0)
offset_reduction = combined × urgency_price_factor      (×0.5)

调整后限价 = 基础限价 + (市价 - 基础限价) × offset_reduction
```

当 `urgency > 0.7` 且存在落后侧时，落后侧限价进一步逼近市价：

```
lagging_limit = min(market × 0.995, limit + (market - limit) × 0.5)
```

---

## 6. 配置参数参考

完整参数见 `config/strategy_defaults.yaml`。以下为关键参数：

### 策略核心

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `target_cost` | 0.96 | UP_limit + DOWN_limit 目标值。4% margin 可吸收 2 笔不对称成交 |
| `batch_ratio` | 0.001 | 订单大小 = position_size × batch_ratio（$100→$0.10/单） |
| `min_order_shares` | 5 | Polymarket 最小订单 shares 数。5 shares × $0.50 = $2.50 |
| `position_size` | 100.0 | 每市场最大投入 |
| `max_orders_per_tick` | 2 | 每次价格更新最多生成 2 个订单 |

### 风控

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ecr_threshold` | 1.05 | ECR 超此值激活少数侧限定 |
| `max_skew_threshold` | 0.85 | 超此值停止一切新限价单 |
| `trend_patience_threshold` | 0.65 | 超此值启用非对称趋势定价 |
| `max_entry_skew` | 0.75 | 入场偏斜上限 |
| `severe_imbalance_threshold` | 0.50 | 触发市价再平衡的余额比 |

### 模拟

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `order_timeout` | 30s | 挂单超时 |
| `stale_order_threshold` | 20% | 市价偏离超此值撤单 |
| `min_trading_time` | 300s | 最少剩余交易时间 |

---

## 7. 成交模型合理性评估

### 7.1 模型与真实市场的差异

| 方面 | 模拟行为 | 真实 Polymarket CLOB | 偏差影响 |
|------|----------|---------------------|----------|
| **成交判定** | 深度模型逐档遍历；无深度时**概率性成交**（20%~90%） | 需要对手方 lift/hit | ✅ 已改善 — 概率模型模拟了等待对手方的不确定性 |
| **流动性** | 静态 REST 快照 + WS 更新 | 实时变化的订单簿 | 快照过期时偏差大（已有 15% 陈旧检测缓解） |
| **深度消耗** | 本地扣减，不知真实对手方行为 | 多个参与者竞争同一流动性 | 模拟中独占流动性，真实中可能被抢先 |
| **部分成交** | 支持 | 支持 | 一致 |
| **滑点** | 逐档遍历 + 微噪声 | 取决于订单簿实际深度 | 大致一致（深度模型准确时） |
| **最小订单** | **5 shares**（`min_order_shares` 参数） | 市场特定（通常 5-15 shares） | ✅ 已对齐 — 默认 5 shares 匹配常见下限 |
| **Maker 费用** | **0%**（与真实一致） | 0% + 每日 20% 返佣 | ✅ 一致（返佣未建模，影响极小） |
| **Taker 费用** | **最高 1.56%**，按价格曲线扣减 shares | 1.56% 峰值，收取 shares | ✅ 已建模 — 再平衡市价单自动扣费 |

### 7.2 残余风险偏差

1. **深度模型成交率仍偏高**：当有有效订单簿快照时，逐档遍历假设簿上流动性全部可用。真实中同一流动性可能被其他参与者抢先消耗。

2. **REST 快照刷新间隔**：15s REST 刷新后深度恢复，模拟中的"流动性恢复"可能快于实际。

3. **Maker 返佣未建模**：策略作为 maker 可获得每日 20% 费用返佣。这是一笔额外收入，模拟偏保守。

4. **概率模型参数需实盘校准**：当前填充概率（20%-90%）基于合理假设，但最优参数需要实盘数据回测确定。

### 7.3 总体评估

经过本次优化，成交模型在以下方面**显著更真实**：

| 改进 | 之前 | 之后 |
|------|------|------|
| 交易费用 | ❌ 未建模 | ✅ Taker 1.56% 峰值，Maker 0% |
| Spread 成交率 | 100% 即时成交 | 20%~90% 概率性成交 |
| Spread fallback | 6%（过宽） | 3%（保守） |
| 最小订单 | $0.10（尘埃级） | 5 shares（~$2.50） |

**建议实盘前的校准方法**：
1. 收集实盘成交记录，对比模拟 vs 实际的 fill rate 和平均成交价
2. 校准概率模型参数（`base_probability` 范围和 `size_penalty` 曲线）
3. 用真实市场的 `min_order_size` API 返回值替代固定 5 shares

---

## 8. 已知局限与优化方向

### 8.1 已完成优化 ✅

| 方向 | 说明 |
|------|------|
| **交易费用纳入** | Taker 费用（最高 1.56%）已建模并应用于再平衡市价单；Maker 免费 |
| **最小订单对齐** | `min_order_shares=5` 参数强制每笔订单 ≥ 5 shares（~$2.50） |
| **Spread fallback 收紧** | 从 6% 降至 3%，并改为概率性成交（20%-90%） |
| **`_should_place_order` 拆分** | 从 ~270 行单函数拆分为 8 个聚焦子方法 |
| **死代码清理** | 移除 4 个未使用方法（~130 行），对齐文档与代码 |
| **target_cost 自适应** | `_get_adaptive_target_cost()` 根据实际 spread 动态调整 target_cost（0.94~0.98），高流动性小折扣，低流动性大折扣 |
| **Phase 1 偏斜感知 ECR 上限** | `_get_phase1_ecr_limit()` 在偏斜市场中按 skew_intensity 收紧 ECR 上限，防止高 ECR 进入 Phase 2 |
| **Phase 3 利润改善** | `_check_phase3()` 在 ECR < 1.0 时允许 lagging 侧进一步改善 ECR 的订单 |
| **动态 min_order_shares** | `on_market_start()` 从 market_info 接收市场级 `min_order_size`，覆盖默认值 |
| **成交延迟模拟** | `_get_fill_delay()` 根据 limit 与 market 的距离计算 1~10s 延迟，防止即时成交 |
| **WS Orderbook 增量** | `apply_incremental_update()` 支持 WS 推送的增量订单簿更新（≤3 level = delta），减少 REST 依赖 |
| **概率模型校准** | `FillCalibrationTracker` 记录每次 spread 模型成交判定的概率与结果，导出 JSONL + Brier score |
| **流动性竞争** | `LiquidityCompetitionTracker` 跟踪跨市场成交活动，多市场同时活跃时降低成交概率（最多 40% 惩罚） |
| **统一 pending 跟踪** | `LimitOrder` 作为 Strategy 和 Runner 的单一数据源，消除幽灵订单和同步问题 |

### 8.2 实盘交易架构（Live Trading Roadmap）

#### 8.2.1 当前状态与差距

系统已完成从 `LiveRunner` 单体到模块化 `TradingRunner` 的重构。纸上交易和实盘交易共用同一代码路径，仅通过 `OrderExecutor` 实现切换。

| 组件 | 状态 | 说明 |
|------|------|------|
| 市场数据（REST + WS） | ✅ 已实现 | `MarketDataProvider` — 独立数据层 |
| 成交模拟（深度 + Spread） | ✅ 已实现 | `SimulatedExecutor` — 提取自 MarketSimulation |
| 策略逻辑（3 阶段 + ECR） | ✅ 已完成 | `PositionArbitrageStrategy` 不变 |
| 轻量市场上下文 | ✅ 已实现 | `MarketContext` — 替代 MarketSimulation |
| CLOB 客户端集成 | ✅ 已实现 | `TradingRunner._init_clob_client()` 从 .env 初始化 |
| 真实下单 / 撤单 | ✅ 已实现 | `LiveExecutor` — 通过 ClobClient 执行 |
| 成交确认与对账 | ✅ 已实现 | `FillManager` — 轮询订单状态 + 仓位对账 |
| 风控（熔断 / Kill Switch） | ✅ 已实现 | `RiskManager` + `KillSwitch` |
| 监控告警 | ✅ 已实现 | `AlertManager` — Discord/通用 Webhook |
| 顶层编排器 | ✅ 已实现 | `TradingRunner` — 替代 LiveRunner |

#### 8.2.2 目标架构

```
TradingRunner（顶层编排）
  ├── MarketDataProvider（数据层）
  │     ├── RealDataFetcher → Polymarket REST API
  │     ├── WebSocketManager → Polymarket WS
  │     └── OrderbookManager（深度缓存 + 增量更新）
  │
  ├── StrategyEngine（策略层）
  │     ├── MarketContext × N（市场状态 + 策略实例）
  │     │     └── PositionArbitrageStrategy
  │     └── OrderExecutor（执行抽象）
  │           ├── SimulatedExecutor（纸上交易：深度 / Spread 模型）
  │           └── LiveExecutor（实盘：ClobClient）
  │                 └── FillManager（成交跟踪 + 仓位对账）
  │
  ├── RiskManager（安全层）
  │     ├── KillSwitch（紧急停止）
  │     ├── CircuitBreaker（每日亏损 / 连续亏损熔断）
  │     └── PositionReconciler（本地 vs 链上仓位对比）
  │
  └── Observability（可观测性）
        ├── MetricsLogger（status.json / metrics.jsonl / results.jsonl）
        ├── TradeLogger（trades.jsonl + DataStorage）
        └── AlertManager（Webhook 告警）
```

数据流：

```
MarketDataProvider
  │
  ├── on_market_discovered → TradingRunner._on_market_discovered()
  │     → 创建 MarketContext + 注册 executor + 订阅 WS
  │
  ├── on_price_update → TradingRunner._on_price_update()
  │     → MarketContext.process_price_update() → OrderSignal[]
  │     → RiskManager.can_trade() → SimulatedExecutor/LiveExecutor.submit_order()
  │     → executor.check_fills() → MarketContext.apply_fill()
  │
  ├── on_orderbook_update → OrderExecutor.update_orderbook()
  │     → SimulatedExecutor 更新深度缓存
  │
  └── on_settlement → TradingRunner._finalize_market()
        → executor.cancel_all() → MarketContext.finalize() → RiskManager.record_result()
```

**启动命令**：

```bash
# 纸上交易（默认）
python scripts/run_trading.py

# 实盘交易
python scripts/run_trading.py --live

# 指定币种和时长
python scripts/run_trading.py -m btc,eth -d 10h

# 自定义仓位
python scripts/run_trading.py --position-size 50
```

#### 8.2.3 实施阶段

| 阶段 | 内容 | 状态 | 文件 |
|------|------|------|------|
| Phase 2 | `OrderExecutor` 接口 + `SimulatedExecutor` + `LiveExecutor` | ✅ 完成 | `polymoney/execution/` |
| Phase 3 | `MarketDataProvider`（市场发现、WS、订单簿、结算） | ✅ 完成 | `polymoney/data/market_data_provider.py` |
| Phase 4 | `MarketContext`（剥离执行逻辑，保留策略+状态） | ✅ 完成 | `polymoney/strategy/market_context.py` |
| Phase 5 | `TradingRunner` 顶层编排器 + `scripts/run_trading.py` | ✅ 完成 | `polymoney/runner.py` |
| Phase 6 | `ClobClient` 初始化、Token ID 映射、钱包验证 | ✅ 完成 | `runner.py._init_clob_client()` |
| Phase 7 | `FillManager` 实盘成交跟踪（轮询 + 对账） | ✅ 完成 | `polymoney/execution/fill_manager.py` |
| Phase 8 | `RiskManager` + `KillSwitch` + 熔断 | ✅ 完成 | `polymoney/risk/` |
| Phase 9 | `AlertManager` Webhook 告警 | ✅ 完成 | `polymoney/monitoring/` |
| Phase 10 | 验证上线（小额 dry run + 校准 + 渐进放量） | 🔜 待执行 | — |

#### 8.2.4 关键组件设计

**OrderExecutor 接口**（`polymoney/execution/executor.py`）：

```python
class OrderExecutor(ABC):
    async def submit_order(market_id, side, token_id, price, size, is_taker) -> OrderResult
    async def cancel_order(order_id) -> bool
    async def check_fills(market_id, price_data) -> List[FillEvent]
    async def cancel_all(market_id) -> int
```

**SimulatedExecutor**：继承现有 `MarketSimulation` 的全部成交逻辑：
- `OrderbookSnapshot.simulate_buy_fill()` 深度逐档遍历
- Spread 概率模型（20%-90%）
- 成交延迟模拟（`_get_fill_delay`）
- Taker 费用模型
- `FillCalibrationTracker` + `LiquidityCompetitionTracker`

**LiveExecutor**：包装 `py-clob-client`：
- `ClobClient.create_order()` + `post_order()` 下单
- `ClobClient.cancel()` 撤单
- Token ID 解析：`(market_id, side)` → 真实 Polymarket token ID

**RiskManager 安全规则**：
- 每日最大亏损限制（默认 $50）
- 单市场最大亏损（默认 $10）
- 连续亏损熔断（5 连亏 → 暂停 30 分钟）
- 总持仓硬上限（对照真实余额检查）
- CLOB API 速率限制（10 req/s）

**KillSwitch 触发方式**：
- SIGINT / SIGTERM → 优雅停止（撤销所有挂单）
- 触摸文件 `simulation_results/KILL` → 紧急停止
- API 端点 `POST /api/kill` → 远程停止

#### 8.2.5 实盘残余风险

| 风险 | 说明 | 缓解措施 |
|------|------|----------|
| 深度模型成交率偏高 | 模拟中独占流动性，真实中可能被抢先 | 实盘校准 fill rate |
| REST 快照刷新间隔 | 15s 刷新后深度恢复可能快于实际 | 增加 WS 增量更新权重 |
| API 故障 / 超时 | 下单失败可能导致单侧暴露 | 指数退避重试 + 仓位对账 |
| 钱包余额不足 | 余额检查与下单之间的竞态条件 | 预留 10% 安全边际 |
| 结算检测延迟 | WS 价格确认需 3 次连续检查 | 结合 HTTP 轮询双重确认 |

### 8.3 未来优化方向

| 方向 | 说明 | 优先级 |
|------|------|--------|
| **实盘概率校准** | 收集 `fill_calibration.jsonl` 数据，用 Brier score 优化 base_probability / size_penalty 参数 | P1 |
| **Polymarket min_order_size API** | 当 Polymarket API 暴露 per-market minimum 时，在 `MarketEventData` 中填充实际值 | P2 |
| **Maker 返佣建模** | Maker 每日 20% 费用返佣未建模；纳入可提高利润预估准确性 | P3 |

---

*文档结束*
