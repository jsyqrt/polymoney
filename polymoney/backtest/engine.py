"""
Backtesting Engine - Historical data replay and strategy testing.

Allows testing strategies against historical data with the same code used for live trading.
"""

import asyncio
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from polymoney.core.logging import get_logger
from polymoney.core.models import Candlestick, PriceData, TokenType
from polymoney.core.strategy import BaseStrategy, create_strategy
from polymoney.data.storage import DataStorage
from polymoney.strategy.order_manager import PaperOrderManager

logger = get_logger("backtest.engine")


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    strategy_type: str
    strategy_id: str = "backtest"
    market_ids: List[str] = field(default_factory=list)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    initial_capital: float = 1000.0
    slippage_pct: float = 0.001
    commission_pct: float = 0.0
    speed: str = "instant"  # instant, 10x, 100x, realtime
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestMetrics:
    """Performance metrics from a backtest."""

    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_trade_duration: float = 0.0  # seconds

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_pnl": self.total_pnl,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "largest_win": self.largest_win,
            "largest_loss": self.largest_loss,
            "avg_trade_duration": self.avg_trade_duration,
        }


@dataclass
class TradeLog:
    """Single trade record for logging."""

    timestamp: datetime
    market_id: str
    side: str
    token_type: str
    price: float
    size: float
    pnl: float = 0.0


@dataclass
class BacktestResult:
    """Complete backtest result."""

    config: BacktestConfig
    metrics: BacktestMetrics
    trades: List[TradeLog] = field(default_factory=list)
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "config": {
                "strategy_type": self.config.strategy_type,
                "market_ids": self.config.market_ids,
                "initial_capital": self.config.initial_capital,
                "slippage_pct": self.config.slippage_pct,
            },
            "metrics": self.metrics.to_dict(),
            "trade_count": len(self.trades),
            "equity_points": len(self.equity_curve),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
        }


