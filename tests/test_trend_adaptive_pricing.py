"""Tests for trend-adaptive hedging pricing in PositionArbitrageStrategy."""

import time
from datetime import datetime, timedelta

import pytest

from polymoney.core.models import PriceData
from polymoney.strategy.builtin.position_arbitrage import (
    PositionArbitrageStrategy,
    TrendDetector,
)


def make_strategy(**overrides) -> PositionArbitrageStrategy:
    defaults = dict(
        strategy_id="test",
        name="test",
        position_size=100.0,
        params={
            "target_cost": 0.96,
            "trend_patience_threshold": 0.65,
            "max_skew_threshold": 0.85,
            "low_prob_threshold": 0.05,
            "enable_trend_detection": True,
            "momentum_window_seconds": 5.0,
            "max_momentum_threshold": 0.10,
        },
    )
    defaults.update(overrides)
    return PositionArbitrageStrategy(**defaults)


def seed_trend(strategy: PositionArbitrageStrategy, side: str,
               start_price: float, end_price: float, steps: int = 20):
    """Feed price history that creates a strong trend."""
    base_ts = time.time() - 10.0
    for i in range(steps):
        t = i / (steps - 1)
        if side == "up":
            up = start_price + (end_price - start_price) * t
            down = 1.0 - up
        else:
            down = start_price + (end_price - start_price) * t
            up = 1.0 - down
        strategy._trend_detector.update(up, down, timestamp=base_ts + i * 0.5)


