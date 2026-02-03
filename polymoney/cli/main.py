"""
CLI Tool - Command-line interface for Polymoney.

Provides commands for running the bot, backtesting, and querying status.
"""

import asyncio
from datetime import datetime
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="polymoney",
    help="Polymarket Trading Bot Framework",
    add_completion=False,
)
console = Console()


@app.command()
def run(
    config_file: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    mode: str = typer.Option("paper", "--mode", "-m", help="Trading mode (paper/live)"),
    host: str = typer.Option("0.0.0.0", "--host", help="API server host"),
    port: int = typer.Option(8000, "--port", "-p", help="API server port"),
):
    """
    Start the trading bot with API server.
    """
    console.print("[bold green]Starting Polymoney...[/bold green]")

    from polymoney.api import create_app, run_server
    from polymoney.core.config import load_config
    from polymoney.core.logging import setup_logging
    from polymoney.data import DataStorage, MarketDataService
    from polymoney.strategy import StrategyEngine

    # Import built-in strategies to register them
    import polymoney.strategy.builtin  # noqa: F401

    # Load config
    config = load_config(config_file)
    config.trading.mode = mode
    config.api.host = host
    config.api.port = port

    # Setup logging
    setup_logging(level=config.log_level, log_file=config.log_file)

    # Initialize components
    async def init():
        storage = DataStorage(config.database.url)
        await storage.initialize()

        market_data = MarketDataService(
            intervals=config.data.candlestick_intervals,
            reconnect_delay=config.trading.reconnect_delay,
            reconnect_max_delay=config.trading.reconnect_max_delay,
        )

        engine = StrategyEngine(
            market_data_service=market_data,
            trading_mode=config.trading.mode,
            max_position_size=config.trading.max_position_size,
            order_rate_limit=config.trading.order_rate_limit,
        )

        return storage, market_data, engine

    storage, market_data, engine = asyncio.run(init())

    # Create API app
    api = create_app(
        config=config,
        strategy_engine=engine,
        market_data_service=market_data,
        data_storage=storage,
    )

    console.print(f"[bold]Trading Mode:[/bold] {mode}")
    console.print(f"[bold]API Server:[/bold] http://{host}:{port}")
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    # Run server
    run_server(api, host=host, port=port)


@app.command()
def backtest(
    strategy: str = typer.Argument(..., help="Strategy type name"),
    market: str = typer.Argument(..., help="Market ID to backtest"),
    start_date: Optional[str] = typer.Option(None, "--start", help="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = typer.Option(None, "--end", help="End date (YYYY-MM-DD)"),
    capital: float = typer.Option(1000.0, "--capital", "-c", help="Initial capital"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file for results"),
    config_file: Optional[str] = typer.Option(None, "--config", help="Config file path"),
):
    """
    Run a backtest for a strategy.
    """
    console.print(f"[bold green]Running backtest: {strategy}[/bold green]")

    from polymoney.backtest import BacktestConfig, BacktestEngine
    from polymoney.core.config import load_config
    from polymoney.data import DataStorage

    # Import built-in strategies
    import polymoney.strategy.builtin  # noqa: F401

    # Load config
    config = load_config(config_file)

    # Parse dates
    start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None

    async def run_backtest():
        storage = DataStorage(config.database.url)
        await storage.initialize()

        engine = BacktestEngine(storage)

        bt_config = BacktestConfig(
            strategy_type=strategy,
            market_ids=[market],
            start_date=start,
            end_date=end,
            initial_capital=capital,
            speed="instant",
        )

        result = await engine.run(bt_config)

        # Display results
        table = Table(title="Backtest Results")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Total PnL", f"${result.metrics.total_pnl:.2f}")
        table.add_row("Total Trades", str(result.metrics.total_trades))
        table.add_row("Win Rate", f"{result.metrics.win_rate:.1%}")
        table.add_row("Max Drawdown", f"${result.metrics.max_drawdown:.2f}")
        table.add_row("Max Drawdown %", f"{result.metrics.max_drawdown_pct:.1%}")

        console.print(table)

        # Export if requested
        if output:
            if output.endswith(".json"):
                engine.export_results_json(output)
            else:
                engine.export_trades_csv(output)
            console.print(f"[dim]Results exported to {output}[/dim]")

        await storage.close()

    asyncio.run(run_backtest())


@app.command("list-strategies")
def list_strategies():
    """
    List all registered strategy types.
    """
    # Import to register strategies
    import polymoney.strategy.builtin  # noqa: F401
    from polymoney.core.strategy import get_strategy_registry

    registry = get_strategy_registry()

    if not registry:
        console.print("[yellow]No strategies registered[/yellow]")
        return

    table = Table(title="Registered Strategies")
    table.add_column("Name", style="cyan")
    table.add_column("Class", style="green")
    table.add_column("Type", style="yellow")

    for name, cls in registry.items():
        # Create temp instance to get type
        try:
            instance = cls(strategy_id="temp", name=name)
            strategy_type = instance.strategy_type
        except Exception:
            strategy_type = "unknown"

        table.add_row(name, cls.__name__, strategy_type)

    console.print(table)


@app.command()
def status(
    host: str = typer.Option("localhost", "--host", help="API host"),
    port: int = typer.Option(8000, "--port", "-p", help="API port"),
):
    """
    Query running bot status via API.
    """
    import httpx

    url = f"http://{host}:{port}"

    try:
        with httpx.Client() as client:
            # Health check
            health = client.get(f"{url}/health").json()

            table = Table(title="Bot Status")
            table.add_column("Component", style="cyan")
            table.add_column("Status", style="green")

            table.add_row("Overall", health.get("status", "unknown"))
            for comp, status in health.get("components", {}).items():
                table.add_row(f"  {comp}", status)

            console.print(table)

            # Strategies
            strategies = client.get(f"{url}/strategies").json()

            if strategies.get("strategies"):
                strat_table = Table(title="Active Strategies")
                strat_table.add_column("ID", style="cyan")
                strat_table.add_column("Type", style="green")
                strat_table.add_column("State", style="yellow")

                for sid, info in strategies["strategies"].items():
                    strat_table.add_row(
                        sid,
                        info.get("type", ""),
                        info.get("state", ""),
                    )

                console.print(strat_table)
            else:
                console.print("[dim]No active strategies[/dim]")

    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {url}[/red]")
        console.print("[dim]Is the bot running?[/dim]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@app.command()
def version():
    """
    Show version information.
    """
    from polymoney import __version__

    console.print(f"[bold]Polymoney[/bold] v{__version__}")


if __name__ == "__main__":
    app()