class BacktestEngine:
    """
    Engine for backtesting trading strategies.

    Features:
    - Historical candlestick data replay
    - Strategy code reuse (same as live trading)
    - Simulated order execution
    - Comprehensive performance metrics
    - Equity curve tracking
    """

    def __init__(self, storage: DataStorage):
        """
        Initialize backtest engine.

        Args:
            storage: Data storage instance for loading historical data
        """
        self._storage = storage
        self._order_manager: Optional[PaperOrderManager] = None
        self._strategy: Optional[BaseStrategy] = None
        self._result: Optional[BacktestResult] = None

        # Replay state
        self._equity_curve: List[Dict[str, Any]] = []
        self._trades: List[TradeLog] = []
        self._peak_equity: float = 0.0
        self._current_equity: float = 0.0

    async def run(self, config: BacktestConfig) -> BacktestResult:
        """
        Run a backtest with the given configuration.

        Args:
            config: Backtest configuration

        Returns:
            BacktestResult with metrics and trade logs
        """
        logger.info(f"Starting backtest: {config.strategy_type} on {config.market_ids}")

        # Initialize
        self._order_manager = PaperOrderManager(
            slippage_pct=config.slippage_pct,
            fill_probability=1.0,
        )

        self._strategy = create_strategy(
            name=config.strategy_type,
            strategy_id=config.strategy_id,
            position_size=config.initial_capital,
            params=config.params,
        )

        if not self._strategy:
            raise ValueError(f"Unknown strategy type: {config.strategy_type}")

        # Setup
        self._current_equity = config.initial_capital
        self._peak_equity = config.initial_capital
        self._equity_curve = []
        self._trades = []

        # Wire up fill callback
        self._order_manager.add_fill_callback(self._on_trade)

        # Load and replay data
        start_time = datetime.now()

        for market_id in config.market_ids:
            await self._replay_market(market_id, config)

        end_time = datetime.now()

        # Calculate metrics
        metrics = self._calculate_metrics(config)

        # Build result
        result = BacktestResult(
            config=config,
            metrics=metrics,
            trades=self._trades,
            equity_curve=self._equity_curve,
            start_time=start_time,
            end_time=end_time,
        )

        self._result = result
        logger.info(f"Backtest complete: PnL={metrics.total_pnl:.2f}, Trades={metrics.total_trades}")

        return result

    async def _replay_market(self, market_id: str, config: BacktestConfig) -> None:
        """Replay historical data for a single market."""
        # Load candlestick data
        candles = await self._storage.get_candlesticks(
            market_id=market_id,
            interval="1m",  # Use 1m for finest granularity
            start_time=config.start_date,
            end_time=config.end_date,
            limit=100000,
        )

        if not candles:
            logger.warning(f"No data found for market {market_id}")
            return

        logger.info(f"Replaying {len(candles)} candles for {market_id}")

        # Initialize strategy for this market
        self._strategy.on_market_start(market_id, {"title": market_id})

        # Determine replay delay based on speed
        delay = self._get_replay_delay(config.speed)

        # Replay each candle
        for candle in candles:
            # Create price data from candle
            price_data = PriceData(
                market_id=market_id,
                up_price=candle.close,
                down_price=1.0 - candle.close,
                timestamp=candle.timestamp,
                spread=0.02,  # Assume 2% spread
            )

            # Update order manager prices
            self._order_manager.set_market_price(
                market_id,
                price_data.up_price,
                price_data.down_price,
            )

            # Get signals from strategy
            signals = self._strategy.on_price_update(price_data)

            # Execute signals
            for signal in signals:
                await self._order_manager.place_order(
                    signal=signal,
                    strategy_id=config.strategy_id,
                    market_id=market_id,
                )

            # Check fills
            await self._order_manager.check_fills()

            # Update equity curve
            self._update_equity(candle.timestamp, price_data)

            # Apply replay delay
            if delay > 0:
                await asyncio.sleep(delay)

        # End market
        self._strategy.on_market_end(market_id, None)

    def _get_replay_delay(self, speed: str) -> float:
        """Get delay between candles based on speed setting."""
        if speed == "instant":
            return 0
        elif speed == "100x":
            return 0.01  # 1ms per candle
        elif speed == "10x":
            return 0.1   # 100ms per candle
        elif speed == "realtime":
            return 60.0  # 1 minute per candle
        return 0

    def _on_trade(self, trade) -> None:
        """Handle trade execution."""
        self._trades.append(TradeLog(
            timestamp=trade.timestamp,
            market_id=trade.market_id,
            side=trade.side if isinstance(trade.side, str) else trade.side.value,
            token_type=trade.token_type if isinstance(trade.token_type, str) else trade.token_type.value,
            price=trade.price,
            size=trade.size,
        ))

    def _update_equity(self, timestamp: datetime, price: PriceData) -> None:
        """Update equity curve with current portfolio value."""
        # Calculate current equity based on positions
        # This is simplified - real implementation would track all positions

        self._equity_curve.append({
            "timestamp": timestamp.isoformat(),
            "equity": self._current_equity,
        })

        # Track peak for drawdown
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity

    def _calculate_metrics(self, config: BacktestConfig) -> BacktestMetrics:
        """Calculate performance metrics from backtest results."""
        metrics = BacktestMetrics()

        if not self._trades:
            return metrics

        # Basic trade stats
        metrics.total_trades = len(self._trades)

        # Calculate PnL per trade (simplified)
        wins = []
        losses = []

        for trade in self._trades:
            # Simplified PnL - real implementation would track entry/exit
            pnl = trade.pnl
            if pnl > 0:
                wins.append(pnl)
            elif pnl < 0:
                losses.append(pnl)

        metrics.winning_trades = len(wins)
        metrics.losing_trades = len(losses)

        if metrics.total_trades > 0:
            metrics.win_rate = metrics.winning_trades / metrics.total_trades

        if wins:
            metrics.avg_win = sum(wins) / len(wins)
            metrics.largest_win = max(wins)

        if losses:
            metrics.avg_loss = sum(losses) / len(losses)
            metrics.largest_loss = min(losses)

        # Calculate drawdown from equity curve
        if self._equity_curve:
            peak = config.initial_capital
            max_dd = 0
            max_dd_pct = 0

            for point in self._equity_curve:
                equity = point["equity"]
                if equity > peak:
                    peak = equity
                dd = peak - equity
                dd_pct = dd / peak if peak > 0 else 0

                if dd > max_dd:
                    max_dd = dd
                if dd_pct > max_dd_pct:
                    max_dd_pct = dd_pct

            metrics.max_drawdown = max_dd
            metrics.max_drawdown_pct = max_dd_pct

        # Total PnL
        metrics.total_pnl = sum(t.pnl for t in self._trades)

        return metrics

    def export_results_json(self, filepath: str) -> None:
        """Export backtest results to JSON file."""
        if not self._result:
            raise RuntimeError("No backtest results to export")

        data = self._result.to_dict()
        data["trades"] = [
            {
                "timestamp": t.timestamp.isoformat(),
                "market_id": t.market_id,
                "side": t.side,
                "token_type": t.token_type,
                "price": t.price,
                "size": t.size,
                "pnl": t.pnl,
            }
            for t in self._result.trades
        ]
        data["equity_curve"] = self._result.equity_curve

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Results exported to {filepath}")

    def export_trades_csv(self, filepath: str) -> None:
        """Export trade log to CSV file."""
        if not self._result:
            raise RuntimeError("No backtest results to export")

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "market_id", "side", "token_type", "price", "size", "pnl"
            ])

            for trade in self._result.trades:
                writer.writerow([
                    trade.timestamp.isoformat(),
                    trade.market_id,
                    trade.side,
                    trade.token_type,
                    trade.price,
                    trade.size,
                    trade.pnl,
                ])

        logger.info(f"Trades exported to {filepath}")

    def export_equity_csv(self, filepath: str) -> None:
        """Export equity curve to CSV file."""
        if not self._result:
            raise RuntimeError("No backtest results to export")

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "equity"])

            for point in self._result.equity_curve:
                writer.writerow([point["timestamp"], point["equity"]])

        logger.info(f"Equity curve exported to {filepath}")