class TestTrendAdaptivePricing:
    """Verify _calculate_limit_prices under various trend conditions."""

    def test_proportional_in_balanced_market(self):
        """When market is balanced (both ~50%), use proportional scaling."""
        s = make_strategy()
        s.market_start_time = datetime.now() - timedelta(seconds=400)
        s.current_up_price = 0.50
        s.current_down_price = 0.50
        up_lim, down_lim = s._calculate_limit_prices(0.50, 0.50)

        assert abs(up_lim - down_lim) < 0.001, "balanced market should give equal limits"
        assert abs(up_lim + down_lim - 0.96) < 0.01, f"pair sum {up_lim+down_lim} != 0.96"

    def test_asymmetric_medium_confidence(self):
        """Medium trend confidence (30-70%) → asymmetric allocation, not maker-aggressive."""
        s = make_strategy()
        s.market_start_time = datetime.now() - timedelta(seconds=400)  # Phase 2
        s.current_up_price = 0.70
        s.current_down_price = 0.30

        # Seed a moderate UP trend (< 70% confidence)
        seed_trend(s, "up", 0.60, 0.70, steps=10)
        _, conf = s._trend_detector.get_trend()
        # If confidence happens to be >= 0.70, reduce the spread
        if conf >= 0.70:
            s._trend_detector._up_history = []
            s._trend_detector._down_history = []
            seed_trend(s, "up", 0.66, 0.70, steps=10)
            _, conf = s._trend_detector.get_trend()

        up_lim, down_lim = s._calculate_limit_prices(0.70, 0.30)
        pair = up_lim + down_lim
        assert pair <= 0.96 + 0.001, f"pair sum {pair} exceeds target"
        # In medium confidence, UP should get tighter limit but NOT 0.5% below market
        assert up_lim < 0.70, "UP limit should be below market"
        assert up_lim < 0.70 * 0.995 or conf >= 0.70, \
            f"UP limit {up_lim} should be less aggressive than maker-aggressive at medium confidence"

    def test_maker_aggressive_high_confidence(self):
        """High confidence (>=70%) in Phase 2+ → maker-aggressive on trending side."""
        s = make_strategy()
        s.market_start_time = datetime.now() - timedelta(seconds=400)  # Phase 2

        # Seed a strong UP trend
        seed_trend(s, "up", 0.55, 0.72, steps=20)
        trend_side, conf = s._trend_detector.get_trend()
        assert trend_side == "up", f"expected 'up' trend, got '{trend_side}'"
        assert conf >= 0.70, f"expected confidence >= 0.70, got {conf}"

        s.current_up_price = 0.72
        s.current_down_price = 0.28

        up_lim, down_lim = s._calculate_limit_prices(0.72, 0.28)
        pair = up_lim + down_lim

        # Pair sum should be exactly effective_target (0.96)
        assert abs(pair - 0.96) < 0.01, f"pair sum {pair} != 0.96"

        # UP limit should be ~0.5% below market = 0.72 * 0.995 = 0.7164
        expected_up = 0.72 * 0.995
        assert abs(up_lim - expected_up) < 0.002, \
            f"UP limit {up_lim} should be ~{expected_up} (0.5% below market)"

        # DOWN limit gets all remaining discount
        expected_down = 0.96 - expected_up
        assert abs(down_lim - expected_down) < 0.002, \
            f"DOWN limit {down_lim} should be ~{expected_down}"

        # DOWN limit should be well below its market price (very conservative)
        assert down_lim < 0.28, f"DOWN limit {down_lim} should be << market 0.28"

    def test_maker_aggressive_down_trend(self):
        """High confidence DOWN trend → maker-aggressive on DOWN side."""
        s = make_strategy()
        s.market_start_time = datetime.now() - timedelta(seconds=400)

        # Seed a strong DOWN trend (UP falling, DOWN rising)
        seed_trend(s, "down", 0.55, 0.72, steps=20)
        trend_side, conf = s._trend_detector.get_trend()
        assert trend_side == "down", f"expected 'down' trend, got '{trend_side}'"
        assert conf >= 0.70, f"expected confidence >= 0.70, got {conf}"

        s.current_up_price = 0.28
        s.current_down_price = 0.72

        up_lim, down_lim = s._calculate_limit_prices(0.28, 0.72)
        pair = up_lim + down_lim

        assert abs(pair - 0.96) < 0.01, f"pair sum {pair} != 0.96"

        # DOWN should be maker-aggressive
        expected_down = 0.72 * 0.995
        assert abs(down_lim - expected_down) < 0.002, \
            f"DOWN limit {down_lim} should be ~{expected_down}"

        # UP gets remaining discount
        assert up_lim < 0.28, f"UP limit {up_lim} should be << 0.28"

    def test_no_maker_aggressive_in_phase1(self):
        """Phase 1 (even with high confidence) should NOT use maker-aggressive."""
        s = make_strategy()
        s.market_start_time = datetime.now() - timedelta(seconds=10)  # Phase 1

        seed_trend(s, "up", 0.55, 0.72, steps=20)
        trend_side, conf = s._trend_detector.get_trend()
        assert conf >= 0.70

        s.current_up_price = 0.72
        s.current_down_price = 0.28

        up_lim, down_lim = s._calculate_limit_prices(0.72, 0.28)
        pair = up_lim + down_lim

        assert pair <= 0.96 + 0.001
        # UP should NOT be at 0.5% below market (which would be 0.7164)
        assert up_lim < 0.72 * 0.995 - 0.001, \
            f"Phase 1 UP limit {up_lim} should be more conservative than maker-aggressive"

    def test_no_maker_aggressive_when_trend_opposes_dominant(self):
        """If trend direction ≠ dominant side, fall back to asymmetric."""
        s = make_strategy()
        s.market_start_time = datetime.now() - timedelta(seconds=400)

        # UP is dominant (0.72) but trend is DOWN (price dropping)
        seed_trend(s, "down", 0.20, 0.35, steps=20)
        trend_side, conf = s._trend_detector.get_trend()

        s.current_up_price = 0.72
        s.current_down_price = 0.28

        up_lim, down_lim = s._calculate_limit_prices(0.72, 0.28)
        pair = up_lim + down_lim

        assert pair <= 0.96 + 0.001
        # Should be regular asymmetric, not maker-aggressive
        if conf >= 0.70:
            # Trend opposes dominant, so should NOT be at 0.5% below
            assert up_lim < 0.72 * 0.995 - 0.001, \
                "trend opposing dominant should not use maker-aggressive"

    def test_minority_floor_with_extreme_skew(self):
        """When dominant is at 0.93, minority limit stays >= low_prob_threshold (0.05)."""
        s = make_strategy()
        s.market_start_time = datetime.now() - timedelta(seconds=400)

        # Extreme skew
        seed_trend(s, "up", 0.80, 0.93, steps=20)
        s.current_up_price = 0.93
        s.current_down_price = 0.07

        up_lim, down_lim = s._calculate_limit_prices(0.93, 0.07)

        assert down_lim >= 0.05, \
            f"DOWN limit {down_lim} should not go below low_prob_threshold 0.05"
        assert up_lim >= 0.05, \
            f"UP limit {up_lim} should not go below low_prob_threshold 0.05"
        pair = up_lim + down_lim
        assert pair <= 0.96 + 0.001, f"pair sum {pair} exceeds target"

    def test_pair_sum_never_exceeds_target(self):
        """Across various price/trend combos, pair sum must stay <= effective_target."""
        combos = [
            (0.50, 0.50), (0.60, 0.40), (0.70, 0.30),
            (0.75, 0.25), (0.80, 0.20), (0.85, 0.15),
        ]
        for up_p, down_p in combos:
            s = make_strategy()
            s.market_start_time = datetime.now() - timedelta(seconds=400)
            s.current_up_price = up_p
            s.current_down_price = down_p

            # Seed strong UP trend
            seed_trend(s, "up", up_p - 0.15, up_p, steps=20)

            up_lim, down_lim = s._calculate_limit_prices(up_p, down_p)
            pair = up_lim + down_lim
            assert pair <= 0.96 + 0.001, \
                f"pair sum {pair} > 0.96 for prices ({up_p}, {down_p})"
            assert up_lim >= 0.05, \
                f"UP limit {up_lim} < floor for prices ({up_p}, {down_p})"
            assert down_lim >= 0.05, \
                f"DOWN limit {down_lim} < floor for prices ({up_p}, {down_p})"

    def test_trend_detector_confidence_above_threshold(self):
        """Verify TrendDetector produces >= 0.70 confidence for large momentum."""
        td = TrendDetector(window_seconds=5.0, max_momentum_threshold=0.10)
        base_ts = time.time() - 10.0
        for i in range(20):
            up = 0.55 + 0.17 * (i / 19)  # 0.55 → 0.72
            td.update(up, 1.0 - up, timestamp=base_ts + i * 0.5)

        side, conf = td.get_trend()
        assert side == "up"
        assert conf >= 0.70, f"confidence {conf} should be >= 0.70"
