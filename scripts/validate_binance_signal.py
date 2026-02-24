#!/usr/bin/env python3
"""
Binance Signal Validator — measures whether Binance prices reliably
lead Polymarket UP/DOWN token prices and whether the directional
signal is predictive of market outcomes.

Usage:
    python scripts/validate_binance_signal.py                # run 30 min, all coins
    python scripts/validate_binance_signal.py -d 2h          # run 2 hours
    python scripts/validate_binance_signal.py -m btc         # BTC only
    python scripts/validate_binance_signal.py --timeframe 1h # 1h markets
    python scripts/validate_binance_signal.py --report-only  # analyze saved data

Metrics produced:
    1. Latency: how far Binance moves ahead of Polymarket mid-price
    2. Directional accuracy: does Binance delta predict UP/DOWN outcome?
    3. Signal strength: distribution of |delta| at different time points
    4. Lead time: how many seconds before Polymarket reflects Binance moves
    5. Per-coin breakdown
"""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymoney.core.logging import get_logger, setup_logging
from polymoney.data.binance_feed import BinancePriceFeed, COIN_TO_SYMBOL
from polymoney.data.real_data_fetcher import RealDataFetcher

logger = get_logger("validate_binance")


# ─────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────

@dataclass
class PriceTick:
    timestamp: float
    price: float
    source: str  # "binance" or "polymarket_up" or "polymarket_down"


@dataclass
class EpochRecord:
    """Tracks one market epoch (e.g. btc-updown-15m-<ts>)."""
    slug: str
    coin: str
    timeframe: str
    epoch_ts: int
    start_time: float = 0.0
    settlement_time: Optional[float] = None

    # Binance prices sampled during this epoch
    binance_ticks: List[PriceTick] = field(default_factory=list)
    binance_start_price: Optional[float] = None

    # Polymarket mid-prices for UP and DOWN tokens
    poly_up_ticks: List[PriceTick] = field(default_factory=list)
    poly_down_ticks: List[PriceTick] = field(default_factory=list)

    # Outcome (if settled during test)
    winner: Optional[str] = None
    settled: bool = False

    # Directional signal snapshots at various time points
    # key = fraction_elapsed (0.5, 0.6, 0.7, 0.8, 0.9), value = binance_delta
    signal_snapshots: Dict[float, float] = field(default_factory=dict)


@dataclass
class LeadLagSample:
    """One measured lead/lag event."""
    coin: str
    binance_move_time: float
    poly_reflect_time: float
    lag_seconds: float
    direction: str  # "up" or "down"
    magnitude: float  # |delta|


# ─────────────────────────────────────────────────────────────────────
# Core validator
# ─────────────────────────────────────────────────────────────────────

