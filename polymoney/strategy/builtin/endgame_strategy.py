"""
Endgame Strategy — Last-Moment Directional Trading + End-of-Life Arbitrage

Two sub-strategies activated only in the final minutes before settlement:

1. Directional Last-Moment (最后时刻方向性交易):
   Uses Binance real-time price delta to predict the winning side with high
   confidence. Targets mid-range prices ($0.15-$0.45) where the risk/reward
   asymmetry strongly favours us: buying at $0.25 risks only $0.25 but wins
   $0.75. EV calculation is conservative — it accounts for bid-ask spread
   and taker fees to ensure profitability carries over to live trading.

2. End-of-Life Arbitrage (临终套利):
   When the combined ask for UP+DOWN drops below $1.00, buys both sides for
   guaranteed profit at settlement. Works when market makers withdraw near
   settlement, creating pricing gaps.

Key design decisions (data-driven, 2026-03-07/08 simulation):
- Avoid $0.50+ prices: 43% win rate at $0.50-$0.70 (need 60% to break even)
- Focus on $0.15-$0.45: 50% win rate at breakeven-WR of only 19-42%
- Use conservative EV: add spread buffer + taker fee to mid-price
- Cap shares per order: stay within realistic order-book depth
"""

import time
from math import erf, sqrt
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    OrderSignal,
    OrderType,
    PriceData,
    TokenType,
    TradeSide,
)
from polymoney.core.strategy import BaseStrategy, register_strategy

logger = get_logger("strategy.endgame")


def win_probability(
    delta: float,
    time_remaining_s: float,
    vol_per_minute: float = 0.001,
) -> float:
    """Probability that current price direction holds to settlement.

    Models BTC/ETH/SOL price as geometric Brownian motion. To flip the
    outcome, the price must reverse by |delta| in the remaining time.

    Args:
        delta: Binance price delta since epoch start (e.g. 0.005 = +0.5%).
        time_remaining_s: Seconds until market settles.
        vol_per_minute: Annualized-style volatility per minute (std dev of
            1-minute returns).  BTC ≈ 0.001, ETH ≈ 0.0012, SOL ≈ 0.0015.

    Returns:
        Probability in [0.5, 1.0] that the sign of delta is correct at
        settlement.
    """
    if abs(delta) < 1e-9:
        return 0.5
    if time_remaining_s <= 0:
        return 1.0

    sigma = vol_per_minute * sqrt(max(time_remaining_s / 60.0, 0.01))
    z = abs(delta) / sigma
    return 0.5 * (1.0 + erf(z / 1.4142135623730951))  # sqrt(2)


COIN_PARAMS = {
    "btc": {"volatility": 0.0008, "min_delta": 0.0015, "max_shares": 20},
    "eth": {"volatility": 0.0012, "min_delta": 0.0012, "max_shares": 15},
    "sol": {"volatility": 0.0015, "min_delta": 0.0010, "max_shares": 10},
}


