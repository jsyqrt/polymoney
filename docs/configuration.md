# Configuration Reference

Polymoney supports configuration via environment variables, `.env` files, and YAML config files.

## Configuration Priority

1. **Environment variables** (highest priority)
2. **`.env` file** in project root
3. **YAML config file** (`config.yaml`, `polymoney.yaml`, or `~/.polymoney/config.yaml`)
4. **Default values** (lowest priority)

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
| `POLYMARKET_PRIVATE_KEY` | None | Wallet private key (required for live) |
| `POLYMARKET_FUNDER` | None | Proxy/funder address |
| `POLYMARKET_SIGNATURE_TYPE` | `0` | Signature type (0=EOA) |

### Trading Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_MODE` | `paper` | Trading mode: `paper` or `live` |
| `TRADING_MAX_POSITION_SIZE` | `1000.0` | Maximum position size per strategy |
| `TRADING_ORDER_RATE_LIMIT` | `10` | Maximum orders per second |
| `TRADING_RECONNECT_DELAY` | `1.0` | Initial WebSocket reconnect delay (seconds) |
| `TRADING_RECONNECT_MAX_DELAY` | `60.0` | Maximum reconnect delay (seconds) |

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

```bash
# .env

# Core
LOG_LEVEL=INFO

# Polymarket (REQUIRED for live trading)
POLYMARKET_PRIVATE_KEY=your_private_key_here
POLYMARKET_FUNDER=0x1234567890abcdef...

# Trading
TRADING_MODE=paper
TRADING_MAX_POSITION_SIZE=500.0

# Database
DATABASE_URL=sqlite+aiosqlite:///data/polymoney.db

# API
API_PORT=8080
API_KEY=your_secret_api_key
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
- Simulated order execution
- Perfect for testing strategies

```yaml
trading:
  mode: paper
```

### Live Mode

- Real orders on Polymarket
- Requires `POLYMARKET_PRIVATE_KEY`
- **Use with caution**

```yaml
trading:
  mode: live
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
print(config.trading.mode)
print(config.polymarket.host)
print(config.api.port)
```

### CLI Override

```bash
# Override config file
polymoney run --config custom.yaml

# Override specific values
polymoney run --mode live --port 9000
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

```yaml
# config.yaml - reference env vars
polymarket:
  private_key: ${POLYMARKET_PRIVATE_KEY}
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
- Range validation (e.g., port numbers)
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
