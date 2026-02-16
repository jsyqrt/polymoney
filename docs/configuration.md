# Configuration Reference

Polymoney supports configuration via environment variables, `.env` files, and YAML config files.

## Configuration Priority

1. **Environment variables** (highest priority)
2. **`.env` file** in project root
3. **YAML config file** (`config.yaml`, `polymoney.yaml`, or `~/.polymoney/config.yaml`)
4. **Default values** (lowest priority)

## Quick Start

```bash
# Copy the template and fill in your values
cp .env.example .env
```

See `.env.example` for a complete template with all available settings.

## Configuration File

### YAML Format

```yaml
# config.yaml

# Logging
log_level: INFO                    # DEBUG, INFO, WARNING, ERROR
log_file: /var/log/polymoney.log   # Optional log file path

# Polymarket API
polymarket:
  host: https://clob.polymarket.com
  chain_id: 137                    # Polygon mainnet
  private_key: ""                  # Your wallet private key
  funder: ""                       # Proxy/funder address
  signature_type: 0                # 0=EOA, 1=Magic, 2=Browser

# Trading Settings
trading:
  mode: paper                      # paper or live
  max_position_size: 1000.0        # Max capital per strategy
  order_rate_limit: 10             # Max orders per second
  reconnect_delay: 1.0             # Initial reconnect delay (seconds)
  reconnect_max_delay: 60.0        # Max reconnect delay (seconds)

# Risk Management
risk:
  daily_loss_limit: 50.0           # Max daily loss in USD
  per_market_loss_limit: 10.0      # Max loss per market in USD
  consecutive_loss_pause: 5        # Pause after N consecutive losses
  pause_duration_seconds: 1800.0   # Pause duration (30 minutes)
  max_total_exposure: 500.0        # Max total exposure in USD
  api_rate_limit: 10               # Max CLOB API calls per second

# Alerts / Monitoring
alert:
  webhook_url: ""                  # Discord/Telegram webhook URL
  enable_alerts: false             # Enable alert notifications
  alert_on_loss: 0.8               # Alert at this fraction of daily loss limit

# Database
database:
  url: sqlite+aiosqlite:///polymoney.db
  echo: false                      # Log SQL queries

# API Server
api:
  host: 0.0.0.0
  port: 8000
  api_key: ""                      # Optional API authentication
  cors_origins:                    # CORS allowed origins
    - "*"

# Data Settings
data:
  candlestick_intervals:           # Aggregation intervals
    - 1m
    - 5m
    - 15m
    - 1h
    - 4h
    - 1d
  retention_days: 30               # Data retention period
```

## Environment Variables

All configuration options can be set via environment variables.

### Naming Convention

- Use UPPERCASE
- Use underscores for separators
- Nested values use double underscore `__`

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `LOG_FILE` | None | Optional log file path |

### Polymarket Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `POLYMARKET_HOST` | `https://clob.polymarket.com` | CLOB API host |
| `POLYMARKET_CHAIN_ID` | `137` | Polygon chain ID |
| `POLYMARKET_PRIVATE_KEY` | None | Wallet private key (**required for live trading**) |
| `POLYMARKET_FUNDER` | None | Proxy/funder address |
| `POLYMARKET_SIGNATURE_TYPE` | `0` | Signature type (0=EOA, 1=Magic, 2=Browser) |

### Trading Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_MODE` | `paper` | Trading mode: `paper` or `live` |
| `TRADING_MAX_POSITION_SIZE` | `1000.0` | Maximum position size per strategy |
| `TRADING_ORDER_RATE_LIMIT` | `10` | Maximum orders per second |
| `TRADING_RECONNECT_DELAY` | `1.0` | Initial WebSocket reconnect delay (seconds) |
| `TRADING_RECONNECT_MAX_DELAY` | `60.0` | Maximum reconnect delay (seconds) |

### Risk Management Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `RISK_DAILY_LOSS_LIMIT` | `50.0` | Maximum daily loss in USD. Trading stops when breached. |
| `RISK_PER_MARKET_LOSS_LIMIT` | `10.0` | Maximum loss per market in USD |
| `RISK_CONSECUTIVE_LOSS_PAUSE` | `5` | Pause trading after N consecutive losses |
| `RISK_PAUSE_DURATION_SECONDS` | `1800.0` | Duration of pause after consecutive losses (seconds) |
| `RISK_MAX_TOTAL_EXPOSURE` | `500.0` | Maximum total exposure across all markets (USD) |
| `RISK_API_RATE_LIMIT` | `10` | Maximum CLOB API calls per second |

### Alert Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ALERT_WEBHOOK_URL` | None | Discord or generic webhook URL for alerts |
| `ALERT_ENABLE_ALERTS` | `false` | Enable alert notifications |
| `ALERT_ON_LOSS` | `0.8` | Send alert when daily loss reaches this fraction of limit |

