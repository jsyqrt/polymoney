## Context

Polymarket 是基于 Polygon 的预测市场平台，提供 CLOB（中央限价订单簿）交易。当前项目有一个 `position_arbitrage.py` 示例策略，但缺乏统一的框架来管理多市场、多策略的自动化交易。

**现有资源**：
- Polymarket 官方 `py-clob-client` 库，支持 REST API 和 WebSocket
- 研究文档：`docs/polymarket-research.md`（平台全面指南）、`docs/polymarket-btc-15min-bot-research.md`（仓位套利策略研究）
- 示例代码：`examples/position_arbitrage.py`（做市商套利策略实现）

**约束**：
- Polymarket API 有速率限制，需合理控制请求频率
- 短周期市场（15分钟）要求低延迟响应
- 需要支持 paper trading 进行策略验证，避免直接实盘测试

## Goals / Non-Goals

**Goals:**
- 构建模块化、可扩展的交易机器人框架
- 支持多市场并发订阅和交易
- 策略代码可同时用于回测和实盘，无需修改
- 提供 REST API 进行运行时监控和控制
- 数据持久化支持策略分析和回测

**Non-Goals:**
- 不实现 AI/ML 预测模型（可作为策略插件后续添加）
- 不构建前端 UI（仅提供 API，UI 可单独开发）
- 不支持跨平台套利（仅 Polymarket）
- 不实现高频交易优化（毫秒级延迟）

## Decisions

### D1: 整体架构 - 事件驱动 + 分层设计

```
┌─────────────────────────────────────────────────────────────┐
│                      API Server (FastAPI)                    │
│         /strategies  /markets  /orders  /metrics            │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                     Strategy Engine                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │Strategy A│  │Strategy B│  │Strategy C│  │   ...    │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │
│         │            │            │                          │
│         ▼            ▼            ▼                          │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              Order Manager (paper/live)              │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                   Market Data Service                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ WS Connection│  │ WS Connection│  │ WS Connection│       │
│  │  Market 1    │  │  Market 2    │  │  Market N    │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│         │                │                │                  │
│         ▼                ▼                ▼                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │           Candlestick Aggregator                     │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                     Data Storage                             │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐             │
│  │ Candles DB │  │ Orders DB  │  │ Metrics DB │             │
│  └────────────┘  └────────────┘  └────────────┘             │
└─────────────────────────────────────────────────────────────┘
```

**选择理由**：
- 事件驱动架构使策略响应实时数据，而非轮询
- 分层设计便于测试和替换组件（如切换 paper/live 订单管理）
- 与 `py-clob-client` 的 WebSocket 接口自然契合

**替代方案**：
- 轮询架构：简单但延迟高，不适合短周期市场
- 微服务架构：过度设计，单进程多协程足够

### D2: 策略插件系统 - 基类继承 + 注册装饰器

```python
from polymoney.core import BaseStrategy, register_strategy

@register_strategy("position-arbitrage")
class PositionArbitrageStrategy(BaseStrategy):
    def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
        # 策略逻辑
        pass
    
    def on_market_end(self, winner: str) -> Tuple[float, float, str]:
        # 结算逻辑
        pass
```

**选择理由**：
- 继承基类确保策略接口一致
- 装饰器注册简化策略发现和加载
- 现有 `position_arbitrage.py` 已使用类似模式，迁移成本低

**替代方案**：
- 配置文件 + 动态加载：灵活但类型安全差
- 纯函数式策略：简单但状态管理困难

### D3: 交易模式切换 - OrderManager 抽象

```python
class OrderManager(ABC):
    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult: ...
    
    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

class PaperOrderManager(OrderManager):
    # 模拟订单执行，基于当前市场价格
    pass

class LiveOrderManager(OrderManager):
    # 调用 py-clob-client 执行真实订单
    pass
```

**选择理由**：
- 策略代码完全不感知交易模式
- 切换只需更换 OrderManager 实现
- Paper trading 可模拟滑点、延迟等真实场景

**替代方案**：
- 策略内部 if/else 判断模式：代码耦合，难以维护
- 完全独立的 paper trading 系统：代码重复

### D4: 数据存储 - SQLite + SQLAlchemy ORM

