#!/usr/bin/env python3
"""
PolyMoney — unified CLI for backtesting, paper trading, and live trading.

Commands:
    list       List available Polymarket 15-minute markets
    backtest   Backtest strategy on historical market data
    run        Paper or live trading (default command)
    claim      Check and redeem unredeemed tokens to recover USDC balance

Usage:
    # Paper trading (default)
    python scripts/run_trading.py run

    # Live trading (real orders)
    python scripts/run_trading.py run --live

    # Shorthand — "run" is the default command when no subcommand given
    python scripts/run_trading.py
    python scripts/run_trading.py --live
    python scripts/run_trading.py -m btc,eth -d 10h

    # List markets
    python scripts/run_trading.py list

    # Backtest
    python scripts/run_trading.py backtest --slug btc-updown-15m-XXXXXXX
    python scripts/run_trading.py backtest --count 5

    # Claim unredeemed tokens
    python scripts/run_trading.py claim          # check + redeem all
    python scripts/run_trading.py claim --dry-run # check only, don't redeem

Output:
    simulation_results/
    ├── status.json      Current trading state
    ├── metrics.jsonl    Time-series metrics
    ├── results.jsonl    Per-market settlements
    └── trades.jsonl     Individual trade log

Monitoring:
    python -m polymoney.simulation.status_cli --tail
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from polymoney.config import StrategyConfig, load_config
from polymoney.core.logging import get_logger, setup_logging
from polymoney.data.real_data_fetcher import RealDataFetcher
from polymoney.data.storage import DataStorage
from polymoney.runner import TradingRunner
from polymoney.simulation import SimulationConfig
from polymoney.strategy.builtin.position_arbitrage import PositionArbitrageStrategy
from polymoney.testing import PerformanceAnalytics, TestRunner
from polymoney.testing.runner import TestConfig, TestResult

logger = get_logger("run_trading")


# =============================================================================
# Helpers
# =============================================================================

def parse_duration(s: str) -> float:
    """Parse duration string: '10h', '3d', '30m', '0'=infinite."""
    if not s or s == "0":
        return 0
    s = s.strip().lower()
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("d"):
        return float(s[:-1]) * 86400
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("s"):
        return float(s[:-1])
    return float(s)


# =============================================================================
# list
# =============================================================================

async def cmd_list(args):
    """List available BTC 15-minute markets."""
    print(f"\nFetching {args.count} markets from Polymarket...")

    async with RealDataFetcher() as fetcher:
        markets = await fetcher.find_btc_15min_markets(
            count=args.count,
            include_active=not args.closed_only,
            include_closed=not args.active_only,
        )

    if not markets:
        print("No markets found.")
        return

    print(f"\nFound {len(markets)} markets:\n")
    for i, m in enumerate(markets, 1):
        status = "ACTIVE" if not m["closed"] else f"CLOSED ({m['winner']})"
        vol = float(m["volume"]) if m["volume"] else 0
        print(f"{i}. {m['title']}")
        print(f"   Slug: {m['slug']}")
        print(f"   Status: {status}")
        print(f"   Volume: ${vol:,.2f}")
        print()


# =============================================================================
# backtest
# =============================================================================

async def cmd_backtest_single(
    slug: str,
    position_size: float = 100.0,
    strategy_params: Optional[dict] = None,
    output_dir: str = "simulation_results",
    config: Optional[StrategyConfig] = None,
) -> TestResult:
    """Run backtest on a single market."""
    logger.info(f"Starting backtest: {slug}")

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

    async with RealDataFetcher() as fetcher:
        candlesticks, market_info = await fetcher.get_market_candlesticks(slug)

    if not candlesticks:
        raise ValueError(f"No market data for {slug}")

    logger.info(
        f"Got {len(candlesticks)} candlesticks for "
        f"{market_info.get('title', slug)}"
    )

    storage = DataStorage("sqlite+aiosqlite:///:memory:")
    await storage.initialize()

    market_id = market_info.get("condition_id", slug)
    for candle in candlesticks:
        candle.market_id = market_id
        await storage.save_candlestick(candle)

    tc = TestConfig(
        strategy_class=PositionArbitrageStrategy,
        strategy_params=strategy_params,
        position_size=position_size,
        replay_speed=0,
    )

    runner = TestRunner(storage=storage, config=tc)
    settlement = market_info.get("winner", "up")
    result = await runner.run_replay(
        market_id=market_id,
        title=market_info.get("title", slug),
        settlement_winner=settlement,
    )

    analytics = PerformanceAnalytics()
    analytics.add_result(result)

    output_path = Path(output_dir) / "backtest" / slug
    output_path.mkdir(parents=True, exist_ok=True)

    report = analytics.generate_report()
    report_file = (
        output_path / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    report_file.write_text(report)
    analytics.export_results_json(str(output_path / "results.json"))
    analytics.export_equity_curve_csv(
        result, str(output_path / "equity_curve.csv")
    )

    try:
        analytics.plot_equity_curve(
            result, str(output_path / "equity_curve.png")
        )
    except Exception as e:
        logger.warning(f"Could not generate plot: {e}")

    print()
    print("=" * 60)
    print(f"  BACKTEST: {market_info.get('title', slug)}")
    print("=" * 60)
    print(f"  Settlement : {settlement.upper()} won")
    print(f"  UP         : {result.up_shares:.2f} shares @ ${result.up_cost:.2f}")
    print(f"  DOWN       : {result.down_shares:.2f} shares @ ${result.down_cost:.2f}")
    print(f"  PnL        : ${result.pnl:.2f} ({result.roi:.2%})")
    print(f"  ECR        : {result.effective_cost_rate:.2%}")
    print(f"  Balance    : {result.balance_ratio:.2%}")
    print(f"  Fill Rate  : {result.fill_rate:.1%} ({result.orders_filled}/{result.orders_submitted})")
    print(f"  Output     : {output_path}")
    print("=" * 60)

    await storage.close()
    return result


async def cmd_backtest_multi(
    count: int = 5,
    position_size: float = 100.0,
    strategy_params: Optional[dict] = None,
    output_dir: str = "simulation_results",
):
    """Backtest on multiple recent closed markets."""
    print(f"\n{'=' * 60}")
    print(f"  MULTI-MARKET BACKTEST ({count} markets)")
    print(f"{'=' * 60}\n")

    async with RealDataFetcher() as fetcher:
        markets = await fetcher.find_btc_15min_markets(
            count=count, include_active=False, include_closed=True
        )

    if not markets:
        print("No closed markets found.")
        return

    print(f"Found {len(markets)} closed markets\n")
    results = []

    for i, m in enumerate(markets, 1):
        print(f"[{i}/{len(markets)}] {m['slug']} (winner: {m['winner']})")
        try:
            result = await cmd_backtest_single(
                slug=m["slug"],
                position_size=position_size,
                strategy_params=strategy_params,
                output_dir=output_dir,
            )
            results.append({
                "slug": m["slug"],
                "winner": m["winner"],
                "pnl": result.pnl,
                "roi": result.roi,
                "ecr": result.effective_cost_rate,
                "balance": result.balance_ratio,
                "fill_rate": result.fill_rate,
            })
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"slug": m["slug"], "error": str(e)})

    successful = [r for r in results if "pnl" in r]
    if successful:
        total_pnl = sum(r["pnl"] for r in successful)
        avg_roi = sum(r["roi"] for r in successful) / len(successful)
        win_count = sum(1 for r in successful if r["pnl"] > 0)

        print()
        print("=" * 60)
        print("  SUMMARY")
        print("=" * 60)
        print(f"  Tested  : {len(successful)}/{len(results)}")
        print(f"  Win Rate: {win_count}/{len(successful)} ({win_count / len(successful):.0%})")
        print(f"  PnL     : ${total_pnl:.2f}")
        print(f"  Avg ROI : {avg_roi:.2%}")
        for r in successful:
            s = "+" if r["pnl"] > 0 else "-"
            print(f"    {s} {r['slug']}: ${r['pnl']:.2f} ({r['roi']:.1%})")
        print("=" * 60)

    summary_path = Path(output_dir) / "backtest" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to: {summary_path}")


async def cmd_backtest(args, yaml_config):
    """Handle backtest command."""
    if yaml_config:
        sp = yaml_config.get_strategy_params(
            target_cost=args.target_cost, ecr_threshold=args.ecr_threshold
        )
    else:
        sp = {
            "target_cost": args.target_cost,
            "batch_ratio": 0.001,
            "ecr_threshold": args.ecr_threshold,
            "enable_ecr_stoploss": True,
            "enable_rebalancing": True,
            "enable_trend_detection": True,
            "enable_urgency_pricing": True,
        }

    if args.slug:
        await cmd_backtest_single(
            slug=args.slug,
            position_size=args.position_size,
            strategy_params=sp,
            output_dir=args.output_dir,
            config=yaml_config,
        )
    elif args.count:
        await cmd_backtest_multi(
            count=args.count,
            position_size=args.position_size,
            strategy_params=sp,
            output_dir=args.output_dir,
        )
    else:
        print("Specify --slug or --count. Examples:")
        print("  python scripts/run_trading.py backtest --slug btc-updown-15m-XXXXXXX")
        print("  python scripts/run_trading.py backtest --count 5")


# =============================================================================
# claim (check and redeem unredeemed tokens)
# =============================================================================

async def cmd_claim(args):
    """Check for unredeemed tokens and claim them to recover USDC balance."""
    from polymoney.core.config import PolymarketConfig
    from polymoney.execution.redeemer import PositionRedeemer

    pm_config = PolymarketConfig()
    if not pm_config.private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY is required.")
        print("Set it in .env or as an environment variable.")
        sys.exit(1)

    redeemer = PositionRedeemer(
        private_key=pm_config.private_key,
        funder=pm_config.funder,
        signature_type=pm_config.signature_type,
        builder_api_key=pm_config.builder_api_key,
        builder_secret=pm_config.builder_secret,
        builder_passphrase=pm_config.builder_passphrase,
    )

    if not redeemer.is_available:
        print("ERROR: polymarket-apis is required for redemption.")
        print("Install with: pip install polymarket-apis")
        sys.exit(1)

    print("=" * 60)
    print("  POLYMONEY — TOKEN CLAIM")
    print("=" * 60)
    print()

    # Fetch redeemable positions
    print("Scanning for unredeemed tokens...")
    positions = await redeemer.get_redeemable_positions()

    if not positions:
        print("No unredeemed tokens found. Balance is fully available.")
        return

    # Group by condition
    by_condition: dict = {}
    for pos in positions:
        by_condition.setdefault(pos.condition_id, []).append(pos)

    total_value = sum(p.current_value for p in positions)

    print(f"Found {len(positions)} unredeemed position(s) "
          f"across {len(by_condition)} market(s):")
    print()

    for i, (cid, cid_positions) in enumerate(by_condition.items(), 1):
        title = cid_positions[0].title or cid[:20] + "..."
        value = sum(p.current_value for p in cid_positions)
        outcomes = ", ".join(
            f"{p.outcome}: {p.size:.2f} shares" for p in cid_positions
        )
        print(f"  {i}. {title}")
        print(f"     Value: ${value:.2f}  ({outcomes})")
        print(f"     Condition: {cid[:32]}...")
        print()

    print(f"  Total redeemable: ${total_value:.2f}")
    print()

    if args.dry_run:
        print("Dry run — no redemptions performed.")
        return

    # Build work items: one per condition_id
    work_items = []
    for cid, cid_positions in by_condition.items():
        title = cid_positions[0].title or cid[:20] + "..."
        neg_risk = cid_positions[0].negative_risk
        amounts = [0.0, 0.0]
        cond_value = 0.0
        for pos in cid_positions:
            amounts[pos.outcome_index] = pos.size
            cond_value += pos.current_value
        outcomes_str = ", ".join(
            f"{p.outcome}: {p.size:.2f}" for p in cid_positions
        )
        work_items.append({
            "cid": cid, "title": title, "neg_risk": neg_risk,
            "amounts": amounts, "value": cond_value,
            "outcomes_str": outcomes_str,
        })

    # Relayer limit: 25 req/min. Space requests ~5s apart → 12 req/min.
    print(f"Redeeming {len(work_items)} condition(s) "
          f"(relayer limit: 25 req/min)...")
    print()

    total_redeemed = 0.0
    succeeded = []
    rate_limited = []
    other_failed = []

    for i, item in enumerate(work_items, 1):
        result = await redeemer.redeem_condition(
            condition_id=item["cid"],
            amounts=item["amounts"],
            neg_risk=item["neg_risk"],
            label=item["title"],
            value=item["value"],
        )

        if result.success:
            total_redeemed += result.value_redeemed
            succeeded.append(item)
            print(f"  [{i}/{len(work_items)}] OK  {item['title']}")
            print(f"       ${result.value_redeemed:.2f} "
                  f"({item['outcomes_str']})")
        elif result.error == "RATE_LIMITED":
            rate_limited.append(item)
            print(f"  [{i}/{len(work_items)}] 429 {item['title']}  "
                  f"(rate-limited, will retry)")
        else:
            other_failed.append((item, result.error))
            print(f"  [{i}/{len(work_items)}] FAIL {item['title']}")
            print(f"       {result.error}")

        if i < len(work_items):
            await asyncio.sleep(5.0)

    # Retry pass for rate-limited conditions after quota reset
    if rate_limited:
        wait_sec = 65
        print()
        print(f"  {len(rate_limited)} condition(s) rate-limited. "
              f"Waiting {wait_sec}s for quota reset...")
        await asyncio.sleep(wait_sec)
        print(f"  Retrying {len(rate_limited)} condition(s)...")
        print()

        for i, item in enumerate(rate_limited[:], 1):
            result = await redeemer.redeem_condition(
                condition_id=item["cid"],
                amounts=item["amounts"],
                neg_risk=item["neg_risk"],
                label=item["title"],
                value=item["value"],
            )

            if result.success:
                total_redeemed += result.value_redeemed
                succeeded.append(item)
                rate_limited.remove(item)
                print(f"  [retry {i}] OK  {item['title']}")
                print(f"       ${result.value_redeemed:.2f} "
                      f"({item['outcomes_str']})")
            else:
                print(f"  [retry {i}] FAIL {item['title']}")
                print(f"       {result.error}")

            if i < len(rate_limited) + 1:
                await asyncio.sleep(5.0)

    # Summary
    total_failed = len(rate_limited) + len(other_failed)
    print()
    print("-" * 60)
    print(f"  Redeemed : ${total_redeemed:.2f}  "
          f"({len(succeeded)}/{len(work_items)} conditions)")
    if rate_limited:
        print(f"  Rate-limited : {len(rate_limited)} condition(s) "
              f"— run again in ~1 min")
    if other_failed:
        print(f"  Other fails  : {len(other_failed)} condition(s)")
        for item, err in other_failed:
            print(f"    - {item['title']}: {err}")
    print("-" * 60)


# =============================================================================
# validate-signal (Binance signal validation)
# =============================================================================

async def cmd_validate_signal(args):
    """Run Binance signal validation — no trading, only measurement."""
    from scripts.validate_binance_signal import BinanceSignalValidator, parse_duration as sig_parse_duration

    coins = [c.strip().lower() for c in args.markets.split(",")]
    duration = sig_parse_duration(args.duration)

    validator = BinanceSignalValidator(
        coins=coins,
        timeframe=args.timeframe,
        output_dir=args.output_dir,
    )
    await validator.run(duration)


# =============================================================================
# run (paper / live trading)
# =============================================================================

async def cmd_run(args, yaml_config):
    """Run paper or live trading."""
    mode = "live" if args.live else "paper"
    coins = [c.strip().lower() for c in args.markets.split(",")]
    duration = parse_duration(args.duration)

    timeframes = [t.strip() for t in args.timeframes.split(",")]
    enable_binance = getattr(args, "enable_binance_feed", False)

    if yaml_config:
        config = yaml_config.to_simulation_config(
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
            market_scan_interval=args.scan_interval,
            min_trading_time=args.min_trading_time,
        )
        config.timeframes = timeframes
        config.enable_binance_feed = enable_binance
        config.strategy_version = getattr(args, "strategy_version", "v1")
    else:
        config = SimulationConfig(
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
            enable_urgency_pricing=True,
            market_scan_interval=args.scan_interval,
            metrics_output_interval=1.0,
            min_trading_time=args.min_trading_time,
            timeframes=timeframes,
            enable_binance_feed=enable_binance,
            strategy_version=getattr(args, "strategy_version", "v1"),
        )

    label = "LIVE TRADING" if mode == "live" else "PAPER TRADING"
    batch_size = max(args.position_size * args.batch_ratio, 0.10)

    print("=" * 60)
    print(f"  POLYMONEY {label}")
    print("=" * 60)
    print(f"  Coins     : {', '.join(c.upper() for c in coins)}")
    print(f"  Timeframes: {', '.join(timeframes)}")
    if duration > 0:
        print(f"  Duration  : {duration / 3600:.1f}h")
    else:
        print(f"  Duration  : Infinite (Ctrl+C to stop)")
    print(f"  Output    : {args.output_dir}/")
    print(f"  Position  : ${args.position_size:.0f} per market")
    print(f"  Batch     : ${batch_size:.2f} per order")
    print(f"  ECR limit : {args.ecr_threshold}")
    print(f"  Target    : {args.target_cost}")
    if enable_binance:
        print(f"  Binance   : ENABLED (directional signal)")
    if mode == "live":
        print()
        print("  *** LIVE — REAL ORDERS ON POLYMARKET CLOB ***")
        print("  *** Ensure POLYMARKET_PRIVATE_KEY is set  ***")
    print("=" * 60)
    print()

    runner = TradingRunner(config, mode=mode)
    try:
        stats = await runner.run(resume=args.resume)
    except KeyboardInterrupt:
        print("\nStopped.")
        return

    print()
    print("=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  Markets : {stats.markets_processed}")
    print(f"  Win Rate: {stats.win_rate * 100:.1f}%")
    print(f"  PnL     : ${stats.total_pnl:.2f}")
    print(f"  ROI     : {stats.total_roi * 100:.2f}%")
    print(f"  Sharpe  : {stats.sharpe_ratio:.2f}")
    print("=" * 60)


# =============================================================================
# CLI
# =============================================================================

def add_common_args(p):
    """Add args shared across subcommands."""
    p.add_argument(
        "--config", "-c", type=str, default=None,
        help="YAML config file (default: config/strategy_defaults.yaml if exists)",
    )
    p.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")


def add_run_args(p):
    """Add args for the 'run' subcommand (also used as top-level defaults)."""
    p.add_argument("--live", action="store_true",
                    help="Live trading (real orders). Default: paper")
    p.add_argument("-m", "--markets", type=str, default="btc,eth,sol",
                    help="Coins (default: btc,eth,sol)")
    p.add_argument("-d", "--duration", type=str, default="0",
                    help="Duration: 10h, 3d, 30m, 0=infinite (default: 0)")
    p.add_argument("-o", "--output-dir", type=Path,
                    default=Path("simulation_results"))
    p.add_argument("--resume", action="store_true")
    g = p.add_argument_group("strategy")
    g.add_argument("--target-cost", type=float, default=0.96)
    g.add_argument("--batch-ratio", type=float, default=0.001)
    g.add_argument("--position-size", type=float, default=100.0,
                    help="USD per market (default: 100)")
    g.add_argument("--ecr-threshold", type=float, default=1.05)
    g.add_argument("--min-trading-time", type=float, default=300.0)
    g.add_argument("--scan-interval", type=float, default=60.0)
    g.add_argument("--disable-ecr-stoploss", action="store_true")
    g.add_argument("--disable-rebalancing", action="store_true")
    g.add_argument("--disable-trend-detection", action="store_true")
    g.add_argument("--timeframes", type=str, default="15m",
                    help="Comma-separated timeframes to trade (default: 15m). "
                         "Options: 5m,15m,1h,4h")
    g.add_argument("--enable-binance-feed", action="store_true",
                    help="Enable Binance real-time price feed for directional signal")
    g.add_argument("--disable-directional-signal", action="store_true",
                    help="Disable late-game directional signal even when Binance feed is on")
    g.add_argument("--strategy-version", type=str, default="v1",
                    choices=["v1", "v2"],
                    help="Strategy version: v1 (maker-only) or v2 (taker-hybrid)")


def main():
    parser = argparse.ArgumentParser(
        prog="run_trading",
        description="PolyMoney — backtest, paper trade, and live trade on Polymarket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_trading.py                            # paper trading
  python scripts/run_trading.py --live                     # live trading
  python scripts/run_trading.py -m btc,eth -d 10h         # custom
  python scripts/run_trading.py --timeframes 1h,4h         # multi-timeframe
  python scripts/run_trading.py list                       # show markets
  python scripts/run_trading.py backtest --count 5         # backtest
  python scripts/run_trading.py validate-signal -d 1h      # test Binance signal
  python scripts/run_trading.py claim                      # redeem tokens
  python scripts/run_trading.py claim --dry-run            # check only
""",
    )
    sub = parser.add_subparsers(dest="command")

    # --- list ---
    p_list = sub.add_parser("list", help="List available markets")
    p_list.add_argument("-n", "--count", type=int, default=10)
    p_list.add_argument("--active-only", action="store_true")
    p_list.add_argument("--closed-only", action="store_true")

    # --- backtest ---
    p_bt = sub.add_parser("backtest", help="Backtest on historical data")
    p_bt.add_argument("--slug", type=str, help="Market slug")
    p_bt.add_argument("-n", "--count", type=int, help="Number of markets")
    p_bt.add_argument("--position-size", type=float, default=100.0)
    p_bt.add_argument("--target-cost", type=float, default=0.96)
    p_bt.add_argument("--ecr-threshold", type=float, default=1.05)
    p_bt.add_argument("-o", "--output-dir", type=str, default="simulation_results")

    # --- run ---
    p_run = sub.add_parser("run", help="Paper or live trading")
    add_run_args(p_run)

    # --- validate-signal ---
    p_sig = sub.add_parser(
        "validate-signal",
        help="Test Binance price signal lead and accuracy (no trading)",
    )
    p_sig.add_argument("-m", "--markets", type=str, default="btc,eth,sol",
                       help="Coins to monitor")
    p_sig.add_argument("-d", "--duration", type=str, default="30m",
                       help="Test duration (30m, 2h, 1d)")
    p_sig.add_argument("--timeframe", type=str, default="15m",
                       help="Market timeframe (15m, 1h, 4h)")
    p_sig.add_argument("-o", "--output-dir", type=Path,
                       default=Path("signal_validation"),
                       help="Output directory")

    # --- claim ---
    p_claim = sub.add_parser("claim", help="Check and redeem unredeemed tokens")
    p_claim.add_argument(
        "--dry-run", action="store_true",
        help="Only check, don't actually redeem",
    )

    # Common args on the top-level parser too (for shorthand usage)
    add_run_args(parser)
    add_common_args(parser)
    add_common_args(p_list)
    add_common_args(p_bt)
    add_common_args(p_run)
    add_common_args(p_sig)
    add_common_args(p_claim)

    args = parser.parse_args()

    # Logging
    setup_logging("DEBUG" if args.verbose else args.log_level)

    # YAML config
    yaml_config = None
    if args.config:
        yaml_config = load_config(args.config)
        logger.info(f"Config: {args.config}")
    else:
        try:
            yaml_config = load_config()
        except FileNotFoundError:
            pass

    # Dispatch
    if args.command == "list":
        asyncio.run(cmd_list(args))
    elif args.command == "backtest":
        asyncio.run(cmd_backtest(args, yaml_config))
    elif args.command == "validate-signal":
        asyncio.run(cmd_validate_signal(args))
    elif args.command == "claim":
        asyncio.run(cmd_claim(args))
    elif args.command == "run":
        asyncio.run(cmd_run(args, yaml_config))
    elif args.command is None:
        # No subcommand → default to "run" (paper trading)
        asyncio.run(cmd_run(args, yaml_config))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