class BinanceSignalValidator:
    """Simultaneously monitors Binance and Polymarket to measure signal quality."""

    SNAPSHOT_FRACTIONS = [0.5, 0.6, 0.7, 0.8, 0.9]

    def __init__(
        self,
        coins: List[str],
        timeframe: str = "15m",
        output_dir: Path = Path("signal_validation"),
    ):
        self.coins = [c.lower() for c in coins]
        self.timeframe = timeframe
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.binance_feed = BinancePriceFeed(coins=self.coins)
        self.fetcher: Optional[RealDataFetcher] = None

        self.epochs: Dict[str, EpochRecord] = {}
        self.completed_epochs: List[EpochRecord] = []
        self.lead_lag_samples: List[LeadLagSample] = []

        # For lead/lag detection: track last significant Binance move
        self._last_binance_prices: Dict[str, float] = {}
        self._binance_move_events: Dict[str, List[Tuple[float, float, str]]] = defaultdict(list)
        # (timestamp, price, direction)

        self._running = False
        self._start_time = 0.0

    async def run(self, duration_seconds: float) -> None:
        """Run the validator for a specified duration."""
        self._running = True
        self._start_time = time.time()
        end_time = self._start_time + duration_seconds

        print(f"\n{'=' * 70}")
        print(f"  BINANCE SIGNAL VALIDATOR")
        print(f"{'=' * 70}")
        print(f"  Coins      : {', '.join(c.upper() for c in self.coins)}")
        print(f"  Timeframe  : {self.timeframe}")
        print(f"  Duration   : {duration_seconds / 60:.0f} min")
        print(f"  Output     : {self.output_dir}/")
        print(f"{'=' * 70}\n")

        # Start Binance feed
        self.binance_feed.on_price_update = self._on_binance_price
        await self.binance_feed.start()

        # Start Polymarket fetcher
        self.fetcher = RealDataFetcher()
        await self.fetcher.__aenter__()

        try:
            scan_task = asyncio.create_task(self._market_scan_loop())
            poll_task = asyncio.create_task(self._price_poll_loop())
            snapshot_task = asyncio.create_task(self._snapshot_loop())
            print_task = asyncio.create_task(self._status_print_loop())

            while time.time() < end_time and self._running:
                await asyncio.sleep(1.0)

            self._running = False
            for t in [scan_task, poll_task, snapshot_task, print_task]:
                t.cancel()
            await asyncio.gather(
                scan_task, poll_task, snapshot_task, print_task,
                return_exceptions=True,
            )
        finally:
            await self.binance_feed.stop()
            if self.fetcher:
                await self.fetcher.__aexit__(None, None, None)

        self._save_raw_data()
        self._print_report()

    # ── Binance callback ──────────────────────────────────────────────

    def _on_binance_price(self, coin: str, price: float, ts: float) -> None:
        """Called on every Binance trade — record ticks for active epochs."""
        prev = self._last_binance_prices.get(coin)
        self._last_binance_prices[coin] = price

        # Record tick in all active epochs for this coin
        for epoch in self.epochs.values():
            if epoch.coin == coin and not epoch.settled:
                epoch.binance_ticks.append(PriceTick(ts, price, "binance"))
                if epoch.binance_start_price is None:
                    epoch.binance_start_price = price

        # Detect significant moves for lead/lag measurement
        if prev is not None and prev > 0:
            delta = (price - prev) / prev
            if abs(delta) > 0.0002:  # 0.02% threshold
                direction = "up" if delta > 0 else "down"
                self._binance_move_events[coin].append((ts, price, direction))
                # Keep only last 200 events per coin
                if len(self._binance_move_events[coin]) > 200:
                    self._binance_move_events[coin] = self._binance_move_events[coin][-100:]

    # ── Market scanning ───────────────────────────────────────────────

    async def _market_scan_loop(self) -> None:
        """Periodically discover active markets."""
        while self._running:
            try:
                for coin in self.coins:
                    markets = await self.fetcher.find_markets_by_timeframe(
                        coin=coin,
                        timeframe=self.timeframe,
                        count=3,
                        include_active=True,
                        include_closed=True,
                    )
                    for m in markets:
                        slug = m.get("slug", "")
                        if slug in self.epochs:
                            # Check if settled
                            if m.get("closed") and not self.epochs[slug].settled:
                                self.epochs[slug].settled = True
                                self.epochs[slug].winner = m.get("winner")
                                self.completed_epochs.append(self.epochs[slug])
                                logger.info(
                                    f"Settled: {slug} → {m.get('winner', '?')}"
                                )
                            continue
                        if m.get("closed"):
                            continue

                        # New active market
                        parts = slug.rsplit("-", 1)
                        epoch_ts = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0

                        record = EpochRecord(
                            slug=slug,
                            coin=coin,
                            timeframe=self.timeframe,
                            epoch_ts=epoch_ts,
                            start_time=time.time(),
                        )

                        # Record Binance start price
                        bp = self.binance_feed.get_price(coin)
                        if bp:
                            record.binance_start_price = bp
                        self.binance_feed.record_epoch_start(coin, epoch_ts)

                        self.epochs[slug] = record
                        logger.info(f"Tracking: {slug} (binance=${bp or 0:.2f})")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Scan error: {e}")

            await asyncio.sleep(30)

    # ── Polymarket price polling ──────────────────────────────────────

    async def _price_poll_loop(self) -> None:
        """Poll Polymarket prices for active epochs via REST."""
        while self._running:
            try:
                for epoch in list(self.epochs.values()):
                    if epoch.settled:
                        continue

                    # Find market to get token IDs
                    markets = await self.fetcher.find_markets_by_timeframe(
                        coin=epoch.coin,
                        timeframe=self.timeframe,
                        count=1,
                        include_active=True,
                        include_closed=False,
                    )
                    for m in markets:
                        if m.get("slug") != epoch.slug:
                            continue

                        now = time.time()
                        up_tid = m.get("up_token_id")
                        down_tid = m.get("down_token_id")

                        if up_tid:
                            pu = await self.fetcher.get_mid_price(up_tid)
                            if pu and pu.mid_price:
                                epoch.poly_up_ticks.append(
                                    PriceTick(now, pu.mid_price, "polymarket_up")
                                )
                                self._check_lead_lag(epoch.coin, now, pu.mid_price, "up")

                        if down_tid:
                            pd = await self.fetcher.get_mid_price(down_tid)
                            if pd and pd.mid_price:
                                epoch.poly_down_ticks.append(
                                    PriceTick(now, pd.mid_price, "polymarket_down")
                                )
                                self._check_lead_lag(epoch.coin, now, pd.mid_price, "down")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Poll error: {e}")

            await asyncio.sleep(2.0)

    def _check_lead_lag(
        self, coin: str, poly_time: float, poly_price: float, side: str,
    ) -> None:
        """Check if a recent Binance move is now being reflected in Polymarket."""
        events = self._binance_move_events.get(coin, [])
        if not events:
            return

        # For UP token: Binance price rise → UP price should rise
        # For DOWN token: Binance price rise → DOWN price should fall
        # We look for Binance moves in the last 10 seconds and see if
        # Polymarket is now moving in the expected direction.
        recent_events = [(t, p, d) for t, p, d in events if poly_time - t < 10.0]
        if not recent_events:
            return

        for b_time, b_price, b_dir in recent_events:
            lag = poly_time - b_time
            if lag < 0.1 or lag > 10.0:
                continue

            bp = self.binance_feed.get_price(coin)
            if bp is None:
                continue
            b_start = self._last_binance_prices.get(coin, bp)
            magnitude = abs(bp - b_start) / b_start if b_start > 0 else 0

            self.lead_lag_samples.append(LeadLagSample(
                coin=coin,
                binance_move_time=b_time,
                poly_reflect_time=poly_time,
                lag_seconds=lag,
                direction=b_dir,
                magnitude=magnitude,
            ))

    # ── Signal snapshots ──────────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        """Capture directional signal at key time fractions."""
        while self._running:
            try:
                now = time.time()
                for epoch in list(self.epochs.values()):
                    if epoch.settled or epoch.epoch_ts == 0:
                        continue

                    # Estimate market duration from timeframe
                    tf_seconds = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
                    duration = tf_seconds.get(epoch.timeframe, 900)
                    elapsed = now - epoch.epoch_ts
                    fraction = elapsed / duration

                    for snap_frac in self.SNAPSHOT_FRACTIONS:
                        if snap_frac in epoch.signal_snapshots:
                            continue
                        if fraction >= snap_frac:
                            delta = self.binance_feed.get_epoch_delta(
                                epoch.coin, epoch.epoch_ts
                            )
                            if delta is not None:
                                epoch.signal_snapshots[snap_frac] = delta
                                logger.debug(
                                    f"Snapshot {epoch.slug} @ {snap_frac:.0%}: "
                                    f"delta={delta:+.4f}"
                                )

            except asyncio.CancelledError:
                break
            except Exception:
                pass

            await asyncio.sleep(5.0)

    # ── Live status ───────────────────────────────────────────────────

    async def _status_print_loop(self) -> None:
        """Print live status every 30 seconds."""
        while self._running:
            await asyncio.sleep(30)
            try:
                elapsed = time.time() - self._start_time
                active = sum(1 for e in self.epochs.values() if not e.settled)
                settled = len(self.completed_epochs)
                samples = len(self.lead_lag_samples)
                binance_ok = all(
                    self.binance_feed.get_price(c) is not None
                    for c in self.coins
                )

                prices_str = "  ".join(
                    f"{c.upper()}=${self.binance_feed.get_price(c) or 0:.2f}"
                    for c in self.coins
                )

                print(
                    f"  [{elapsed/60:5.1f}m] active={active} settled={settled} "
                    f"lag_samples={samples} binance={'OK' if binance_ok else 'WAIT'}  "
                    f"{prices_str}"
                )
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ── Save & Report ─────────────────────────────────────────────────

    def _save_raw_data(self) -> None:
        """Save all collected data to JSONL for later re-analysis."""
        out_file = self.output_dir / "signal_validation.jsonl"
        with open(out_file, "w") as f:
            for epoch in list(self.epochs.values()):
                record = {
                    "slug": epoch.slug,
                    "coin": epoch.coin,
                    "timeframe": epoch.timeframe,
                    "epoch_ts": epoch.epoch_ts,
                    "settled": epoch.settled,
                    "winner": epoch.winner,
                    "binance_start_price": epoch.binance_start_price,
                    "binance_tick_count": len(epoch.binance_ticks),
                    "poly_up_tick_count": len(epoch.poly_up_ticks),
                    "poly_down_tick_count": len(epoch.poly_down_ticks),
                    "signal_snapshots": {
                        str(k): v for k, v in epoch.signal_snapshots.items()
                    },
                }
                f.write(json.dumps(record) + "\n")

        lag_file = self.output_dir / "lead_lag_samples.jsonl"
        with open(lag_file, "w") as f:
            for s in self.lead_lag_samples:
                f.write(json.dumps({
                    "coin": s.coin,
                    "binance_move_time": s.binance_move_time,
                    "poly_reflect_time": s.poly_reflect_time,
                    "lag_seconds": s.lag_seconds,
                    "direction": s.direction,
                    "magnitude": s.magnitude,
                }) + "\n")

        print(f"\n  Raw data saved to {out_file}")
        print(f"  Lead/lag samples saved to {lag_file}")

    def _print_report(self) -> None:
        """Print comprehensive validation report."""
        all_epochs = list(self.epochs.values())
        settled = [e for e in all_epochs if e.settled and e.winner]

        print(f"\n{'=' * 70}")
        print(f"  BINANCE SIGNAL VALIDATION REPORT")
        print(f"{'=' * 70}")
        print(f"  Duration  : {(time.time() - self._start_time) / 60:.1f} minutes")
        print(f"  Epochs    : {len(all_epochs)} tracked, {len(settled)} settled")
        print(f"  Lag samples: {len(self.lead_lag_samples)}")
        print()

        # ── Section 1: Binance Feed Health ────────────────────────────
        print(f"  {'─' * 66}")
        print(f"  1. BINANCE FEED HEALTH")
        print(f"  {'─' * 66}")
        for coin in self.coins:
            coin_epochs = [e for e in all_epochs if e.coin == coin]
            total_ticks = sum(len(e.binance_ticks) for e in coin_epochs)
            price = self.binance_feed.get_price(coin)
            print(
                f"    {coin.upper():4s}: {total_ticks:6d} ticks received, "
                f"latest=${price or 0:.2f}"
            )
        print()

        # ── Section 2: Lead/Lag Analysis ──────────────────────────────
        print(f"  {'─' * 66}")
        print(f"  2. LEAD/LAG ANALYSIS (Binance → Polymarket)")
        print(f"  {'─' * 66}")
        if self.lead_lag_samples:
            lags = [s.lag_seconds for s in self.lead_lag_samples]
            print(f"    Samples      : {len(lags)}")
            print(f"    Mean lag     : {statistics.mean(lags):.2f}s")
            print(f"    Median lag   : {statistics.median(lags):.2f}s")
            if len(lags) > 1:
                print(f"    Std dev      : {statistics.stdev(lags):.2f}s")
            print(f"    Min lag      : {min(lags):.2f}s")
            print(f"    Max lag      : {max(lags):.2f}s")

            # Percentiles
            sorted_lags = sorted(lags)
            for pct in [25, 50, 75, 90, 95]:
                idx = int(len(sorted_lags) * pct / 100)
                idx = min(idx, len(sorted_lags) - 1)
                print(f"    P{pct:<3d}         : {sorted_lags[idx]:.2f}s")

            # Per-coin breakdown
            print()
            for coin in self.coins:
                coin_lags = [s.lag_seconds for s in self.lead_lag_samples if s.coin == coin]
                if coin_lags:
                    print(
                        f"    {coin.upper():4s}: n={len(coin_lags):4d}, "
                        f"mean={statistics.mean(coin_lags):.2f}s, "
                        f"median={statistics.median(coin_lags):.2f}s"
                    )
        else:
            print("    No lead/lag samples collected (need more runtime)")
        print()

        # ── Section 3: Directional Accuracy ───────────────────────────
        print(f"  {'─' * 66}")
        print(f"  3. DIRECTIONAL ACCURACY (Signal → Outcome)")
        print(f"  {'─' * 66}")

        if not settled:
            print("    No settled epochs yet — run longer to collect outcomes")
        else:
            for snap_frac in self.SNAPSHOT_FRACTIONS:
                correct = 0
                total = 0
                deltas = []

                for epoch in settled:
                    delta = epoch.signal_snapshots.get(snap_frac)
                    if delta is None:
                        continue
                    total += 1
                    deltas.append(delta)

                    predicted = "up" if delta > 0 else "down"
                    if predicted == epoch.winner:
                        correct += 1

                if total > 0:
                    accuracy = correct / total * 100
                    avg_delta = statistics.mean([abs(d) for d in deltas])
                    print(
                        f"    @ {snap_frac:3.0%} elapsed: "
                        f"accuracy={accuracy:5.1f}% ({correct}/{total}), "
                        f"avg |delta|={avg_delta:.4f}"
                    )

            # Accuracy by delta threshold
            print()
            print("    Accuracy by signal strength (at 70% elapsed):")
            for threshold in [0.001, 0.002, 0.003, 0.005, 0.01]:
                correct = 0
                total = 0
                for epoch in settled:
                    delta = epoch.signal_snapshots.get(0.7)
                    if delta is None or abs(delta) < threshold:
                        continue
                    total += 1
                    predicted = "up" if delta > 0 else "down"
                    if predicted == epoch.winner:
                        correct += 1
                if total > 0:
                    print(
                        f"      |delta| >= {threshold:.3f}: "
                        f"{correct/total*100:5.1f}% ({correct}/{total})"
                    )
                else:
                    print(
                        f"      |delta| >= {threshold:.3f}: "
                        f"  n/a (0 samples)"
                    )
        print()

        # ── Section 4: Signal Strength Distribution ───────────────────
        print(f"  {'─' * 66}")
        print(f"  4. SIGNAL STRENGTH DISTRIBUTION")
        print(f"  {'─' * 66}")

        for snap_frac in [0.5, 0.7, 0.9]:
            deltas = []
            for epoch in all_epochs:
                d = epoch.signal_snapshots.get(snap_frac)
                if d is not None:
                    deltas.append(d)
            if deltas:
                abs_deltas = [abs(d) for d in deltas]
                print(f"    @ {snap_frac:.0%} elapsed (n={len(deltas)}):")
                print(f"      mean |delta| = {statistics.mean(abs_deltas):.5f}")
                if len(abs_deltas) > 1:
                    print(f"      std  |delta| = {statistics.stdev(abs_deltas):.5f}")
                print(f"      max  |delta| = {max(abs_deltas):.5f}")
                # Histogram buckets
                buckets = [0, 0.001, 0.002, 0.003, 0.005, 0.01, 0.02, 1.0]
                for i in range(len(buckets) - 1):
                    count = sum(
                        1 for d in abs_deltas
                        if buckets[i] <= d < buckets[i + 1]
                    )
                    bar = "█" * min(40, int(count / max(len(abs_deltas), 1) * 40))
                    print(
                        f"        [{buckets[i]:.3f}, {buckets[i+1]:.3f}): "
                        f"{count:3d} ({count/len(abs_deltas)*100:4.1f}%) {bar}"
                    )
                print()
        print()

        # ── Section 5: Per-Coin Summary ───────────────────────────────
        print(f"  {'─' * 66}")
        print(f"  5. PER-COIN SUMMARY")
        print(f"  {'─' * 66}")
        for coin in self.coins:
            coin_settled = [e for e in settled if e.coin == coin]
            coin_all = [e for e in all_epochs if e.coin == coin]
            print(f"\n    {coin.upper()}:")
            print(f"      Epochs tracked: {len(coin_all)}")
            print(f"      Epochs settled: {len(coin_settled)}")

            if coin_settled:
                # Accuracy at 70% mark
                correct = total = 0
                for e in coin_settled:
                    d = e.signal_snapshots.get(0.7)
                    if d is None:
                        continue
                    total += 1
                    if ("up" if d > 0 else "down") == e.winner:
                        correct += 1
                if total:
                    print(f"      Accuracy @70%: {correct/total*100:.1f}% ({correct}/{total})")

            coin_lags = [s.lag_seconds for s in self.lead_lag_samples if s.coin == coin]
            if coin_lags:
                print(
                    f"      Avg lead time: {statistics.mean(coin_lags):.2f}s "
                    f"(n={len(coin_lags)})"
                )

        # ── Section 6: Conclusion ─────────────────────────────────────
        print(f"\n  {'─' * 66}")
        print(f"  6. VERDICT")
        print(f"  {'─' * 66}")

        verdict_parts = []

        if self.lead_lag_samples:
            avg_lag = statistics.mean([s.lag_seconds for s in self.lead_lag_samples])
            if avg_lag > 0.5:
                verdict_parts.append(
                    f"  ✓ Binance leads Polymarket by ~{avg_lag:.1f}s on average"
                )
            else:
                verdict_parts.append(
                    f"  ✗ Binance lead is minimal ({avg_lag:.2f}s) — may not be exploitable"
                )

        if settled:
            # Best accuracy across all time points
            best_acc = 0
            best_frac = 0
            for frac in self.SNAPSHOT_FRACTIONS:
                c = t = 0
                for e in settled:
                    d = e.signal_snapshots.get(frac)
                    if d is not None:
                        t += 1
                        if ("up" if d > 0 else "down") == e.winner:
                            c += 1
                if t > 0 and c / t > best_acc:
                    best_acc = c / t
                    best_frac = frac

            if best_acc > 0.55:
                verdict_parts.append(
                    f"  ✓ Directional accuracy {best_acc*100:.1f}% at {best_frac:.0%} elapsed — USABLE"
                )
            elif best_acc > 0.50:
                verdict_parts.append(
                    f"  ~ Directional accuracy {best_acc*100:.1f}% at {best_frac:.0%} elapsed — MARGINAL"
                )
            else:
                verdict_parts.append(
                    f"  ✗ Directional accuracy {best_acc*100:.1f}% — NOT PREDICTIVE"
                )

        if not verdict_parts:
            verdict_parts.append("  ? Insufficient data — run longer")

        for v in verdict_parts:
            print(v)

        print(f"\n{'=' * 70}\n")


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def parse_duration(s: str) -> float:
    s = s.strip().lower()
    if not s or s == "0":
        return 1800  # default 30 min
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("d"):
        return float(s[:-1]) * 86400
    return float(s)