### Database Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///polymoney.db` | Database connection URL |
| `DATABASE_ECHO` | `false` | Echo SQL queries to log |

### API Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST` | `0.0.0.0` | API server bind address |
| `API_PORT` | `8000` | API server port |
| `API_KEY` | None | Optional API key for authentication |

### Data Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_RETENTION_DAYS` | `30` | Data retention period in days |

## Example .env File

项目根目录提供了 `.env.example` 模板，包含所有可配置项：

```bash
# .env

# Core
LOG_LEVEL=INFO

# Polymarket (REQUIRED for live trading)
POLYMARKET_PRIVATE_KEY=your_wallet_private_key_here
POLYMARKET_HOST=https://clob.polymarket.com
POLYMARKET_CHAIN_ID=137
# POLYMARKET_FUNDER=0x_your_funder_address

# Risk Management
RISK_DAILY_LOSS_LIMIT=50.0
RISK_PER_MARKET_LOSS_LIMIT=10.0
RISK_CONSECUTIVE_LOSS_PAUSE=5
RISK_MAX_TOTAL_EXPOSURE=500.0

# Alerts
# ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
# ALERT_ENABLE_ALERTS=true

# Trading
TRADING_MODE=paper
# TRADING_MAX_POSITION_SIZE=1000.0

# Database
DATABASE_URL=sqlite+aiosqlite:///data/polymoney.db

# API
API_PORT=8080
# API_KEY=your_secret_api_key
```

## Database URLs

### SQLite (default)

```
sqlite+aiosqlite:///polymoney.db         # Relative path
sqlite+aiosqlite:////data/polymoney.db   # Absolute path
```

### PostgreSQL

```
postgresql+asyncpg://user:password@localhost:5432/polymoney
```

### MySQL

```
mysql+aiomysql://user:password@localhost:3306/polymoney
```

## Trading Modes

### Paper Mode (Default)

- No real money at risk
- Simulated order execution via `SimulatedExecutor`
- Uses depth-based + spread probability fill model
- Perfect for strategy validation

```bash
python scripts/run_trading.py
```

### Live Mode

- Real orders on Polymarket CLOB via `LiveExecutor`
- Requires `POLYMARKET_PRIVATE_KEY` in `.env`
- Risk management enforced (`RiskManager` + `KillSwitch`)
- **Use with caution — real money at risk**

```bash
python scripts/run_trading.py --live
```

## Strategy Configuration

策略参数通过 YAML 配置文件管理，默认路径 `config/strategy_defaults.yaml`：

```bash
# 使用默认配置
python scripts/run_trading.py

# 使用自定义配置文件
python scripts/run_trading.py --config my_config.yaml

# CLI 参数覆盖配置文件中的同名参数
python scripts/run_trading.py --target-cost 0.98
```

## API Authentication

When `API_KEY` is set, all endpoints (except `/health` and `/docs`) require authentication:

```bash
curl -H "X-API-Key: your_secret_key" http://localhost:8000/strategies
```

## Configuration Loading

### Programmatic Access

```python
from polymoney.core.config import load_config, Config

# Load from default locations
config = load_config()

# Load from specific file
config = load_config("custom-config.yaml")

# Access values
print(config.trading.mode)          # "paper"
print(config.polymarket.host)       # "https://clob.polymarket.com"
print(config.risk.daily_loss_limit) # 50.0
print(config.alert.enable_alerts)   # False
```

### CLI Override

```bash
# Override config file
python scripts/run_trading.py --config custom.yaml

# Override specific values via CLI flags
python scripts/run_trading.py --target-cost 0.96
```

## Security Best Practices

### Never Commit Secrets

```gitignore
# .gitignore
.env
config.yaml
*.key
```

### Use Environment Variables for Secrets

```bash
# Set in shell or .env file — never hardcode in YAML
export POLYMARKET_PRIVATE_KEY=0x...
```

### Restrict API Access

```yaml
api:
  api_key: ${API_KEY}          # Require authentication
  cors_origins:
    - https://yourdomain.com   # Restrict CORS
```

## Validation

Configuration is validated on load using Pydantic:

- Type checking (string, int, float, bool)
- Range validation
- Required field validation

Invalid configuration will raise a clear error:

```
pydantic.ValidationError: 1 validation error for TradingConfig
max_position_size
  Input should be greater than 0 [type=greater_than, input_value=-100, input_type=int]
```

## Default Config Locations

The framework searches for config files in this order:

1. `./config.yaml`
2. `./config.yml`
3. `./polymoney.yaml`
4. `./polymoney.yml`
5. `~/.polymoney/config.yaml`

Use `--config` flag to specify a custom location.

---

*最后更新：2026-02-16*
