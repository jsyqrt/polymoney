# Polymoney - Polymarket Trading Bot Framework

A modular, extensible Python framework for automated trading on [Polymarket](https://polymarket.com), the leading decentralized prediction market.

## Features

- **Real-time Market Data**: WebSocket connections for live orderbook and trade data
- **Multi-interval Candlesticks**: Automatic aggregation (1m, 5m, 15m, 1h, 4h, 1d)
- **Strategy Plugin System**: Easy-to-implement trading strategies with registry pattern
- **Paper & Live Trading**: Switch seamlessly between simulation and real trading
- **Backtesting Engine**: Test strategies against historical data
- **REST API**: Full control and monitoring via FastAPI server
- **CLI Interface**: Command-line tools for running, backtesting, and management

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/your-repo/polymoney.git
cd polymoney

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode
pip install -e ".[dev]"
```

### Run the Trading Bot

```bash
# Start in paper trading mode (default)
polymoney run --mode paper --port 8000

# Start in live trading mode (requires configuration)
polymoney run --mode live --port 8000
```

### List Available Strategies

```bash
polymoney list-strategies
```

### Run a Backtest

```bash
polymoney backtest --strategy position-arbitrage --start 2024-01-01 --end 2024-01-31
```

### Query Bot Status

```bash
polymoney status
```

## Configuration

Create a `.env` file or `config.yaml` in the project root:

```yaml
# config.yaml
log_level: INFO

polymarket:
  host: https://clob.polymarket.com
  chain_id: 137
  private_key: ${POLYMARKET_PRIVATE_KEY}  # Set via environment variable
  funder: "0x..."  # Your proxy address

trading:
  mode: paper  # paper or live
  max_position_size: 1000.0
  order_rate_limit: 10

database:
  url: sqlite+aiosqlite:///polymoney.db

api:
  host: 0.0.0.0
  port: 8000
  api_key: ${API_KEY}  # Optional
```

Environment variables can override config:
```bash
export POLYMARKET_PRIVATE_KEY="your_private_key"
export API_KEY="your_api_key"
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/strategies` | GET | List all strategy instances |
| `/strategies/{id}` | GET | Get strategy details |
| `/strategies` | POST | Create new strategy instance |
| `/strategies/{id}/start` | POST | Start a strategy |
| `/strategies/{id}/stop` | POST | Stop a strategy |
| `/strategies/{id}/pause` | POST | Pause a strategy |
| `/strategies/{id}/config` | PUT | Update strategy config |
| `/markets` | GET | List subscribed markets |
| `/markets/{id}/subscribe` | POST | Subscribe to market |
| `/orders` | GET | Query order history |
| `/metrics` | GET | Get performance metrics |
| `/health` | GET | Health check |

## Developing Custom Strategies

Create a new strategy by inheriting from `BaseStrategy`:

```python
from polymoney.core import BaseStrategy, register_strategy
from polymoney.core.models import PriceData, OrderSignal, TradeSide, TokenType

@register_strategy("my-strategy")
class MyStrategy(BaseStrategy):
    
    @property
    def strategy_type(self) -> str:
        return "custom"
    
    def on_price_update(self, price_data: PriceData) -> list[OrderSignal]:
        """Called on each price update. Return order signals."""
        signals = []
        
        # Your trading logic here
        if price_data.up_price < 0.30:
            signals.append(OrderSignal(
                side=TradeSide.BUY,
                token_type=TokenType.YES,
                target_price=price_data.up_price,
                size=10.0,
            ))
        
        return signals
    
    def on_market_start(self, market_id: str, market_info: dict) -> None:
        """Called when market becomes active."""
        self.reset()
    
    def on_market_end(self, market_id: str, winner: str) -> tuple[float, float, str]:
        """Called on market settlement. Return (cost, pnl, outcome)."""
        position = self.get_position(market_id)
        # Calculate final PnL
        cost = position.total_cost
        pnl = 0.0  # Calculate based on winner
        return cost, pnl, winner or "unknown"
    
    def get_status(self) -> dict:
        """Return current strategy status."""
        return {
            "strategy_id": self.strategy_id,
            "state": self.state.value,
            "positions": {
                mid: {
                    "up_shares": p.up.shares,
                    "down_shares": p.down.shares,
                }
                for mid, p in self._positions.items()
            },
        }
```

## Built-in Strategies

### Position Arbitrage (`position-arbitrage`)

A market-maker strategy that maintains balanced YES/NO positions to guarantee profit regardless of outcome:

- Target cost rate < 100% (e.g., 98%)
- Uses limit orders to buy both sides below market
- Phase-based execution for short-duration markets

## Architecture

```
polymoney/
├── core/           # Data models, base strategy, config
├── data/           # WebSocket client, candlestick aggregation
├── strategy/       # Strategy engine, order managers
├── backtest/       # Backtesting engine
├── api/            # FastAPI REST server
└── cli/            # Command-line interface
```

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_models.py -v

# Run with coverage
pytest tests/ --cov=polymoney --cov-report=html
```

## Dependencies

- Python 3.10+
- `py-clob-client` - Polymarket official API client
- `websockets` - WebSocket connections
- `fastapi` + `uvicorn` - REST API server
- `sqlalchemy` - Database ORM
- `pydantic` - Data validation
- `typer` + `rich` - CLI interface

## License

MIT License - see LICENSE file for details.

## Disclaimer

This software is for educational purposes. Trading on prediction markets involves financial risk. Use at your own risk. Always start with paper trading before using real funds.
