## Why

当前 Polymarket 交易需要手动操作或使用分散的脚本，缺乏一个统一的、可扩展的自动化交易框架。需要构建一个模块化的交易机器人框架，支持多市场实时数据订阅、多策略管理、回测验证、以及运行时监控和控制。

## What Changes

- 新增 **实时数据服务**：通过 WebSocket 订阅多个市场的实时订单簿和成交数据，聚合为 candlestick 格式存储
- 新增 **策略引擎**：插件化策略系统，支持针对不同市场类型编写策略，统一支持模拟交易和实盘交易
- 新增 **回测系统**：基于历史 candlestick 数据的策略回测引擎，复用策略代码
- 新增 **API 服务**：REST API 用于查询收益数据、运行状态、配置参数，以及控制策略启停
- 新增 **数据持久化**：SQLite/PostgreSQL 存储交易记录、仓位快照、性能指标

## Capabilities

### New Capabilities

- `market-data-service`: 实时市场数据订阅服务
  - WebSocket 连接管理（多市场并发订阅）
  - 原始数据 → candlestick 聚合（支持 1m/5m/15m/1h/4h/1d 周期）
  - 数据持久化到时序存储
  - 订单簿快照和增量更新处理

- `strategy-engine`: 策略执行引擎
  - 策略基类和插件注册机制
  - 交易模式切换（paper/live）
  - 订单管理（下单、撤单、状态跟踪）
  - 仓位管理和风险控制
  - 策略生命周期管理（初始化、运行、暂停、停止）

- `backtesting`: 回测引擎
  - 历史数据回放
  - 策略性能评估（PnL、胜率、最大回撤等）
  - 与实盘策略代码复用

- `api-server`: 控制和监控 API
  - 查询接口：收益数据、仓位状态、订单历史、策略状态
  - 控制接口：启停策略、启停市场、更新配置参数
  - 系统接口：健康检查、日志查询

- `data-storage`: 数据存储层
  - 交易记录存储
  - candlestick 时序数据存储
  - 仓位快照存储
  - 配置和状态持久化

### Modified Capabilities

（无现有能力需要修改）

## Impact

**新增代码模块**：
- `polymoney/core/` - 核心抽象（策略基类、订单类型、数据模型）
- `polymoney/data/` - 数据服务（WebSocket 客户端、candlestick 聚合器、存储）
- `polymoney/strategy/` - 策略引擎和内置策略
- `polymoney/backtest/` - 回测引擎
- `polymoney/api/` - REST API 服务（FastAPI）
- `polymoney/cli/` - 命令行工具

**依赖**：
- `py-clob-client` - Polymarket 官方 Python 客户端
- `websockets` - WebSocket 客户端
- `fastapi` + `uvicorn` - API 服务
- `sqlalchemy` - ORM
- `pydantic` - 数据模型验证

**现有代码**：
- `examples/position_arbitrage.py` 将迁移为内置策略插件
