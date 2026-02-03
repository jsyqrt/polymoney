# Polymarket 深度研究报告

> 本文档为面向开发者和交易研究者的 Polymarket 综合指南，涵盖平台历史、技术架构、市场特征、交易策略及 API 开发等内容。

---

## 目录

1. [平台概述与历史](#1-平台概述与历史)
2. [技术架构](#2-技术架构)
3. [市场类型与特征](#3-市场类型与特征)
4. [开户与入金](#4-开户与入金)
5. [下单机制与费用](#5-下单机制与费用)
6. [风险管理](#6-风险管理)
7. [手动交易策略](#7-手动交易策略)
8. [量化交易策略](#8-量化交易策略)
9. [API 开发指南](#9-api-开发指南)
10. [风险与注意事项](#10-风险与注意事项)
11. [竞品对比](#11-竞品对比)
12. [附录与资源](#12-附录与资源)

---

## 1. 平台概述与历史

### 1.1 什么是 Polymarket

Polymarket 是一个基于区块链的去中心化预测市场平台，用户可以对未来事件的结果进行交易。平台使用 USDC 稳定币作为交易媒介，在 Polygon 网络上运行，提供政治、经济、体育、加密货币等多种类型的预测市场。

**核心理念**：预测市场通过聚合分散的信息，将交易价格转化为事件发生的风险中性概率。研究表明，预测市场通常比专家预测和民意调查更准确。

### 1.2 发展历史

| 时间 | 事件 |
|------|------|
| 2020年 | Shayne Coplan 创立 Polymarket，总部位于纽约曼哈顿 |
| 2020年10月 | 完成 400 万美元种子轮融资，投资方包括 Polychain Capital、Naval Ravikant 等 |
| 2022年1月 | 与 CFTC 达成和解，被指控运营未注册的衍生品交易平台，开始禁止美国用户访问 |
| 2024年 | 美国总统大选期间交易量激增，创下 36 亿美元历史记录 |
| 2025年12月2日 | 在 Trump 政府下恢复对美国用户开放，Donald Trump Jr. 加入顾问团队 |
| 2025年12月 | 周交易量突破 12.5 亿美元，打破历史记录 |

### 1.3 当前规模

- **月访问量**：约 2000 万次（2025年11月数据）
- **活跃市场**：10,000+ 个
- **2024年选举交易量**：超过 36 亿美元
- **主要竞争对手**：Kalshi（受 CFTC 监管）、PredictIt

---

## 2. 技术架构

### 2.1 区块链基础设施

Polymarket 构建在 **Polygon 网络**（以太坊 Layer 2）之上，具有以下特点：

- **低 Gas 费用**：每笔交易约 $0.01-$0.50
- **快速确认**：交易通常在几秒内确认
- **ERC-1155 代币标准**：支持批量转账和高效的代币管理

### 2.2 条件代币框架 (CTF)

Polymarket 使用 **Gnosis Conditional Token Framework**，这是一个应用无关的资产类别：

```
核心概念：
- 每个市场对应一组条件代币（如 YES 和 NO 代币）
- 市场结算后，正确结果的代币价值 $1.00，错误结果价值 $0.00
- 支持将 USDC 拆分为条件代币，或将条件代币合并回 USDC
```

**关键操作**：
- **Split（拆分）**：将 USDC 拆分为 YES + NO 代币
- **Merge（合并）**：将 YES + NO 代币合并回 USDC
- **Redeem（赎回）**：市场结算后赎回获胜代币

### 2.3 中央限价订单簿 (CLOB)

Polymarket 使用 **Central Limit Order Book (CLOB)** 作为核心交易引擎：

```
特点：
- 类似传统交易所的订单簿模式
- 支持限价单、市价单等多种订单类型
- 提供 REST API 和 WebSocket 实时数据
- 做市商可获得流动性奖励
```

### 2.4 Gamma 市场结构

Polymarket 引入了 **Gamma Markets** 和 **Neg-Risk CTF Adapter**：

- **Gamma Markets**：支持更复杂的市场结构
- **Neg-Risk Adapter**：处理负风险条件代币的特殊适配器
- **多结果市场**：如"谁将赢得大选"可有多个候选人选项

### 2.5 预言机与结算

使用 **UMA Optimistic Oracle** 进行市场结算：

```
结算流程：
T+0: 市场关闭，授权提议者提交结果（需质押 $750 保证金）
T+2h: 挑战期结束，若无争议则自动接受结果
     提议者获得保证金返还 + $2 奖励

如有争议：
- 争议者也需质押保证金
- UMA 代币持有者在 48-96 小时内投票
- 获胜方获得奖励，失败方失去保证金
```

**注意**：结算期间资金锁定，无争议市场需 2+ 小时，有争议市场需 4-5 天。

---

## 3. 市场类型与特征

### 3.1 主要市场类别

| 类别 | 说明 | 示例 |
|------|------|------|
| **政治** | 选举、政策、任命 | "Trump 是否会任命某人为 Fed 主席" |
| **体育** | 比赛结果、MVP | "超级碗冠军" |
| **加密货币** | 价格预测 | "BTC 今天会上涨吗" |
| **经济指标** | 通胀、利率 | "CPI 是否超过 3%" |
| **娱乐** | 颁奖、流行文化 | "奥斯卡最佳影片" |
| **科技/AI** | 产品发布、突破 | "GPT-5 何时发布" |

### 3.2 加密货币市场详解

Polymarket 提供多种时间框架的加密货币价格预测市场：

**时间框架**：
- **超短期**：15 分钟、1 小时、4 小时
- **日内**：今天上涨/下跌
- **每周**：本周价格区间
- **月度及更长**：月度/季度价格目标

**主要代币覆盖**：
- Bitcoin (BTC)
- Ethereum (ETH)
- Solana (SOL)
- XRP
- Dogecoin (DOGE)

**市场示例（2026年1月数据）**：
```
"BTC 今天会上涨吗？" - 约 51% 概率
"BTC 本周会跌破 $85,000 吗？" - 约 32% 概率
"ETH 1月会跌破 $2,800 吗？" - 约 43% 概率
```

### 3.3 市场特征分析

**二元市场**：
- 只有 YES/NO 两个结果
- 理论上 YES + NO 价格应等于 $1.00
- 价格偏离提供套利机会

**多结果市场**：
- 多个互斥选项（如多个候选人）
- 所有选项价格之和应等于 $1.00
- 组合套利更复杂但利润更高

**流动性特征**：
- 热门市场深度好，点差小（1-2%）
- 冷门市场点差大（5-10%+）
- 流动性在新闻事件时快速变化

---

## 4. 开户与入金

### 4.1 账户类型

Polymarket 支持两种主要钱包类型：

| 类型 | 说明 | 适用场景 |
|------|------|----------|
| **Email/Magic Wallet** | 通过邮件验证的托管钱包 | 普通用户，易用性优先 |
| **浏览器钱包 (EOA)** | MetaMask、Coinbase Wallet 等 | 开发者，需要 API 访问 |
| **代理钱包 (Proxy)** | Polymarket 托管代理 | 两种方式都可使用 |

### 4.2 注册流程

1. **访问 polymarket.com**
2. **选择登录方式**：
   - 邮箱注册（获得 Magic Link）
   - 连接浏览器钱包（MetaMask 等）
3. **完成必要的身份验证**（部分地区需要）

### 4.3 入金方式

#### 方式一：交易所直接提款（推荐）

支持 Polygon USDC 直接提款的交易所：
- **Crypto.com**：手续费约 $1
- **Kraken**：手续费约 $0.50-2
- **OKX**：手续费约 $1

⚠️ **Coinbase 不支持直接 Polygon 提款**

```
步骤：
1. 在交易所购买 USDC
2. 进入提款页面
3. 选择外部钱包地址
4. 【关键】选择 Polygon 网络（不是 Ethereum！）
5. 粘贴 Polymarket 钱包地址
6. 输入金额并完成 2FA 验证
```

#### 方式二：跨链桥接

如果 USDC 在以太坊主网：
1. 使用跨链桥（如 Polygon Bridge）
2. 将 USDC 从 Ethereum 桥接到 Polygon
3. 桥接费用约 $10-30
4. 需要 ETH 作为主网 Gas 费

#### 方式三：Polymarket 内置桥接

- 平台提供内置 Bridge 和 Swap 功能
- 可直接在平台内转换资产

### 4.4 Gas 费准备

```
建议准备：
- 0.01-0.1 MATIC 用于 Polygon Gas 费
- 或使用平台的 Gas 代付服务（如有）

典型 Gas 成本：
- 买卖股份：$0.10-$0.30
- 下单/取消订单：$0.05-$0.20
- 充值/提现：$0.15-$0.50
```

### 4.5 私钥导出（API 开发需要）

如果需要 API 访问，需要导出私钥：
1. 访问 **reveal.polymarket.com**
2. 验证身份
3. 导出私钥（妥善保管！）

---

## 5. 下单机制与费用

### 5.1 订单类型

| 订单类型 | 缩写 | 说明 |
|----------|------|------|
| **Good-Till-Cancelled** | GTC | 限价单，持续有效直到成交或取消 |
| **Good-Till-Date** | GTD | 限价单，有效期至指定时间 |
| **Fill-Or-Kill** | FOK | 市价单，必须立即全部成交，否则取消 |
| **Fill-And-Kill** | FAK | 市价单，立即成交可用部分，剩余取消 |
| **Post-Only** | - | 限价单，只挂单不吃单，会被拒绝如果会立即成交 |

### 5.2 订单参数

```python
OrderArgs(
    price=0.50,      # 价格 (0.01-0.99)
    size=100.0,      # 数量（股份数）
    side=BUY,        # 方向（BUY 或 SELL）
    token_id="xxx"   # 代币 ID（从市场 API 获取）
)
```

**Tick Size 规则**：
- 价格范围 [0.01, 0.99]
- 最小价格单位取决于市场配置（通常 0.01）
- 最小订单大小有市场要求

### 5.3 费用结构

#### 交易费
- **仅对获胜交易收取约 2%**
- 计算基础：结算时的毛收益
- 亏损交易：**零交易费**

```
示例：
投入 $40，赢得 $100 毛收益
交易费 = $100 × 2% = $2
净利润 = $100 - $40 - $2 = $58
```

#### Gas 费（网络费）
| 操作 | Polygon | Ethereum |
|------|---------|----------|
| 买卖股份 | $0.10-$0.30 | $5-$50 |
| 下单/取消 | $0.05-$0.20 | $3-$30 |
| 充值/提现 | $0.15-$0.50 | $5-$30 |

#### 充提费
- **充值费**：$0（免费）
- **提现费**：$0（仅 Gas 费）
- **跨链桥费**：$10-$30（如需要）

### 5.4 费用优化建议

1. **使用 Polygon 网络**而非 Ethereum
2. **批量交易**减少 Gas 次数
3. **使用限价单**控制执行价格
4. **非高峰时段交易**降低 Gas 成本
5. **保持少量 MATIC** 用于 Gas

---

## 6. 风险管理

### 6.1 平台层面

**无仓位限制**：Polymarket 不强制设置最大仓位限制，风险管理完全由交易者自负。

**已发生的大额亏损案例**：
- 单个交易者在一个月内因体育博彩亏损近 1000 万美元
- 某交易者因风险管理不当单次亏损 200 万美元

### 6.2 个人风险管理框架

```
建议的风险控制规则：

1. 单笔仓位上限
   - 不超过账户资金的 5-10%
   - 高风险市场进一步降低

2. 总风险敞口
   - 所有未结仓位不超过账户的 50%
   - 保留足够现金应对机会

3. 止损规则
   - 设定明确的亏损上限
   - 达到阈值时强制平仓

4. 分散化
   - 避免在单一市场过度集中
   - 跨类别分散投资
```

### 6.3 特定风险

| 风险类型 | 说明 | 应对 |
|----------|------|------|
| **流动性风险** | 无法以预期价格平仓 | 关注市场深度，避免大单 |
| **结算风险** | 结算争议导致资金锁定 | 预留时间缓冲 |
| **智能合约风险** | 合约漏洞 | 关注审计报告 |
| **操作风险** | 下错单、网络问题 | 双重确认，备用网络 |
| **监管风险** | 政策变化 | 了解所在地法规 |

### 6.4 资金锁定周期

```
正常结算：
- 市场关闭 → 2 小时挑战期 → 资金释放

有争议结算：
- 市场关闭 → 争议提出 → 48-96 小时投票 → 资金释放
- 总计可能 4-5 天以上
```

---

## 7. 手动交易策略

### 7.1 信息优势策略

**核心理念**：比市场更快或更准确地处理信息

#### 7.1.1 新闻事件交易

```
策略要点：
1. 监控高质量新闻源（路透社、彭博等）
2. 识别与特定市场相关的新闻
3. 快速评估新闻对概率的影响
4. 在市场反应前下单

工具建议：
- 设置新闻提醒
- 使用 Polymarket 价格提醒服务
- 建立新闻-市场映射表
```

#### 7.1.2 专业领域优势

```
适用场景：
- 你是某领域专家（如医学、法律、技术）
- 你能比大众更准确评估专业事件的概率
- 市场存在明显的信息不对称

示例：
- 医生对 FDA 批准概率的判断
- 律师对案件结果的评估
- 工程师对技术发布时间的预测
```

### 7.2 套利策略

#### 7.2.1 市场内套利（二元市场）

```
条件：YES + NO 价格 < $1.00

示例：
YES = $0.45, NO = $0.50
总成本 = $0.95
保证收益 = $1.00 - $0.95 = $0.05 (5.26%)

操作：
同时买入 YES 和 NO 各 100 股
成本 = $95
任意结果收益 = $100
纯利润 = $5（扣除费用前）
```

#### 7.2.2 多结果市场套利

```
条件：所有选项价格之和 ≠ $1.00

示例（3 候选人选举）：
A = $0.30, B = $0.35, C = $0.40
总和 = $1.05 > $1.00

套利操作：
卖出所有选项（如果有持仓）
或在总和 < $1.00 时买入所有选项
```

#### 7.2.3 跨平台套利

```
条件：同一事件在不同平台价格不同

示例：
Polymarket: 特朗普获胜 = $0.55
Kalshi: 特朗普获胜 = $0.62

操作：
在 Polymarket 买入 $0.55
在 Kalshi 卖出 $0.62
锁定 $0.07 无风险利润

注意：
- 需要两个平台都有账户
- 考虑资金成本和转账时间
- 实际执行难度高
```

### 7.3 均值回归策略

```
适用条件：
- 市场因情绪波动产生非理性定价
- 价格明显偏离合理概率估计

策略：
1. 建立基于基本面的"合理价格"模型
2. 当市场价格显著偏离时入场
3. 等待价格回归后获利

风险：
- "市场可以保持非理性的时间比你保持偿付能力的时间更长"
- 需要深厚的领域知识
```

### 7.4 终局策略（Endgame）

```
策略：在市场即将结算时买入高确定性结果

示例：
选举投票已开始计票
候选人 A 领先且胜局已定
市场价格：A = $0.97, B = $0.03

操作：
买入 A @ $0.97
2 天后结算收到 $1.00
收益率 = 3.1%
年化收益率 = 3.1% × (365/2) = 566%

风险：
- "确定"的事情可能翻转
- 需要准确判断结算时间
```

### 7.5 做市策略（手动版）

```
策略：在两边挂单，赚取买卖价差

操作：
1. 在 YES 侧挂买单 @ $0.48
2. 在 YES 侧挂卖单 @ $0.52
3. 如果两边都成交，赚取 $0.04 价差

风险：
- 只有一边成交，持有方向性风险
- 大的市场波动可能导致亏损
- 需要持续监控和调整
```

---

## 8. 量化交易策略

### 8.1 统计套利

#### 8.1.1 市场内价格异常检测

```python
def detect_binary_arbitrage(yes_price, no_price):
    """检测二元市场套利机会"""
    total = yes_price + no_price
    if total < 1.0:
        # 买入套利
        profit = 1.0 - total
        return {
            'type': 'buy_both',
            'profit': profit,
            'action': f'Buy YES@{yes_price} and NO@{no_price}'
        }
    elif total > 1.0:
        # 如果持有头寸，可以卖出套利
        return {
            'type': 'potential_sell',
            'premium': total - 1.0
        }
    return None

def detect_multi_outcome_arbitrage(prices: list):
    """检测多结果市场套利"""
    total = sum(prices)
    if total < 1.0:
        return {
            'type': 'buy_all',
            'profit': 1.0 - total,
            'roi': (1.0 - total) / total
        }
    return None
```

#### 8.1.2 跨市场相关性套利

```python
def correlation_arbitrage(market_a_price, market_b_price,
                         historical_correlation):
    """
    基于市场相关性的套利
    当两个相关市场价格偏离历史关系时交易
    """
    expected_b = predict_price(market_a_price, historical_correlation)
    deviation = market_b_price - expected_b

    if abs(deviation) > threshold:
        if deviation > 0:
            # B 被高估，卖 B 买 A
            return {'action': 'sell_B_buy_A', 'spread': deviation}
        else:
            # B 被低估，买 B 卖 A
            return {'action': 'buy_B_sell_A', 'spread': abs(deviation)}
```

### 8.2 时间序列策略

#### 8.2.1 价格动量

```python
def momentum_signal(prices: list, lookback: int = 20):
    """
    动量策略：追随近期价格趋势
    适用于有持续信息流入的市场
    """
    if len(prices) < lookback:
        return None

    returns = np.diff(np.log(prices[-lookback:]))
    momentum = np.sum(returns)

    if momentum > momentum_threshold:
        return 'BUY'
    elif momentum < -momentum_threshold:
        return 'SELL'
    return 'HOLD'
```

#### 8.2.2 均值回归

```python
def mean_reversion_signal(prices: list, window: int = 50):
    """
    均值回归策略：价格偏离均值时反向交易
    适用于稳定的市场
    """
    if len(prices) < window:
        return None

    mean_price = np.mean(prices[-window:])
    std_price = np.std(prices[-window:])
    current_price = prices[-1]

    z_score = (current_price - mean_price) / std_price

    if z_score > 2:
        return 'SELL'  # 价格过高，预期回落
    elif z_score < -2:
        return 'BUY'   # 价格过低，预期回升
    return 'HOLD'
```

### 8.3 信息优势策略

#### 8.3.1 新闻情感分析

```python
import asyncio
from datetime import datetime

class NewsTrader:
    def __init__(self, polymarket_client, news_api):
        self.client = polymarket_client
        self.news_api = news_api
        self.market_mappings = {}  # 新闻关键词 -> 市场映射

    async def monitor_news(self):
        """监控新闻并快速响应"""
        async for news_item in self.news_api.stream():
            market = self.match_market(news_item)
            if market:
                sentiment = self.analyze_sentiment(news_item)
                impact = self.estimate_impact(news_item, market)

                if abs(impact) > self.threshold:
                    await self.execute_trade(market, sentiment, impact)

    def analyze_sentiment(self, news):
        """分析新闻对事件概率的影响"""
        # 使用 NLP 模型评估
        pass

    def estimate_impact(self, news, market):
        """估计新闻对市场价格的影响"""
        # 基于历史数据和新闻重要性
        pass
```

#### 8.3.2 社交媒体信号

```python
class SocialSignalTrader:
    def __init__(self):
        self.twitter_api = None
        self.sentiment_model = None

    def track_influencers(self, market_id, influencer_list):
        """追踪关键意见领袖的发言"""
        pass

    def aggregate_sentiment(self, market_id, time_window='1h'):
        """聚合社交媒体情感"""
        pass

    def detect_unusual_activity(self, market_id):
        """检测异常社交活动（可能预示新闻）"""
        pass
```

### 8.4 做市策略

#### 8.4.1 基础做市

```python
class SimpleMarketMaker:
    def __init__(self, client, market_id, spread=0.02, size=100):
        self.client = client
        self.market_id = market_id
        self.spread = spread
        self.size = size
        self.inventory = {'YES': 0, 'NO': 0}

    def calculate_quotes(self, fair_value):
        """计算买卖报价"""
        bid = fair_value - self.spread / 2
        ask = fair_value + self.spread / 2
        return bid, ask

    def update_quotes(self, fair_value):
        """更新订单簿上的报价"""
        bid, ask = self.calculate_quotes(fair_value)

        # 取消旧订单
        self.client.cancel_all_orders(self.market_id)

        # 下新订单
        self.client.place_order(
            market_id=self.market_id,
            side='BUY',
            price=bid,
            size=self.size
        )
        self.client.place_order(
            market_id=self.market_id,
            side='SELL',
            price=ask,
            size=self.size
        )

    def manage_inventory(self):
        """库存管理：偏移报价以平衡持仓"""
        net_position = self.inventory['YES'] - self.inventory['NO']

        # 根据净持仓调整报价偏移
        skew = net_position * self.skew_factor
        return skew
```

#### 8.4.2 Polymarket 流动性奖励计算

Polymarket 使用类似 dYdX 的流动性奖励机制：

```python
def calculate_liquidity_score(spread, max_spread, size, is_two_sided=True):
    """
    计算流动性奖励得分

    公式：S(v,s) = ((v-s)/v)^2 * size

    参数：
    - spread: 当前订单距离中间价的价差
    - max_spread: 最大合格价差
    - size: 订单大小
    """
    if spread > max_spread:
        return 0  # 超出最大价差，不计分

    score = ((max_spread - spread) / max_spread) ** 2 * size

    if not is_two_sided:
        score = score / 3.0  # 单边流动性打折

    return score

def estimate_daily_reward(scores, total_market_scores, daily_reward_pool):
    """估算每日奖励"""
    share = scores / total_market_scores
    return share * daily_reward_pool
```

### 8.5 黑天鹅/尾部风险策略

#### 8.5.1 低概率事件狙击

```python
class TailRiskTrader:
    """
    策略：买入市场低估的尾部风险
    """

    def find_mispriced_tails(self, markets):
        """寻找被低估的低概率事件"""
        opportunities = []

        for market in markets:
            market_prob = market['price']
            estimated_prob = self.estimate_true_probability(market)

            # 寻找市场概率 < 5% 但实际概率被低估的情况
            if market_prob < 0.05 and estimated_prob > market_prob * 2:
                opportunities.append({
                    'market': market,
                    'market_prob': market_prob,
                    'estimated_prob': estimated_prob,
                    'expected_value': estimated_prob / market_prob
                })

        return sorted(opportunities,
                     key=lambda x: x['expected_value'],
                     reverse=True)

    def position_sizing(self, opportunity, bankroll, kelly_fraction=0.25):
        """凯利公式头寸计算（保守版）"""
        p = opportunity['estimated_prob']
        b = (1 / opportunity['market_prob']) - 1  # 赔率

        kelly = (p * b - (1 - p)) / b
        position = bankroll * kelly * kelly_fraction  # 1/4 凯利

        return max(0, position)
```

#### 8.5.2 对冲组合策略

```python
class TailHedger:
    """
    策略：使用预测市场对冲组合的尾部风险
    """

    def build_hedge_portfolio(self, risk_exposure, available_markets):
        """
        构建对冲组合

        例如：如果持有大量科技股
        可以买入"经济衰退"、"监管打击"等市场
        """
        hedges = []

        for market in available_markets:
            correlation = self.estimate_correlation(
                risk_exposure,
                market['outcome']
            )

            if correlation < -0.5:  # 负相关 = 对冲
                hedges.append({
                    'market': market,
                    'correlation': correlation,
                    'optimal_size': self.calculate_hedge_size(
                        risk_exposure, correlation, market
                    )
                })

        return hedges
```

### 8.6 加密货币市场专用策略

#### 8.6.1 基于实际价格的预测

```python
class CryptoMarketTrader:
    """
    针对 Polymarket 加密货币价格预测市场的策略
    """

    def __init__(self, polymarket_client, binance_client):
        self.pm = polymarket_client
        self.exchange = binance_client

    def compare_with_spot(self, pm_market, current_price):
        """
        比较 Polymarket 预测与当前现货价格
        """
        target_price = pm_market['target_price']
        direction = pm_market['direction']  # 'up' or 'down'
        pm_probability = pm_market['price']

        # 基于波动率计算概率
        time_to_expiry = pm_market['expiry'] - datetime.now()
        historical_vol = self.calculate_volatility()

        # 使用简化的 Black-Scholes 类似计算
        theoretical_prob = self.calculate_probability(
            current_price, target_price,
            time_to_expiry, historical_vol
        )

        if theoretical_prob > pm_probability + 0.05:
            return 'BUY'
        elif theoretical_prob < pm_probability - 0.05:
            return 'SELL'
        return 'HOLD'

    def arbitrage_with_derivatives(self, pm_market):
        """
        与加密货币期权/期货的跨市场套利
        """
        # 如果 Polymarket 的隐含概率与期权市场差异大
        # 可以进行跨市场套利
        pass
```

#### 8.6.2 短周期市场高频策略

```python
class ShortTermCryptoTrader:
    """
    针对 15 分钟/1 小时加密货币市场的高频策略
    """

    async def monitor_orderbook(self, market_id):
        """监控订单簿变化"""
        async for update in self.ws_client.subscribe_orderbook(market_id):
            imbalance = self.calculate_imbalance(update)
            if abs(imbalance) > self.threshold:
                await self.execute_signal(market_id, imbalance)

    def calculate_imbalance(self, orderbook):
        """计算买卖压力失衡"""
        bid_volume = sum([o['size'] for o in orderbook['bids'][:5]])
        ask_volume = sum([o['size'] for o in orderbook['asks'][:5]])
        return (bid_volume - ask_volume) / (bid_volume + ask_volume)
```

---

## 9. API 开发指南

### 9.1 环境准备

```bash
# 安装官方 Python 客户端
pip install py-clob-client

# 依赖
# Python 3.9+
# Web3.py
```

### 9.2 客户端初始化

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

# 配置
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon Mainnet
PRIVATE_KEY = "your_private_key"  # 从 reveal.polymarket.com 导出
PROXY_ADDRESS = "your_polymarket_proxy_address"  # 你的 Polymarket 充值地址

# 初始化方式 1: Email/Magic 钱包用户
client = ClobClient(
    HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    signature_type=1,  # Magic wallet
    funder=PROXY_ADDRESS
)

# 初始化方式 2: 浏览器钱包用户 (MetaMask 等)
client = ClobClient(
    HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    signature_type=2,  # Browser wallet
    funder=PROXY_ADDRESS
)

# 初始化方式 3: 直接 EOA 钱包
client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)

# 设置 API 凭证
client.set_api_creds(client.create_or_derive_api_creds())
```

### 9.3 市场数据获取

```python
# 获取所有市场
markets = client.get_markets()

# 获取特定市场详情
market = client.get_market(condition_id="xxx")

# 获取订单簿
orderbook = client.get_order_book(token_id="xxx")
print(f"Best bid: {orderbook.bids[0] if orderbook.bids else 'None'}")
print(f"Best ask: {orderbook.asks[0] if orderbook.asks else 'None'}")

# 获取最近交易
trades = client.get_trades(market_id="xxx", limit=100)

# 获取价格历史
prices = client.get_prices_history(
    market_id="xxx",
    interval="1h",  # 1m, 5m, 15m, 1h, 4h, 1d
    startTs=1700000000,
    endTs=1700100000
)
```

### 9.4 下单操作

```python
# 创建限价买单
order_args = OrderArgs(
    price=0.50,      # 买入价格 $0.50
    size=100.0,      # 买入 100 股
    side=BUY,
    token_id="xxx"   # YES 或 NO 代币 ID
)

# 签名订单
signed_order = client.create_order(order_args)

# 提交订单 - GTC (Good-Till-Cancelled)
response = client.post_order(signed_order, OrderType.GTC)
print(f"Order ID: {response.get('orderId')}")

# FOK (Fill-Or-Kill) 市价单
response = client.post_order(signed_order, OrderType.FOK)

# GTD (Good-Till-Date) 带过期时间
import time
expiration = int(time.time()) + 3600  # 1小时后过期
response = client.post_order(signed_order, OrderType.GTD, expiration=expiration)
```

### 9.5 订单管理

```python
# 获取活跃订单
active_orders = client.get_orders(market_id="xxx")

# 获取特定订单状态
order = client.get_order(order_id="xxx")

# 取消订单
client.cancel_order(order_id="xxx")

# 批量取消
client.cancel_all_orders()
# 或按市场取消
client.cancel_orders(market_id="xxx")
```

### 9.6 WebSocket 实时数据

```python
import asyncio
from py_clob_client.client import ClobClient

async def stream_market_data():
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)

    # 订阅市场频道
    async for message in client.subscribe_market_channel(token_id="xxx"):
        if message['type'] == 'book':
            print(f"Orderbook update: {message}")
        elif message['type'] == 'trade':
            print(f"New trade: {message}")

async def stream_user_updates():
    """订阅用户订单更新"""
    async for message in client.subscribe_user_channel():
        print(f"Order update: {message}")

# 运行
asyncio.run(stream_market_data())
```

### 9.7 API 认证详解

```python
"""
Polymarket 使用 Ed25519 签名认证

请求头格式：
- X-PM-Access-Key: 你的公钥 ID (UUID)
- X-PM-Timestamp: Unix 毫秒时间戳
- X-PM-Signature: Base64 编码的签名

签名构造：
message = timestamp + method + path
signature = Ed25519.sign(private_key, message)
"""

import time
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

def create_signature(private_key: bytes, method: str, path: str):
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}".encode()

    key = Ed25519PrivateKey.from_private_bytes(private_key)
    signature = key.sign(message)

    return {
        'X-PM-Timestamp': timestamp,
        'X-PM-Signature': base64.b64encode(signature).decode()
    }
```

### 9.8 完整交易示例

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

class PolymarketTrader:
    def __init__(self, config):
        self.client = ClobClient(
            config['host'],
            key=config['private_key'],
            chain_id=config['chain_id'],
            signature_type=config.get('signature_type', 0),
            funder=config.get('funder')
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def get_market_info(self, market_id):
        """获取市场信息"""
        market = self.client.get_market(market_id)
        orderbook = self.client.get_order_book(market['tokens'][0]['token_id'])

        return {
            'market': market,
            'yes_token': market['tokens'][0],
            'no_token': market['tokens'][1],
            'best_bid': orderbook.bids[0] if orderbook.bids else None,
            'best_ask': orderbook.asks[0] if orderbook.asks else None,
            'spread': self.calculate_spread(orderbook)
        }

    def place_limit_order(self, token_id, side, price, size):
        """下限价单"""
        order_args = OrderArgs(
            price=price,
            size=size,
            side=BUY if side == 'buy' else SELL,
            token_id=token_id
        )
        signed_order = self.client.create_order(order_args)
        response = self.client.post_order(signed_order, OrderType.GTC)
        return response

    def market_buy(self, token_id, amount_usd):
        """市价买入（FOK）"""
        orderbook = self.client.get_order_book(token_id)
        best_ask = float(orderbook.asks[0]['price'])
        size = amount_usd / best_ask

        order_args = OrderArgs(
            price=best_ask,
            size=size,
            side=BUY,
            token_id=token_id
        )
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order, OrderType.FOK)

    def check_arbitrage(self, market_id):
        """检查套利机会"""
        info = self.get_market_info(market_id)

        yes_price = float(info['best_ask']['price']) if info['best_ask'] else 1
        no_book = self.client.get_order_book(info['no_token']['token_id'])
        no_price = float(no_book.asks[0]['price']) if no_book.asks else 1

        total = yes_price + no_price

        if total < 0.98:  # 考虑费用后仍有利润
            profit = 1.0 - total
            return {
                'opportunity': True,
                'profit': profit,
                'yes_price': yes_price,
                'no_price': no_price
            }
        return {'opportunity': False}

# 使用示例
if __name__ == "__main__":
    config = {
        'host': 'https://clob.polymarket.com',
        'private_key': 'YOUR_PRIVATE_KEY',
        'chain_id': 137,
        'signature_type': 1,  # Magic wallet
        'funder': 'YOUR_PROXY_ADDRESS'
    }

    trader = PolymarketTrader(config)

    # 检查市场
    market_id = "0x..."
    info = trader.get_market_info(market_id)
    print(f"Market: {info['market']['question']}")
    print(f"Spread: {info['spread']}")

    # 检查套利
    arb = trader.check_arbitrage(market_id)
    if arb['opportunity']:
        print(f"Arbitrage opportunity! Profit: {arb['profit']}")
```

### 9.9 API 速率限制

根据 Polymarket 文档，API 有速率限制。建议：

- 避免高频轮询，使用 WebSocket 获取实时数据
- 批量操作使用批量 API 端点
- 实现指数退避重试机制
- 监控 429 响应并相应调整

---

## 10. 风险与注意事项

### 10.1 地理限制

**美国用户**（截至 2025 年 12 月已开放，但历史上曾被禁止）：
- 2022-2025 年间美国用户被禁止访问
- 使用 VPN 绕过限制违反服务条款
- 账户可能被冻结，资金可能被锁定

**其他受限地区**：
- 参考 Polymarket 官方地区限制列表
- 违规访问无法律保护

### 10.2 市场操纵风险

**Wash Trading（洗盘交易）**：
- 哥伦比亚大学研究发现约 25% 的交易量可能是虚假的
- 2024 年 12 月峰值时虚假交易占比达 60%
- 影响市场价格发现的可靠性

**内幕交易案例**：
- "French Whale"：使用私人民调数据在 2024 大选中获利 8500 万美元
- 委内瑞拉事件：用户在新闻公布前精准下注
- 监管机构正在关注但执法困难

### 10.3 智能合约风险

- 尽管经过审计，智能合约可能存在漏洞
- Neg-Risk Adapter 等新组件风险相对较高
- 建议不要将全部资金投入单一平台

### 10.4 流动性风险

- 热门市场外流动性可能很差
- 大额订单可能严重影响价格
- 结算前流动性可能枯竭

### 10.5 结算争议风险

- 结算结果可能存在争议
- 争议期间资金完全锁定
- 争议可能持续数天

### 10.6 合规风险

- 预测市场的法律地位因地区而异
- 税务处理可能复杂
- 建议咨询当地法律和税务专业人士

---

## 11. 竞品对比

### 11.1 平台对比表

| 特性 | Polymarket | Kalshi | PredictIt |
|------|------------|--------|-----------|
| **监管状态** | 去中心化，无监管 | CFTC 注册 | 学术豁免（已过期） |
| **美国合法性** | 2025年12月开放 | 完全合法 | 法律灰色地带 |
| **结算货币** | USDC (加密) | USD (法币) | USD (法币) |
| **区块链** | Polygon | 无 | 无 |
| **最大仓位** | 无限制 | $25,000 | $850 |
| **市场审批** | 快速 | 需 CFTC 批准 | 中等 |
| **流动性** | 最高 | 中等 | 较低 |
| **费用** | 2% 胜利费 | 约 10% | 约 5-10% |
| **API 访问** | 完整 | 有限 | 无 |

### 11.2 预测准确性

根据 2025 年范德堡大学研究：

| 平台 | 准确率 | 样本量 |
|------|--------|--------|
| PredictIt | 93% | 2,500 合约 |
| Kalshi | 78% | - |
| Polymarket | 67% | - |

**注意**：Polymarket 较低的准确率可能与样本选择、市场类型和 wash trading 有关。

### 11.3 选择建议

- **追求流动性和 API 访问**：Polymarket
- **追求合规性和美国用户**：Kalshi
- **小额测试和学习**：PredictIt

---

## 12. 附录与资源

### 12.1 官方资源

| 资源 | 链接 |
|------|------|
| 官网 | https://polymarket.com |
| 文档 | https://docs.polymarket.com |
| GitHub | https://github.com/Polymarket |
| Python 客户端 | https://github.com/Polymarket/py-clob-client |
| Discord | https://discord.gg/polymarket |

### 12.2 API 端点

```
主站点：https://polymarket.com
CLOB API：https://clob.polymarket.com
Gamma API：https://gamma-api.polymarket.com
数据 API：https://data-api.polymarket.com
```

### 12.3 有用的第三方工具

| 工具 | 功能 |
|------|------|
| Polymarket Monitor | 实时市场监控，套利机会发现 |
| PolyAlertHub | 价格提醒服务 |
| PolyTrack | 市场分析和追踪 |

### 12.4 学习资源

**学术文献**：
- "Toward Black–Scholes for Prediction Markets" (arXiv)
- "Prediction Markets" (Stanford GSB Working Papers)
- "Graphical Model Market Maker for Combinatorial Prediction Markets" (JAIR)

**策略指南**：
- The Complete Polymarket Playbook (Medium)
- Systematic Edges in Prediction Markets (Quantpedia)

### 12.5 代码仓库结构建议

```
polymarket-trading/
├── config/
│   ├── settings.py      # API 密钥和配置
│   └── markets.yaml     # 关注的市场列表
├── data/
│   ├── collector.py     # 数据收集
│   └── storage.py       # 数据存储
├── strategies/
│   ├── base.py          # 策略基类
│   ├── arbitrage.py     # 套利策略
│   ├── market_maker.py  # 做市策略
│   └── signals.py       # 信号生成
├── execution/
│   ├── order_manager.py # 订单管理
│   └── risk_manager.py  # 风险管理
├── backtest/
│   └── engine.py        # 回测引擎
└── main.py              # 主程序
```

### 12.6 常见问题 FAQ

**Q: 我是美国用户，可以使用 Polymarket 吗？**
A: 截至 2025 年 12 月，美国用户可以访问 Polymarket。但建议关注最新政策变化。

**Q: 最低入金是多少？**
A: 没有官方最低限制，但考虑到 Gas 费，建议至少 $50-100。

**Q: 资金安全吗？**
A: 资金存储在 Polygon 链上的智能合约中。虽然经过审计，但仍存在智能合约风险。

**Q: 可以做空吗？**
A: 可以。在二元市场中，买入 NO 等同于做空 YES。

**Q: 盈利需要交税吗？**
A: 取决于你所在地区的税法。建议咨询税务专业人士。

**Q: API 交易有延迟吗？**
A: WebSocket 提供近实时数据，REST API 有一定延迟。高频交易需考虑网络延迟。

---

## 更新日志

| 日期 | 版本 | 更新内容 |
|------|------|----------|
| 2026-01-25 | 1.0 | 初始版本 |

---