**选择理由**：
- SQLite 零配置，适合单机部署
- SQLAlchemy ORM 便于后续迁移到 PostgreSQL
- Candlestick 数据量可控（15分钟市场一天仅 96 条）

**替代方案**：
- PostgreSQL：功能强但部署复杂
- InfluxDB：时序优化但增加依赖
- 纯文件存储：简单但查询不便

### D5: WebSocket 连接管理 - asyncio + 自动重连

```python
class MarketDataService:
    async def subscribe_market(self, market_id: str):
        while self.running:
            try:
                async with self.ws_connect(market_id) as ws:
                    async for msg in ws:
                        await self.process_message(market_id, msg)
            except ConnectionError:
                await asyncio.sleep(self.reconnect_delay)
```

**选择理由**：
- asyncio 协程高效处理多连接
- 自动重连保证长期运行稳定性
- 与 FastAPI 的 async 生态一致

### D6: Candlestick 聚合 - 内存聚合 + 周期性持久化

```python
class CandlestickAggregator:
    def __init__(self, intervals: List[str]):
        self.current_candles: Dict[str, Dict[str, Candle]] = {}
        # {market_id: {interval: current_candle}}
    
    def on_trade(self, market_id: str, trade: Trade):
        for interval in self.intervals:
            self._update_candle(market_id, interval, trade)
    
    def _on_interval_close(self, market_id: str, interval: str):
        # 持久化完成的 candle，开始新 candle
        pass
```

**选择理由**：
- 内存聚合避免每笔交易都写数据库
- 仅在周期结束时持久化，减少 I/O
- 支持多周期同时聚合（1m/5m/15m/1h）

### D7: API 设计 - RESTful + 资源导向

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/strategies` | GET | 列出所有策略及状态 |
| `/strategies/{id}` | GET | 获取策略详情（仓位、PnL） |
| `/strategies/{id}/start` | POST | 启动策略 |
| `/strategies/{id}/stop` | POST | 停止策略 |
| `/strategies/{id}/config` | PUT | 更新策略配置 |
| `/markets` | GET | 列出订阅的市场 |
| `/markets/{id}/subscribe` | POST | 订阅新市场 |
| `/orders` | GET | 查询订单历史 |
| `/metrics` | GET | 获取性能指标 |
| `/health` | GET | 健康检查 |

**选择理由**：
- RESTful 风格清晰易懂
- 资源导向符合操作语义
- FastAPI 自动生成 OpenAPI 文档

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| WebSocket 连接不稳定导致数据丢失 | 实现重连机制 + 连接状态监控 + 启动时数据补全 |
| Paper trading 与实盘表现差异 | 模拟滑点、延迟；提供 dry-run 模式验证 |
| SQLite 性能瓶颈（高频写入） | 批量写入 + 内存缓存；必要时迁移 PostgreSQL |
| 策略 bug 导致资金损失 | 强制 paper trading 验证期；仓位和订单金额上限 |
| API 速率限制触发 | 请求队列 + 指数退避；合理控制订阅市场数量 |

## Migration Plan

1. **Phase 1 - 核心框架**
   - 实现 `core/` 数据模型和策略基类
   - 实现 `data/` WebSocket 客户端和 Candlestick 聚合
   - 迁移 `position_arbitrage.py` 为内置策略

2. **Phase 2 - 交易能力**
   - 实现 Paper OrderManager
   - 实现 Live OrderManager（对接 py-clob-client）
   - 完成策略引擎生命周期管理

3. **Phase 3 - API 和持久化**
   - 实现 FastAPI 服务
   - 实现数据存储层
   - 添加回测引擎

4. **Phase 4 - 验证和优化**
   - Paper trading 充分验证
   - 性能优化和边界测试
   - 文档完善

## Open Questions

1. **多账户支持**：是否需要支持多个 Polymarket 账户同时交易？
   - 当前设计：单账户
   - 如需多账户：需要扩展 OrderManager 和配置管理

2. **WebSocket vs REST 轮询**：对于非关键数据（如市场列表），是否用 REST 轮询？
   - 建议：市场列表用 REST 周期刷新（5分钟），价格数据用 WebSocket

3. **Candlestick 存储周期**：保留多长时间的历史数据？
   - 建议：默认 30 天，可配置
