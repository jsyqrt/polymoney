"""
Simulation Status CLI - Query running simulation status.

This module provides a command-line interface to:
- View current simulation status
- Query historical performance
- Export data in various formats
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


class SimStatusCLI:
    """Command-line interface for querying simulation status."""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or Path("simulation_results")
    
    @property
    def status_file(self) -> Path:
        return self.output_dir / "status.json"
    
    @property
    def metrics_file(self) -> Path:
        return self.output_dir / "metrics.jsonl"
    
    @property
    def results_file(self) -> Path:
        return self.output_dir / "results.jsonl"
    
    def _load_status(self) -> Optional[Dict[str, Any]]:
        """Load status.json if exists."""
        if not self.status_file.exists():
            return None
        
        try:
            with open(self.status_file) as f:
                return json.load(f)
        except json.JSONDecodeError:
            return None
    
    def _load_metrics(self, since_seconds: float = None) -> List[Dict[str, Any]]:
        """Load metrics from metrics.jsonl."""
        if not self.metrics_file.exists():
            return []
        
        metrics = []
        cutoff_time = time.time() - since_seconds if since_seconds else 0
        
        with open(self.metrics_file) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if since_seconds is None or entry.get("epoch", 0) >= cutoff_time:
                        metrics.append(entry)
                except json.JSONDecodeError:
                    continue
        
        return metrics
    
    def _load_results(self, market_filter: str = None) -> List[Dict[str, Any]]:
        """Load results from results.jsonl."""
        if not self.results_file.exists():
            return []
        
        results = []
        
        with open(self.results_file) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if market_filter is None or market_filter in entry.get("slug", ""):
                        results.append(entry)
                except json.JSONDecodeError:
                    continue
        
        return results
    
    def show_status(self, json_output: bool = False) -> int:
        """Show current simulation status."""
        status = self._load_status()
        
        if status is None:
            if json_output:
                print(json.dumps({"error": "No simulation data found"}))
            else:
                print("No simulation data found.")
                print(f"(Looking in: {self.output_dir})")
            return 0
        
        if json_output:
            print(json.dumps(status, indent=2))
            return 0
        
        # Human-readable output
        running = status.get("running", False)
        stats = status.get("stats", {})
        
        print("=" * 60)
        print("SIMULATION STATUS")
        print("=" * 60)
        print()
        
        if running:
            print(f"Status: RUNNING")
            start_time = status.get("start_time", "Unknown")
            print(f"Started: {start_time}")
        else:
            print(f"Status: STOPPED")
            last_update = status.get("last_update", "Unknown")
            print(f"Last Update: {last_update}")
        
        print()
        print("Configuration:")
        config = status.get("config", {})
        print(f"  Coins: {', '.join(config.get('coins', []))}")
        duration = config.get("duration_seconds", 0)
        if duration > 0:
            hours = duration / 3600
            print(f"  Duration: {hours:.1f} hours")
        else:
            print(f"  Duration: Infinite")
        
        print()
        print("Performance:")
        print(f"  Markets Processed: {stats.get('markets_processed', 0)}")
        print(f"  Win Rate: {stats.get('win_rate', 0)*100:.1f}%")
        print(f"  Total PnL: ${stats.get('total_pnl', 0):.2f}")
        print(f"  Total ROI: {stats.get('total_roi', 0)*100:.2f}%")
        print(f"  Sharpe Ratio: {stats.get('sharpe_ratio', 0):.2f}")
        
        active = status.get("active_markets", [])
        if active:
            print()
            print(f"Active Markets ({len(active)}):")
            for slug in active[:5]:
                print(f"  - {slug}")
            if len(active) > 5:
                print(f"  ... and {len(active) - 5} more")
        
        print("=" * 60)
        return 0
    
    def show_history(
        self,
        since: str = None,
        json_output: bool = False,
        export_file: str = None,
    ) -> int:
        """Show historical performance data."""
        # Parse since parameter
        since_seconds = None
        if since:
            since_seconds = self._parse_duration(since)
        
        metrics = self._load_metrics(since_seconds)
        results = self._load_results()
        
        if not metrics and not results:
            if json_output:
                print(json.dumps({"error": "No historical data found"}))
            else:
                print("No historical data found.")
            return 0
        
        if export_file:
            return self._export_csv(results, export_file)
        
        if json_output:
            output = {
                "metrics": metrics,
                "results": results,
            }
            print(json.dumps(output, indent=2))
            return 0
        
        # Human-readable output
        print("=" * 60)
        print("HISTORICAL PERFORMANCE")
        print("=" * 60)
        
        if metrics:
            print(f"\nMetrics entries: {len(metrics)}")
            if metrics:
                latest = metrics[-1]
                print(f"Latest metrics ({latest.get('timestamp', 'Unknown')}):")
                print(f"  PnL: ${latest.get('total_pnl', 0):.2f}")
                print(f"  ROI: {latest.get('total_roi', 0)*100:.2f}%")
                print(f"  Sharpe: {latest.get('sharpe_ratio', 0):.2f}")
        
        if results:
            print(f"\nMarket Results: {len(results)}")
            print()
            print(f"{'Slug':<40} {'Winner':<6} {'PnL':>10} {'ROI':>8}")
            print("-" * 70)
            
            for r in results[-10:]:  # Last 10
                slug = r.get("slug", "")[:38]
                winner = r.get("winner", "?")[:4]
                pnl = r.get("pnl", 0)
                roi = r.get("roi", 0) * 100
                print(f"{slug:<40} {winner:<6} ${pnl:>9.2f} {roi:>7.1f}%")
            
            if len(results) > 10:
                print(f"... and {len(results) - 10} more")
        
        print("=" * 60)
        return 0
    
    def show_market(self, market_slug: str, json_output: bool = False) -> int:
        """Show details for a specific market."""
        results = self._load_results(market_slug)
        
        if not results:
            if json_output:
                print(json.dumps({"error": f"No data for market: {market_slug}"}))
            else:
                print(f"No data found for market: {market_slug}")
            return 1
        
        # Find exact match or most recent match
        exact_match = None
        for r in results:
            if r.get("slug") == market_slug:
                exact_match = r
                break
        
        result = exact_match or results[-1]
        
        if json_output:
            print(json.dumps(result, indent=2))
            return 0
        
        print("=" * 60)
        print(f"MARKET: {result.get('slug', 'Unknown')}")
        print("=" * 60)
        print()
        print(f"Coin: {result.get('coin', 'Unknown')}")
        print(f"Winner: {result.get('winner', 'Pending')}")
        print()
        print("Positions:")
        print(f"  UP: {result.get('up_shares', 0):.2f} shares @ ${result.get('up_cost', 0):.2f}")
        print(f"  DOWN: {result.get('down_shares', 0):.2f} shares @ ${result.get('down_cost', 0):.2f}")
        print()
        print("Orders:")
        print(f"  Submitted: {result.get('orders_submitted', 0)}")
        print(f"  Filled: {result.get('orders_filled', 0)}")
        print()
        print("Results:")
        print(f"  PnL: ${result.get('pnl', 0):.2f}")
        print(f"  ROI: {result.get('roi', 0)*100:.2f}%")
        print(f"  ECR: {result.get('ecr', 0)*100:.2f}%")
        print(f"  Balance Ratio: {result.get('balance_ratio', 0)*100:.1f}%")
        print("=" * 60)
        return 0
    
    def list_markets(self, json_output: bool = False) -> int:
        """List all processed markets."""
        results = self._load_results()
        
        if not results:
            if json_output:
                print(json.dumps({"markets": []}))
            else:
                print("No markets found.")
            return 0
        
        # Sort by timestamp (most recent first)
        results.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        
        if json_output:
            markets = [
                {
                    "slug": r.get("slug"),
                    "coin": r.get("coin"),
                    "winner": r.get("winner"),
                    "pnl": r.get("pnl"),
                    "timestamp": r.get("timestamp"),
                }
                for r in results
            ]
            print(json.dumps({"markets": markets}, indent=2))
            return 0
        
        print("=" * 60)
        print("PROCESSED MARKETS")
        print("=" * 60)
        print()
        print(f"{'Slug':<45} {'Coin':<5} {'Winner':<6} {'PnL':>10}")
        print("-" * 70)
        
        for r in results:
            slug = r.get("slug", "")[:43]
            coin = r.get("coin", "?")[:4]
            winner = r.get("winner", "?")[:4]
            pnl = r.get("pnl", 0)
            print(f"{slug:<45} {coin:<5} {winner:<6} ${pnl:>9.2f}")
        
        print("-" * 70)
        print(f"Total: {len(results)} markets")
        print("=" * 60)
        return 0
    
    def tail(self, market_filter: str = None, json_output: bool = False) -> int:
        """Tail live metrics updates."""
        print("Tailing metrics (Ctrl+C to stop)...")
        print()
        
        last_position = 0
        
        try:
            while True:
                if self.metrics_file.exists():
                    with open(self.metrics_file) as f:
                        f.seek(last_position)
                        for line in f:
                            try:
                                entry = json.loads(line)
                                if json_output:
                                    print(json.dumps(entry))
                                else:
                                    ts = entry.get("timestamp", "")
                                    pnl = entry.get("total_pnl", 0)
                                    markets = entry.get("markets_processed", 0)
                                    active = entry.get("active_markets", 0)
                                    print(f"[{ts}] PnL=${pnl:.2f} | Markets={markets} | Active={active}")
                            except json.JSONDecodeError:
                                continue
                        last_position = f.tell()
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
    
    def _export_csv(self, results: List[Dict[str, Any]], filename: str) -> int:
        """Export results to CSV."""
        if not results:
            print("No data to export.")
            return 1
        
        fieldnames = [
            "timestamp", "slug", "coin", "winner",
            "pnl", "roi", "ecr", "balance_ratio",
            "up_shares", "down_shares", "up_cost", "down_cost",
            "orders_submitted", "orders_filled",
        ]
        
        with open(filename, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        
        print(f"Exported {len(results)} results to {filename}")
        return 0
    
    def _parse_duration(self, duration_str: str) -> float:
        """Parse duration string to seconds."""
        import re
        
        duration_str = duration_str.strip().lower()
        match = re.match(r'^(\d+(?:\.\d+)?)\s*([hdms]?)$', duration_str)
        
        if match:
            value = float(match.group(1))
            unit = match.group(2) or 'h'  # Default to hours
            
            multipliers = {
                's': 1,
                'm': 60,
                'h': 3600,
                'd': 86400,
            }
            return value * multipliers.get(unit, 3600)
        
        try:
            return float(duration_str) * 3600  # Assume hours
        except ValueError:
            return 3600  # Default 1 hour


def main():
    """Main entry point for sim-status command."""
    parser = argparse.ArgumentParser(
        description="Query simulation status and performance",
        prog="sim-status",
    )
    
    parser.add_argument(
        "--output-dir", "-d",
        type=Path,
        default=Path("simulation_results"),
        help="Simulation output directory",
    )
    
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output in JSON format",
    )
    
    # Subcommands via flags
    parser.add_argument(
        "--history",
        action="store_true",
        help="Show historical performance",
    )
    
    parser.add_argument(
        "--since",
        type=str,
        help="Filter history (e.g., '2h', '1d')",
    )
    
    parser.add_argument(
        "--export",
        type=str,
        metavar="FILE",
        help="Export history to CSV file",
    )
    
    parser.add_argument(
        "--market", "-m",
        type=str,
        help="Show specific market details",
    )
    
    parser.add_argument(
        "--list-markets",
        action="store_true",
        help="List all processed markets",
    )
    
    parser.add_argument(
        "--tail", "-f",
        action="store_true",
        help="Tail live metrics updates",
    )
    
    args = parser.parse_args()
    
    cli = SimStatusCLI(args.output_dir)
    
    if args.tail:
        return cli.tail(json_output=args.json)
    elif args.list_markets:
        return cli.list_markets(json_output=args.json)
    elif args.market:
        return cli.show_market(args.market, json_output=args.json)
    elif args.history:
        return cli.show_history(
            since=args.since,
            json_output=args.json,
            export_file=args.export,
        )
    else:
        return cli.show_status(json_output=args.json)


if __name__ == "__main__":
    sys.exit(main())
