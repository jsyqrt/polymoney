"""
Performance Analytics - Analyze and visualize test results.

Provides:
- Equity curve tracking
- Trade log analysis
- Key metrics computation
- Report generation
- Data export (CSV, JSON)
- Visualization (if matplotlib available)
"""

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from polymoney.core.logging import get_logger

from .runner import TestResult

logger = get_logger("testing.analytics")

# Try to import matplotlib for visualization
try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    logger.info("matplotlib not available, visualization disabled")


@dataclass
class EquityPoint:
    """A point on the equity curve."""

    timestamp: datetime
    up_shares: float
    down_shares: float
    up_cost: float
    down_cost: float
    market_value: float  # Current market value of position
    total_cost: float
    unrealized_pnl: float


@dataclass
class AggregateMetrics:
    """Aggregate metrics across multiple test runs."""

    total_markets: int = 0
    profitable_markets: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_cost: float = 0.0
    total_roi: float = 0.0
    avg_pnl: float = 0.0
    avg_roi: float = 0.0
    avg_effective_cost_rate: float = 0.0
    avg_balance_ratio: float = 0.0
    avg_fill_rate: float = 0.0
    best_pnl: float = 0.0
    worst_pnl: float = 0.0


class PerformanceAnalytics:
    """
    Analyze and visualize trading performance.

    Features:
    - Track equity curve over time
    - Record trade logs
    - Compute key metrics
    - Generate summary reports
    - Export data (CSV, JSON)
    - Visualization (if matplotlib available)
    """

    def __init__(self):
        """Initialize analytics."""
        self._results: List[TestResult] = []
        self._equity_curves: Dict[str, List[EquityPoint]] = {}

    def add_result(self, result: TestResult) -> None:
        """Add a test result for analysis."""
        self._results.append(result)
        logger.info(f"Added result for market {result.market_id}")

    def clear(self) -> None:
        """Clear all results."""
        self._results.clear()
        self._equity_curves.clear()

    @property
    def results(self) -> List[TestResult]:
        """Get all results."""
        return self._results

    def compute_aggregate_metrics(self) -> AggregateMetrics:
        """Compute aggregate metrics across all results."""
        if not self._results:
            return AggregateMetrics()

        total_pnl = sum(r.pnl for r in self._results)
        total_cost = sum(r.total_cost for r in self._results)
        profitable = sum(1 for r in self._results if r.pnl > 0)

        pnls = [r.pnl for r in self._results]
        rois = [r.roi for r in self._results if r.total_cost > 0]
        eff_costs = [r.effective_cost_rate for r in self._results if r.effective_cost_rate < 999]
        balances = [r.balance_ratio for r in self._results]
        fill_rates = [r.fill_rate for r in self._results]

        return AggregateMetrics(
            total_markets=len(self._results),
            profitable_markets=profitable,
            win_rate=profitable / len(self._results) if self._results else 0.0,
            total_pnl=total_pnl,
            total_cost=total_cost,
            total_roi=total_pnl / total_cost if total_cost > 0 else 0.0,
            avg_pnl=sum(pnls) / len(pnls) if pnls else 0.0,
            avg_roi=sum(rois) / len(rois) if rois else 0.0,
            avg_effective_cost_rate=sum(eff_costs) / len(eff_costs) if eff_costs else 0.0,
            avg_balance_ratio=sum(balances) / len(balances) if balances else 0.0,
            avg_fill_rate=sum(fill_rates) / len(fill_rates) if fill_rates else 0.0,
            best_pnl=max(pnls) if pnls else 0.0,
            worst_pnl=min(pnls) if pnls else 0.0,
        )

    def generate_report(self) -> str:
        """Generate a text summary report."""
        if not self._results:
            return "No results to report."

        metrics = self.compute_aggregate_metrics()

        lines = [
            "=" * 60,
            "PERFORMANCE ANALYTICS REPORT",
            "=" * 60,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## Summary",
            f"Total Markets Tested: {metrics.total_markets}",
            f"Profitable Markets: {metrics.profitable_markets}",
            f"Win Rate: {metrics.win_rate:.1%}",
            "",
            "## Financial Performance",
            f"Total Cost: ${metrics.total_cost:.2f}",
            f"Total PnL: ${metrics.total_pnl:.2f}",
            f"Total ROI: {metrics.total_roi:.2%}",
            f"Average PnL per Market: ${metrics.avg_pnl:.2f}",
            f"Average ROI: {metrics.avg_roi:.2%}",
            f"Best PnL: ${metrics.best_pnl:.2f}",
            f"Worst PnL: ${metrics.worst_pnl:.2f}",
            "",
            "## Strategy Metrics",
            f"Average Effective Cost Rate: {metrics.avg_effective_cost_rate:.2%}",
            f"Average Balance Ratio: {metrics.avg_balance_ratio:.2%}",
            f"Average Fill Rate: {metrics.avg_fill_rate:.2%}",
            "",
            "## Individual Results",
            "-" * 60,
        ]

        for i, r in enumerate(self._results, 1):
            lines.extend([
                f"\n### Market {i}: {r.market_id}",
                f"Settlement: {r.settlement_winner or 'unknown'}",
                f"Position: UP {r.up_shares:.1f} @ ${r.up_cost:.2f}, DOWN {r.down_shares:.1f} @ ${r.down_cost:.2f}",
                f"Total Cost: ${r.total_cost:.2f}",
                f"Settlement Value: ${r.settlement_value:.2f}",
                f"PnL: ${r.pnl:.2f} ({r.roi:.2%})",
                f"Effective Cost Rate: {r.effective_cost_rate:.2%}",
                f"Balance Ratio: {r.balance_ratio:.2%}",
                f"Orders: {r.orders_filled}/{r.orders_submitted} filled ({r.fill_rate:.1%})",
            ])

        lines.extend([
            "",
            "=" * 60,
            "END OF REPORT",
            "=" * 60,
        ])

        return "\n".join(lines)

    def export_results_json(self, filepath: str) -> None:
        """Export all results to JSON."""
        data = {
            "generated_at": datetime.now().isoformat(),
            "aggregate_metrics": self.compute_aggregate_metrics().__dict__,
            "results": [r.to_dict() for r in self._results],
        }

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info(f"Exported results to {filepath}")

    def export_equity_curve_csv(
        self,
        result: TestResult,
        filepath: str,
    ) -> None:
        """Export equity curve from a single result to CSV."""
        if not result.price_history:
            logger.warning("No price history in result")
            return

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)

        # Build equity curve from price history and trade log
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "up_price",
                "down_price",
                "up_shares",
                "down_shares",
                "total_cost",
                "market_value",
                "unrealized_pnl",
            ])

            # Track cumulative position from trade log
            up_shares = 0.0
            down_shares = 0.0
            up_cost = 0.0
            down_cost = 0.0

            trade_idx = 0
            fills = [t for t in result.trade_log if t["type"] == "order_filled"]

            for price_point in result.price_history:
                ts = price_point["timestamp"]
                up_price = price_point["up_price"]
                down_price = price_point["down_price"]

                # Apply any fills that happened before this price point
                while trade_idx < len(fills) and fills[trade_idx]["timestamp"] <= ts:
                    fill = fills[trade_idx]
                    if fill["token_type"] == "yes":
                        up_shares += fill["size"]
                        up_cost += fill["cost"]
                    else:
                        down_shares += fill["size"]
                        down_cost += fill["cost"]
                    trade_idx += 1

                total_cost = up_cost + down_cost
                market_value = up_shares * up_price + down_shares * down_price
                unrealized_pnl = market_value - total_cost

                writer.writerow([
                    ts,
                    f"{up_price:.4f}",
                    f"{down_price:.4f}",
                    f"{up_shares:.2f}",
                    f"{down_shares:.2f}",
                    f"{total_cost:.2f}",
                    f"{market_value:.2f}",
                    f"{unrealized_pnl:.2f}",
                ])

        logger.info(f"Exported equity curve to {filepath}")

    def export_trades_json(self, result: TestResult, filepath: str) -> None:
        """Export trade log to JSON."""
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)

        data = {
            "market_id": result.market_id,
            "strategy_id": result.strategy_id,
            "trades": result.trade_log,
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Exported trades to {filepath}")

    def plot_equity_curve(
        self,
        result: TestResult,
        filepath: str,
        show: bool = False,
    ) -> bool:
        """
        Plot equity curve.

        Returns True if plot was created, False if matplotlib unavailable.
        """
        if not HAS_MATPLOTLIB:
            logger.warning("matplotlib not available for plotting")
            return False

        if not result.price_history:
            logger.warning("No price history to plot")
            return False

        # Build data for plotting
        timestamps = []
        market_values = []
        costs = []
        unrealized_pnls = []

        up_shares = 0.0
        down_shares = 0.0
        up_cost = 0.0
        down_cost = 0.0

        trade_idx = 0
        fills = [t for t in result.trade_log if t["type"] == "order_filled"]

        for price_point in result.price_history:
            ts = datetime.fromisoformat(price_point["timestamp"])
            up_price = price_point["up_price"]
            down_price = price_point["down_price"]

            # Apply fills
            while trade_idx < len(fills):
                fill_ts = datetime.fromisoformat(fills[trade_idx]["timestamp"])
                if fill_ts <= ts:
                    fill = fills[trade_idx]
                    if fill["token_type"] == "yes":
                        up_shares += fill["size"]
                        up_cost += fill["cost"]
                    else:
                        down_shares += fill["size"]
                        down_cost += fill["cost"]
                    trade_idx += 1
                else:
                    break

            total_cost = up_cost + down_cost
            market_value = up_shares * up_price + down_shares * down_price

            timestamps.append(ts)
            market_values.append(market_value)
            costs.append(total_cost)
            unrealized_pnls.append(market_value - total_cost)

        # Create plot
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

        # Top: Market value and cost
        ax1.plot(timestamps, market_values, label="Market Value", color="blue")
        ax1.plot(timestamps, costs, label="Total Cost", color="orange", linestyle="--")
        ax1.set_ylabel("USD")
        ax1.set_title(f"Equity Curve - {result.market_id}")
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # Bottom: Unrealized PnL
        colors = ["green" if p >= 0 else "red" for p in unrealized_pnls]
        ax2.fill_between(timestamps, unrealized_pnls, 0, alpha=0.3, color="gray")
        ax2.plot(timestamps, unrealized_pnls, color="black", linewidth=0.5)
        ax2.axhline(y=0, color="black", linestyle="-", linewidth=0.5)
        ax2.set_ylabel("Unrealized PnL (USD)")
        ax2.set_xlabel("Time")
        ax2.grid(True, alpha=0.3)

        # Format x-axis
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

        plt.tight_layout()

        # Save
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(filepath, dpi=150)
        logger.info(f"Saved equity curve plot to {filepath}")

        if show:
            plt.show()
        else:
            plt.close()

        return True

    def plot_aggregate_results(self, filepath: str, show: bool = False) -> bool:
        """Plot aggregate results across all markets."""
        if not HAS_MATPLOTLIB:
            logger.warning("matplotlib not available for plotting")
            return False

        if not self._results:
            logger.warning("No results to plot")
            return False

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))

        # 1. PnL distribution
        ax1 = axes[0, 0]
        pnls = [r.pnl for r in self._results]
        colors = ["green" if p >= 0 else "red" for p in pnls]
        ax1.bar(range(len(pnls)), pnls, color=colors)
        ax1.axhline(y=0, color="black", linestyle="-", linewidth=0.5)
        ax1.set_xlabel("Market")
        ax1.set_ylabel("PnL (USD)")
        ax1.set_title("PnL by Market")

        # 2. Effective cost rate
        ax2 = axes[0, 1]
        eff_costs = [min(r.effective_cost_rate, 1.5) for r in self._results]
        ax2.bar(range(len(eff_costs)), eff_costs, color="blue", alpha=0.7)
        ax2.axhline(y=1.0, color="red", linestyle="--", label="Break-even")
        ax2.set_xlabel("Market")
        ax2.set_ylabel("Effective Cost Rate")
        ax2.set_title("Effective Cost Rate by Market")
        ax2.legend()

        # 3. Balance ratio
        ax3 = axes[1, 0]
        balances = [r.balance_ratio for r in self._results]
        ax3.bar(range(len(balances)), balances, color="purple", alpha=0.7)
        ax3.set_xlabel("Market")
        ax3.set_ylabel("Balance Ratio")
        ax3.set_title("Position Balance by Market")
        ax3.set_ylim(0, 1)

        # 4. Cumulative PnL
        ax4 = axes[1, 1]
        cum_pnl = []
        total = 0
        for r in self._results:
            total += r.pnl
            cum_pnl.append(total)
        ax4.plot(range(len(cum_pnl)), cum_pnl, marker="o", color="green")
        ax4.axhline(y=0, color="black", linestyle="-", linewidth=0.5)
        ax4.set_xlabel("Market")
        ax4.set_ylabel("Cumulative PnL (USD)")
        ax4.set_title("Cumulative PnL")
        ax4.grid(True, alpha=0.3)

        plt.tight_layout()

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(filepath, dpi=150)
        logger.info(f"Saved aggregate plot to {filepath}")

        if show:
            plt.show()
        else:
            plt.close()

        return True