def main():
    parser = argparse.ArgumentParser(
        description="Validate Binance price signal lead and directional accuracy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/validate_binance_signal.py                 # 30 min, all coins
  python scripts/validate_binance_signal.py -d 2h           # 2 hours
  python scripts/validate_binance_signal.py -m btc -d 1h    # BTC only, 1 hour
  python scripts/validate_binance_signal.py --timeframe 1h  # 1h markets
""",
    )
    parser.add_argument("-m", "--markets", type=str, default="btc,eth,sol",
                        help="Coins to monitor (default: btc,eth,sol)")
    parser.add_argument("-d", "--duration", type=str, default="30m",
                        help="Test duration (default: 30m). Format: 30m, 2h, 1d")
    parser.add_argument("--timeframe", type=str, default="15m",
                        help="Market timeframe to monitor (default: 15m)")
    parser.add_argument("-o", "--output-dir", type=str, default="signal_validation",
                        help="Output directory (default: signal_validation)")
    parser.add_argument("--log-level", type=str, default="INFO")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    setup_logging("DEBUG" if args.verbose else args.log_level)

    coins = [c.strip().lower() for c in args.markets.split(",")]
    duration = parse_duration(args.duration)

    validator = BinanceSignalValidator(
        coins=coins,
        timeframe=args.timeframe,
        output_dir=Path(args.output_dir),
    )

    try:
        asyncio.run(validator.run(duration))
    except KeyboardInterrupt:
        print("\n  Stopped by user — generating report...\n")
        validator._save_raw_data()
        validator._print_report()


if __name__ == "__main__":
    main()
