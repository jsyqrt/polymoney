#!/usr/bin/env python3
"""
Unified Simulation Runner - Real data backtesting and live paper trading.

This script provides two modes for running the position arbitrage strategy:
1. Backtest mode: Test strategy on historical real market data
2. Live mode: Real-time paper trading simulation

Usage:
    # List available markets
    python scripts/run_simulation.py list [--count N]

    # Backtest on a specific market
    python scripts/run_simulation.py backtest --slug btc-updown-15m-XXXXXXX

    # Backtest on N recent closed markets
    python scripts/run_simulation.py backtest --count 5

    # Run live simulation (default: BTC, ETH, SOL)
    python scripts/run_simulation.py live

    # Live simulation for specific coins
    python scripts/run_simulation.py live --markets btc,eth

    # Live simulation with duration
    python scripts/run_simulation.py live --duration 10h

Output:
    Results are written to simulation_results/ (or custom --output-dir):
    - status.json: Current simulation state
    - metrics.jsonl: Time-series of performance metrics
    - results.jsonl: Per-market results

Monitoring:
    # Check status
    python -m polymoney.simulation.status_cli

    # Watch live updates
    python -m polymoney.simulation.status_cli --tail
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymoney.config import StrategyConfig, load_config
from polymoney.core.logging import get_logger, setup_logging
from polymoney.data.real_data_fetcher import RealDataFetcher
from polymoney.data.storage import DataStorage
from polymoney.simulation import LiveRunner, SimulationConfig
from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy
from polymoney.testing import PerformanceAnalytics, TestRunner
from polymoney.testing.runner import TestConfig, TestResult

logger = get_logger("run_simulation")


# =============================================================================
# List Markets
# =============================================================================

async def list_markets(count: int = 10, include_active: bool = True, include_closed: bool = True):
    """List available BTC 15min markets."""
    print(f"\nFetching {count} BTC 15-minute markets from Polymarket...")

    async with RealDataFetcher() as fetcher:
        markets = await fetcher.find_btc_15min_markets(
            count=count,
            include_active=include_active,
            include_closed=include_closed,
        )

    if not markets:
        print("No markets found.")
        return

    print(f"\nFound {len(markets)} markets:\n")
    for i, m in enumerate(markets, 1):
        status = "ACTIVE" if not m['closed'] else f"CLOSED ({m['winner']})"
        vol = float(m['volume']) if m['volume'] else 0
        print(f"{i}. {m['title']}")
        print(f"   Slug: {m['slug']}")
        print(f"   Status: {status}")
        print(f"   Volume: ${vol:,.2f}")
        print()


# =============================================================================
# Backtest
# =============================================================================

async def backtest_single_market(
    slug: str,
    position_size: float = 100.0,
    strategy_params: Optional[dict] = None,
    output_dir: str = "simulation_results",
    config: Optional[StrategyConfig] = None,
) -> TestResult:
    """Run backtest on a specific market by slug."""
    logger.info(f"Starting backtest for market: {slug}")

    # Build strategy params from config + overrides
    if strategy_params is None:
        if config:
            strategy_params = config.get_strategy_params()
        else:
            strategy_params = {
                "target_cost": 0.96,
                "batch_ratio": 0.001,
                "ecr_threshold": 1.05,
                "enable_ecr_stoploss": True,
                "enable_rebalancing": True,
                "enable_trend_detection": True,
                "enable_urgency_pricing": True,
            }

    # Fetch candlesticks using RealDataFetcher
    async with RealDataFetcher() as fetcher:
        candlesticks, market_info = await fetcher.get_market_candlesticks(slug)

    if not candlesticks:
        logger.error(f"No candlestick data available for {slug}")
        raise ValueError(f"No market data for {slug}")

    logger.info(f"Got {len(candlesticks)} candlesticks for {market_info.get('title', slug)}")
    logger.info(f"Winner: {market_info.get('winner', 'unknown')}")

    # Initialize storage
    storage = DataStorage("sqlite+aiosqlite:///:memory:")
    await storage.initialize()

    # Save candlesticks
    market_id = market_info.get("condition_id", slug)
    for candle in candlesticks:
        candle.market_id = market_id
        await storage.save_candlestick(candle)

    # Configure test
    config = TestConfig(
        strategy_class=PositionArbitrageStrategy,
        strategy_params=strategy_params,
        position_size=position_size,
        replay_speed=0,  # Max speed
    )

    # Run test
    runner = TestRunner(storage=storage, config=config)
    settlement = market_info.get("winner", "up")

    result = await runner.run_replay(
        market_id=market_id,
        title=market_info.get("title", slug),
        settlement_winner=settlement,
    )

    # Generate analytics
    analytics = PerformanceAnalytics()
    analytics.add_result(result)

    # Create output directory
    output_path = Path(output_dir) / "backtest" / slug
    output_path.mkdir(parents=True, exist_ok=True)

    # Generate report
    report = analytics.generate_report()
    report_file = output_path / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    report_file.write_text(report)

    # Export data
    analytics.export_results_json(str(output_path / "results.json"))
    analytics.export_equity_curve_csv(result, str(output_path / "equity_curve.csv"))

    # Try to generate plots
    try:
        analytics.plot_equity_curve(result, str(output_path / "equity_curve.png"))
    except Exception as e:
        logger.warning(f"Could not generate plot: {e}")

    # Print summary
    print("\n" + "=" * 60)
    print(f"BACKTEST RESULT: {market_info.get('title', slug)}")
    print("=" * 60)
    print(f"Settlement: {settlement.upper()} won")
    print(f"\nPosition:")
    print(f"  UP:   {result.up_shares:.2f} shares @ ${result.up_cost:.2f}")
    print(f"  DOWN: {result.down_shares:.2f} shares @ ${result.down_cost:.2f}")
    print(f"\nMetrics:")
    print(f"  Total Cost:          ${result.total_cost:.2f}")
    print(f"  Settlement Value:    ${result.settlement_value:.2f}")
    print(f"  PnL:                 ${result.pnl:.2f} ({result.roi:.2%})")
    print(f"  Effective Cost Rate: {result.effective_cost_rate:.2%}")
    print(f"  Balance Ratio:       {result.balance_ratio:.2%}")
    print(f"\nTrading Stats:")
    print(f"  Orders Submitted:    {result.orders_submitted}")
    print(f"  Orders Filled:       {result.orders_filled}")
    print(f"  Fill Rate:           {result.fill_rate:.1%}")
    print(f"\nOutput saved to: {output_path}")
    print("=" * 60)

    await storage.close()
    return result


async def backtest_multiple_markets(
    count: int = 5,
    position_size: float = 100.0,
    strategy_params: Optional[dict] = None,
    output_dir: str = "simulation_results",
):
    """Run backtest on multiple recent closed markets."""
    print(f"\n{'='*60}")
    print(f"MULTI-MARKET BACKTEST ({count} markets)")
    print(f"{'='*60}\n")

    # Find closed markets
    async with RealDataFetcher() as fetcher:
        markets = await fetcher.find_btc_15min_markets(
            count=count,
            include_active=False,
            include_closed=True,
        )

    if not markets:
        print("No closed markets found for backtesting.")
        return

    print(f"Found {len(markets)} closed markets to test\n")

    results = []
    for i, m in enumerate(markets, 1):
        print(f"\n[{i}/{len(markets)}] Testing: {m['slug']}")
        print(f"  Winner: {m['winner']}")

        try:
            result = await backtest_single_market(
                slug=m['slug'],
                position_size=position_size,
                strategy_params=strategy_params,
                output_dir=output_dir,
            )
            results.append({
                "slug": m['slug'],
                "winner": m['winner'],
                "pnl": result.pnl,
                "roi": result.roi,
                "ecr": result.effective_cost_rate,
                "balance": result.balance_ratio,
                "fill_rate": result.fill_rate,
            })
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({
                "slug": m['slug'],
                "winner": m['winner'],
                "error": str(e),
            })

    # Summary
    print("\n" + "=" * 60)
    print("MULTI-MARKET BACKTEST SUMMARY")
    print("=" * 60)

    successful = [r for r in results if "pnl" in r]
    if successful:
        total_pnl = sum(r["pnl"] for r in successful)
        avg_roi = sum(r["roi"] for r in successful) / len(successful)
        avg_ecr = sum(r["ecr"] for r in successful) / len(successful)
        avg_balance = sum(r["balance"] for r in successful) / len(successful)
        win_count = sum(1 for r in successful if r["pnl"] > 0)

        print(f"\nMarkets tested: {len(successful)}/{len(results)}")
        print(f"Win rate: {win_count}/{len(successful)} ({win_count/len(successful):.1%})")
        print(f"Total PnL: ${total_pnl:.2f}")
        print(f"Average ROI: {avg_roi:.2%}")
        print(f"Average ECR: {avg_ecr:.2%}")
        print(f"Average Balance: {avg_balance:.2%}")

        print(f"\nPer-market results:")
        for r in successful:
            status = "✓" if r["pnl"] > 0 else "✗"
            print(f"  {status} {r['slug']}: ${r['pnl']:.2f} ({r['roi']:.1%})")
    else:
        print("No successful tests to summarize.")

    # Save summary
    summary_path = Path(output_dir) / "backtest" / "multi_market_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSummary saved to: {summary_path}")


# =============================================================================
# Live Simulation
# =============================================================================

async def run_live_simulation(
    coins: list[str],
    duration_seconds: float,
    output_dir: Path,
    target_cost: float = 0.96,
    batch_ratio: float = 0.001,
    position_size: float = 100.0,
    ecr_threshold: float = 1.05,
    enable_ecr_stoploss: bool = True,
    enable_rebalancing: bool = True,
    enable_trend_detection: bool = True,
    market_scan_interval: float = 60.0,
    metrics_output_interval: float = 1.0,
    min_trading_time: float = 300.0,
    resume: bool = False,
    yaml_config: Optional[StrategyConfig] = None,
):
    """Run live paper trading simulation."""
    if yaml_config:
        # Build SimulationConfig from YAML with CLI overrides
        config = yaml_config.to_simulation_config(
            coins=coins,
            duration_seconds=duration_seconds,
            output_dir=output_dir,
            target_cost=target_cost,
            batch_ratio=batch_ratio,
            position_size=position_size,
            ecr_threshold=ecr_threshold,
            enable_ecr_stoploss=enable_ecr_stoploss,
            enable_rebalancing=enable_rebalancing,
            enable_trend_detection=enable_trend_detection,
            market_scan_interval=market_scan_interval,
            min_trading_time=min_trading_time,
        )
    else:
        config = SimulationConfig(
            coins=coins,
            duration_seconds=duration_seconds,
            output_dir=output_dir,
            target_cost=target_cost,
            batch_ratio=batch_ratio,
            position_size=position_size,
            ecr_threshold=ecr_threshold,
            enable_ecr_stoploss=enable_ecr_stoploss,
            enable_rebalancing=enable_rebalancing,
            enable_trend_detection=enable_trend_detection,
            enable_urgency_pricing=True,
            market_scan_interval=market_scan_interval,
            metrics_output_interval=metrics_output_interval,
            min_trading_time=min_trading_time,
        )

    # Print startup info
    print("=" * 60)
    print("POLYMONEY LIVE SIMULATION")
    print("=" * 60)
    print()
    print(f"Coins: {', '.join(coins)}")
    if duration_seconds > 0:
        hours = duration_seconds / 3600
        print(f"Duration: {hours:.1f} hours")
    else:
        print("Duration: Infinite (Ctrl+C to stop)")
    print(f"Output: {output_dir}/")
    print()
    batch_size_auto = max(position_size * batch_ratio, 0.10)
    print("Strategy Settings:")
    print(f"  Target Cost: {target_cost}")
    print(f"  Batch Ratio: {batch_ratio} (${batch_size_auto:.2f}/order)")
    print(f"  Position Size: ${position_size} per market")
    print(f"  ECR Threshold: {ecr_threshold}")
    print(f"  ECR Stop-loss: {'Enabled' if enable_ecr_stoploss else 'Disabled'}")
    print(f"  Rebalancing: {'Enabled' if enable_rebalancing else 'Disabled'}")
    print(f"  Trend Detection: {'Enabled' if enable_trend_detection else 'Disabled'}")
    print()
    print("=" * 60)
    print()

    if resume:
        print("Resuming from previous state...")
    else:
        print("Starting fresh simulation...")
    print()

    # Run simulation
    runner = LiveRunner(config)

    try:
        stats = await runner.run(resume=resume)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return

    # Print final stats
    print()
    print("=" * 60)
    print("FINAL RESULTS")
    print("=" * 60)
    print()
    print(f"Markets Processed: {stats.markets_processed}")
    print(f"Win Rate: {stats.win_rate * 100:.1f}%")
    print(f"Total PnL: ${stats.total_pnl:.2f}")
    print(f"Total ROI: {stats.total_roi * 100:.2f}%")
    print(f"Sharpe Ratio: {stats.sharpe_ratio:.2f}")
    print()
    print("=" * 60)


# =============================================================================
# CLI
# =============================================================================

def parse_duration(duration_str: str) -> float:
    """Parse duration string (e.g., '10h', '3d', '0' for infinite)."""
    if not duration_str or duration_str == "0":
        return 0

    duration_str = duration_str.strip().lower()

    if duration_str.endswith("h"):
        return float(duration_str[:-1]) * 3600
    elif duration_str.endswith("d"):
        return float(duration_str[:-1]) * 86400
    elif duration_str.endswith("m"):
        return float(duration_str[:-1]) * 60
    else:
        return float(duration_str)


def main():
    parser = argparse.ArgumentParser(
        description="Run simulation with real Polymarket data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # -------------------------------------------------------------------------
    # List command
    # -------------------------------------------------------------------------
    list_parser = subparsers.add_parser("list", help="List available markets")
    list_parser.add_argument(
        "--count", "-n",
        type=int,
        default=10,
        help="Number of markets to list (default: 10)",
    )
    list_parser.add_argument(
        "--active-only",
        action="store_true",
        help="Show only active markets",
    )
    list_parser.add_argument(
        "--closed-only",
        action="store_true",
        help="Show only closed markets",
    )

    # -------------------------------------------------------------------------
    # Backtest command
    # -------------------------------------------------------------------------
    backtest_parser = subparsers.add_parser("backtest", help="Run backtest on historical data")
    backtest_parser.add_argument(
        "--slug",
        type=str,
        help="Market slug to test (e.g., btc-updown-15m-XXXXXXX)",
    )
    backtest_parser.add_argument(
        "--count", "-n",
        type=int,
        help="Number of recent closed markets to backtest",
    )
    backtest_parser.add_argument(
        "--position-size",
        type=float,
        default=100.0,
        help="Position size in USD (default: 100)",
    )
    backtest_parser.add_argument(
        "--target-cost",
        type=float,
        default=0.98,
        help="Target cost rate (default: 0.98)",
    )
    backtest_parser.add_argument(
        "--ecr-threshold",
        type=float,
        default=1.05,
        help="ECR stop-loss threshold (default: 1.05)",
    )
    backtest_parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default="simulation_results",
        help="Output directory (default: simulation_results)",
    )

    # -------------------------------------------------------------------------
    # Live command
    # -------------------------------------------------------------------------
    live_parser = subparsers.add_parser("live", help="Run live paper trading simulation")
    live_parser.add_argument(
        "--duration", "-d",
        type=str,
        default="0",
        help="Runtime duration (e.g., '10h', '3d', '0' for infinite). Default: infinite",
    )
    live_parser.add_argument(
        "--markets", "-m",
        type=str,
        default="btc,eth,sol",
        help="Comma-separated list of coins (default: btc,eth,sol)",
    )
    live_parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("simulation_results"),
        help="Output directory (default: simulation_results)",
    )
    live_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous simulation state",
    )
    live_parser.add_argument(
        "--target-cost",
        type=float,
        default=0.96,
        help="Target cost ratio (default: 0.96)",
    )
    live_parser.add_argument(
        "--batch-ratio",
        type=float,
        default=0.001,
        help="Order size as fraction of position_size (default: 0.001 = 1/1000)",
    )
    live_parser.add_argument(
        "--position-size",
        type=float,
        default=100.0,
        help="Max position size per market in USD (default: 100.0)",
    )
    live_parser.add_argument(
        "--ecr-threshold",
        type=float,
        default=1.05,
        help="ECR stop-loss threshold (default: 1.05)",
    )
    live_parser.add_argument(
        "--disable-ecr-stoploss",
        action="store_true",
        help="Disable ECR stop-loss",
    )
    live_parser.add_argument(
        "--disable-rebalancing",
        action="store_true",
        help="Disable market order rebalancing",
    )
    live_parser.add_argument(
        "--disable-trend-detection",
        action="store_true",
        help="Disable trend detection",
    )
    live_parser.add_argument(
        "--scan-interval",
        type=float,
        default=60.0,
        help="Market scan interval in seconds (default: 60)",
    )
    live_parser.add_argument(
        "--quick-test",
        action="store_true",
        help="Enable quick test mode (faster intervals)",
    )

    # -------------------------------------------------------------------------
    # Common options
    # -------------------------------------------------------------------------
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to YAML config file (default: config/strategy_defaults.yaml)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )

    args = parser.parse_args()

    # Setup logging
    log_level = "DEBUG" if args.verbose else args.log_level
    setup_logging(log_level)

    # Load YAML config (if specified or default exists)
    yaml_config = None
    if hasattr(args, 'config') and args.config:
        yaml_config = load_config(args.config)
        logger.info(f"Using config: {args.config}")
    else:
        # Try to load default config silently
        try:
            yaml_config = load_config()
        except FileNotFoundError:
            pass

    # Handle commands
    if args.command == "list":
        include_active = not args.closed_only
        include_closed = not args.active_only
        asyncio.run(list_markets(
            count=args.count,
            include_active=include_active,
            include_closed=include_closed,
        ))

    elif args.command == "backtest":
        # Build strategy params from config + CLI overrides
        if yaml_config:
            strategy_params = yaml_config.get_strategy_params(
                target_cost=args.target_cost,
                ecr_threshold=args.ecr_threshold,
            )
        else:
            strategy_params = {
                "target_cost": args.target_cost,
                "batch_ratio": 0.001,
                "ecr_threshold": args.ecr_threshold,
                "enable_ecr_stoploss": True,
                "enable_rebalancing": True,
                "enable_trend_detection": True,
                "enable_urgency_pricing": True,
            }

        if args.slug:
            asyncio.run(backtest_single_market(
                slug=args.slug,
                position_size=args.position_size,
                strategy_params=strategy_params,
                output_dir=args.output_dir,
                config=yaml_config,
            ))
        elif args.count:
            asyncio.run(backtest_multiple_markets(
                count=args.count,
                position_size=args.position_size,
                strategy_params=strategy_params,
                output_dir=args.output_dir,
            ))
        else:
            backtest_parser.print_help()
            print("\n\nExamples:")
            print("  # Backtest a specific market:")
            print("  python scripts/run_simulation.py backtest --slug btc-updown-15m-XXXXXXX")
            print()
            print("  # Backtest 5 recent closed markets:")
            print("  python scripts/run_simulation.py backtest --count 5")

    elif args.command == "live":
        coins = [c.strip().lower() for c in args.markets.split(",")]
        duration = parse_duration(args.duration)

        # Quick test mode
        scan_interval = args.scan_interval
        if args.quick_test:
            scan_interval = 30.0

        asyncio.run(run_live_simulation(
            coins=coins,
            duration_seconds=duration,
            output_dir=args.output_dir,
            target_cost=args.target_cost,
            batch_ratio=args.batch_ratio,
            position_size=args.position_size,
            ecr_threshold=args.ecr_threshold,
            enable_ecr_stoploss=not args.disable_ecr_stoploss,
            enable_rebalancing=not args.disable_rebalancing,
            enable_trend_detection=not args.disable_trend_detection,
            market_scan_interval=scan_interval,
            resume=args.resume,
            yaml_config=yaml_config,
        ))

    else:
        parser.print_help()
        print("\n\nExamples:")
        print("  # List available markets:")
        print("  python scripts/run_simulation.py list")
        print()
        print("  # Backtest on a specific market:")
        print("  python scripts/run_simulation.py backtest --slug btc-updown-15m-XXXXXXX")
        print()
        print("  # Backtest on 5 recent markets:")
        print("  python scripts/run_simulation.py backtest --count 5")
        print()
        print("  # Run live simulation:")
        print("  python scripts/run_simulation.py live")
        print()
        print("  # Live simulation for 10 hours:")
        print("  python scripts/run_simulation.py live --duration 10h")


if __name__ == "__main__":
    main()