@register_strategy("endgame")
class EndgameStrategy(BaseStrategy):
    """
    Endgame trading strategy — trades only in the final minutes.

    Combines:
    - High-confidence directional bets using Binance price feed
    - Pure arbitrage when both sides are simultaneously cheap

    Does NOT trade during the first ~12 minutes of each 15-minute market.
    This avoids the fill-asymmetry and ECR-drift problems that plague
    the position arbitrage strategy.
    """

    def __init__(
        self,
        strategy_id: str = "endgame",
        name: str = "Endgame",
        position_size: float = 100.0,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(strategy_id, name, position_size, params)

        # --- Timing ---
        self.market_duration = self.params.get("market_duration", 900)
        self.directional_window = self.params.get("directional_window", 300)
        self.arb_window = self.params.get("arb_window", 120)

        # --- Directional parameters ---
        self.directional_min_delta = self.params.get("directional_min_delta", 0.001)
        self.min_win_prob = self.params.get("min_win_prob", 0.82)
        self.directional_min_price = self.params.get("directional_min_price", 0.15)
        self.directional_max_price = self.params.get("directional_max_price", 0.45)
        self.min_ev_per_dollar = self.params.get("min_ev_per_dollar", 0.06)
        self.base_volatility = self.params.get("volatility_per_minute", 0.001)
        self.min_spread_buffer = self.params.get("min_spread_buffer", 0.015)

        # --- Arbitrage parameters ---
        self.arb_threshold = self.params.get("arb_threshold", 0.985)
        self.arb_min_margin = self.params.get("arb_min_margin", 0.01)

        # --- Position sizing ---
        self.min_order_shares = self.params.get("min_order_shares", 5)
        self.trade_size_dollars = self.params.get("trade_size_dollars", 5.0)
        self.max_trades_per_market = self.params.get("max_trades_per_market", 2)
        self.trade_cooldown = self.params.get("trade_cooldown", 20.0)
        self.max_market_exposure = self.params.get("max_market_exposure", 25.0)
        self.max_shares_per_order = self.params.get("max_shares_per_order", 20)

        # --- Internal state ---
        self._up_shares: float = 0.0
        self._up_cost: float = 0.0
        self._down_shares: float = 0.0
        self._down_cost: float = 0.0

        self.market_start_time: Optional[datetime] = None
        self.market_settlement_time: Optional[float] = None
        self._coin: str = "btc"

        # Binance data — injected by runner each tick
        self.binance_delta: Optional[float] = None
        self.binance_price: Optional[float] = None

        self._trades_this_market: int = 0
        self._last_trade_time: float = 0.0
        self._logged_activation: bool = False
        self._logged_direction: Optional[str] = None

        # Compatibility stubs for runner/MarketContext expectations
        self.pending_orders: List = []
        self._exit_mode: bool = False

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    @property
    def strategy_type(self) -> str:
        return "directional"

    @property
    def description(self) -> str:
        return (
            f"Endgame (directional {self.directional_window}s, "
            f"arb {self.arb_window}s)"
        )

    # Compatibility properties expected by MarketContext.apply_fill / apply_sell
    @property
    def up_position(self):
        return self._PositionProxy(self, "up")

    @property
    def down_position(self):
        return self._PositionProxy(self, "down")

    class _PositionProxy:
        """Thin proxy so MarketContext can do strategy.up_position.add(...)."""

        def __init__(self, strategy: "EndgameStrategy", side: str):
            self._s = strategy
            self._side = side

        @property
        def shares(self) -> float:
            return self._s._up_shares if self._side == "up" else self._s._down_shares

        @shares.setter
        def shares(self, v: float):
            if self._side == "up":
                self._s._up_shares = v
            else:
                self._s._down_shares = v

        @property
        def cost(self) -> float:
            return self._s._up_cost if self._side == "up" else self._s._down_cost

        @cost.setter
        def cost(self, v: float):
            if self._side == "up":
                self._s._up_cost = v
            else:
                self._s._down_cost = v

        @property
        def avg_price(self) -> float:
            return self.cost / self.shares if self.shares > 0 else 0.0

        def add(self, shares: float, price: float):
            if self._side == "up":
                self._s._up_shares += shares
                self._s._up_cost += shares * price
            else:
                self._s._down_shares += shares
                self._s._down_cost += shares * price

        def reset(self):
            if self._side == "up":
                self._s._up_shares = 0.0
                self._s._up_cost = 0.0
            else:
                self._s._down_shares = 0.0
                self._s._down_cost = 0.0

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def _time_remaining(self) -> float:
        if self.market_settlement_time is not None:
            return max(0.0, self.market_settlement_time - time.time())
        if self.market_start_time is not None:
            elapsed = (datetime.now() - self.market_start_time).total_seconds()
            return max(0.0, self.market_duration - elapsed)
        return float("inf")

    def _total_exposure(self) -> float:
        return self._up_cost + self._down_cost

    def _get_volatility(self) -> float:
        return COIN_PARAMS.get(self._coin, {}).get("volatility", self.base_volatility)

    def _get_asset_params(self, market_id: str = "") -> Dict[str, Any]:
        """Get coin-specific trading parameters based on market_id or coin.

        Detects coin from market_id (e.g. 'BTC-12345' -> btc) and returns
        the corresponding volatility, min_delta, and max_shares parameters.

        Args:
            market_id: The market ID to detect coin type from.

        Returns:
            Dict with 'volatility', 'min_delta', 'max_shares' keys.
        """
        # Try to detect from market_id first
        market_upper = market_id.upper() if market_id else ""
        detected_coin = None
        if "BTC" in market_upper:
            detected_coin = "btc"
        elif "ETH" in market_upper:
            detected_coin = "eth"
        elif "SOL" in market_upper:
            detected_coin = "sol"

        # Use detected coin or fall back to self._coin
        coin = detected_coin if detected_coin else self._coin

        default_params = {
            "volatility": self.base_volatility,
            "min_delta": self.directional_min_delta,
            "max_shares": self.max_shares_per_order,
        }

        return COIN_PARAMS.get(coin, default_params)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_market_start(self, market_id: str, market_info: Dict[str, Any]) -> None:
        self.reset()
        self.market_start_time = datetime.now()
        self.market_settlement_time = market_info.get("settlement_time")
        self._coin = market_info.get("coin", "btc").lower()

        # Get coin-specific parameters
        asset_params = self._get_asset_params(market_id)
        self.directional_min_delta = asset_params["min_delta"]
        self.max_shares_per_order = asset_params["max_shares"]

        api_min = market_info.get("min_order_size")
        if api_min and api_min > 0:
            self.min_order_shares = api_min

        logger.info(
            f"[{self.name}] Market started: {market_id} "
            f"(coin={self._coin.upper()}, delta_thresh={self.directional_min_delta:.4f}, "
            f"max_shares={self.max_shares_per_order}, settlement in {self._time_remaining():.0f}s)"
        )

    def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
        if price_data.up_price <= 0 or price_data.down_price <= 0:
            return []

        price_sum = price_data.up_price + price_data.down_price
        if price_sum < 0.90 or price_sum > 1.10:
            return []

        remaining = self._time_remaining()

        # Too early — do nothing
        if remaining > self.directional_window:
            return []

        if not self._logged_activation:
            self._logged_activation = True
            logger.info(
                f"[{self.name}] ENDGAME ACTIVE: {remaining:.0f}s left, "
                f"UP={price_data.up_price:.3f} DOWN={price_data.down_price:.3f}"
            )

        # Respect trade limits
        if self._trades_this_market >= self.max_trades_per_market:
            return []
        if self._total_exposure() >= self.max_market_exposure:
            return []

        now = time.time()
        if now - self._last_trade_time < self.trade_cooldown:
            return []

        # === 1. End-of-Life Arbitrage (临终套利) ===
        if remaining <= self.arb_window:
            arb = self._check_arb(price_data, remaining)
            if arb:
                self._last_trade_time = now
                return arb

        # === 2. Directional Last-Moment (最后时刻方向性交易) ===
        directional = self._check_directional(price_data, remaining)
        if directional:
            self._last_trade_time = now
            return directional

        return []

    # ------------------------------------------------------------------
    # Sub-strategy: End-of-Life Arbitrage
    # ------------------------------------------------------------------

    def _check_arb(
        self, price_data: PriceData, remaining: float
    ) -> List[OrderSignal]:
        # Use conservative cost: mid → ask + fee for BOTH sides
        up_cost = self._estimate_all_in_cost(
            price_data.up_price, price_data.spread
        )
        down_cost = self._estimate_all_in_cost(
            price_data.down_price, price_data.spread
        )
        combined_cost = up_cost + down_cost
        margin = 1.0 - combined_cost

        if margin < self.arb_min_margin:
            return []

        headroom = self.max_market_exposure - self._total_exposure()
        budget = min(self.trade_size_dollars * 2, headroom)
        if budget < 2.0:
            return []

        shares = min(budget / combined_cost, self.max_shares_per_order)
        if shares < self.min_order_shares:
            return []

        up_fill = min(price_data.up_price + self.min_spread_buffer + 0.01, 0.99)
        down_fill = min(price_data.down_price + self.min_spread_buffer + 0.01, 0.99)

        signals = [
            OrderSignal(
                side=TradeSide.BUY,
                token_type=TokenType.YES,
                target_price=up_fill,
                size=shares,
                order_type=OrderType.FOK,
                is_taker=True,
            ),
            OrderSignal(
                side=TradeSide.BUY,
                token_type=TokenType.NO,
                target_price=down_fill,
                size=shares,
                order_type=OrderType.FOK,
                is_taker=True,
            ),
        ]

        self._trades_this_market += 2
        logger.info(
            f"[{self.name}] ARB: {shares:.1f}sh × 2 sides "
            f"UP@{price_data.up_price:.3f}+DOWN@{price_data.down_price:.3f} "
            f"est_cost={combined_cost:.3f} margin={margin:.3f} "
            f"{remaining:.0f}s left"
        )
        return signals

    # ------------------------------------------------------------------
    # Sub-strategy: Directional Last-Moment
    # ------------------------------------------------------------------

    def _estimate_all_in_cost(
        self, mid_price: float, spread: float
    ) -> float:
        """Conservative cost estimate: mid → ask → plus taker fee.

        This is what we'd actually pay in live trading.  The simulation
        fills at mid+fee which is slightly cheaper, so if our EV is
        positive with this conservative estimate it will also be positive
        in live.
        """
        half_spread = max(spread / 2, self.min_spread_buffer)
        ask_estimate = min(mid_price + half_spread, 0.99)
        fee_rate = 0.25 * (ask_estimate * (1 - ask_estimate)) ** 2
        return ask_estimate / (1 - fee_rate)

    def _check_directional(
        self, price_data: PriceData, remaining: float
    ) -> List[OrderSignal]:
        if self.binance_delta is None:
            if not getattr(self, "_no_delta_logged", False):
                logger.debug(f"[{self.name}] No binance_delta available")
                self._no_delta_logged = True
            return []

        delta = self.binance_delta
        vol = self._get_volatility()
        prob = win_probability(delta, remaining, vol)
        predicted_winner = "up" if delta > 0 else "down"
        winner_mid = (
            price_data.up_price
            if predicted_winner == "up"
            else price_data.down_price
        )

        all_in_cost = self._estimate_all_in_cost(
            winner_mid, price_data.spread
        )
        ev_per_share = prob - all_in_cost
        ev_per_dollar = ev_per_share / all_in_cost if all_in_cost > 0 else 0

        now_ts = time.time()
        if now_ts - getattr(self, "_last_eval_log", 0) > 30:
            self._last_eval_log = now_ts
            logger.info(
                f"[{self.name}] EVAL: delta={delta:+.4f} "
                f"prob={prob:.1%} winner={predicted_winner.upper()} "
                f"mid={winner_mid:.3f} cost={all_in_cost:.3f} "
                f"EV/\u0024={ev_per_dollar:.1%} {remaining:.0f}s left"
            )

        if abs(delta) < self.directional_min_delta:
            return []
        if prob < self.min_win_prob:
            return []
        if winner_mid < self.directional_min_price:
            return []
        if winner_mid >= self.directional_max_price:
            return []
        if ev_per_dollar < self.min_ev_per_dollar:
            return []

        headroom = self.max_market_exposure - self._total_exposure()

        # Dynamic sizing: scale inversely with price — risk less at higher
        # prices where the downside-to-upside ratio is worse.
        price_factor = max(0.3, 1.0 - winner_mid)
        confidence_mult = min(
            1.5,
            0.5 + (prob - self.min_win_prob) / (1.0 - self.min_win_prob),
        )
        trade_dollars = min(
            self.trade_size_dollars * price_factor * confidence_mult,
            headroom,
        )
        if trade_dollars < 1.0:
            return []

        shares = min(trade_dollars / all_in_cost, self.max_shares_per_order)
        if shares < self.min_order_shares:
            return []

        fill_price = min(winner_mid + self.min_spread_buffer + 0.01, 0.99)
        token_type = (
            TokenType.YES if predicted_winner == "up" else TokenType.NO
        )

        signal = OrderSignal(
            side=TradeSide.BUY,
            token_type=token_type,
            target_price=fill_price,
            size=shares,
            order_type=OrderType.FOK,
            is_taker=True,
        )

        logger.info(
            f"[{self.name}] DIRECTIONAL BUY {predicted_winner.upper()}: "
            f"delta={delta:+.4f} prob={prob:.1%} "
            f"mid={winner_mid:.3f} cost={all_in_cost:.3f} "
            f"EV/\u0024={ev_per_dollar:.1%} "
            f"${trade_dollars:.1f}→{shares:.1f}sh {remaining:.0f}s left"
        )

        self._trades_this_market += 1
        return [signal]

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def on_market_end(
        self, market_id: str, winner: Optional[str]
    ) -> Tuple[float, float, str]:
        total_cost = self._up_cost + self._down_cost

        if winner is None:
            pnl = -total_cost
            outcome = "unknown"
        elif winner.lower() in ("up", "yes"):
            pnl = self._up_shares * 1.0 - total_cost
            outcome = "up"
        else:
            pnl = self._down_shares * 1.0 - total_cost
            outcome = "down"

        logger.info(
            f"[{self.name}] SETTLED: winner={outcome} "
            f"UP={self._up_shares:.1f}@{self.up_position.avg_price:.3f} "
            f"DOWN={self._down_shares:.1f}@{self.down_position.avg_price:.3f} "
            f"cost=${total_cost:.2f} PnL=${pnl:+.2f} "
            f"trades={self._trades_this_market}"
        )
        return total_cost, pnl, outcome

    # ------------------------------------------------------------------
    # Status / reset
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        remaining = self._time_remaining()
        return {
            "strategy": "endgame",
            "time_remaining": remaining,
            "up_shares": self._up_shares,
            "down_shares": self._down_shares,
            "up_cost": self._up_cost,
            "down_cost": self._down_cost,
            "total_exposure": self._total_exposure(),
            "trades": self._trades_this_market,
            "binance_delta": self.binance_delta,
            "active": remaining <= self.directional_window,
        }

    def reset(self) -> None:
        super().reset()
        self._up_shares = 0.0
        self._up_cost = 0.0
        self._down_shares = 0.0
        self._down_cost = 0.0
        self.market_start_time = None
        self.market_settlement_time = None
        self.binance_delta = None
        self.binance_price = None
        self._trades_this_market = 0
        self._last_trade_time = 0.0
        self._logged_activation = False
        self._logged_direction = None
        self.pending_orders = []
        self._exit_mode = False

    # ------------------------------------------------------------------
    # Runner compatibility stubs
    # ------------------------------------------------------------------

    def record_fill_event(self, side: str, is_cancelled: bool) -> None:
        pass

    def update_spread_info(self, up_spread: float, down_spread: float) -> None:
        pass
