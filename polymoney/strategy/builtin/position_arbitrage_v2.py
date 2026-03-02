"""
Position Arbitrage V2 — Taker-Hybrid Strategy

First-principles redesign:
  In a binary market, buying n UP + n DOWN guarantees $n payout.
  If total cost < $n, profit is locked in.

V1 problem: maker-only orders fail to fill both sides 46% of the time,
  creating directional exposure that wipes out balanced profits.

V2 solution: when one side fills as maker (0% fee), immediately complete
  the hedge using a taker order (~1.56% fee) if profitable, or scratch
  (sell back) the filled side to avoid any directional exposure.

Key invariant: NEVER hold an unhedged position beyond maker_window_seconds.
"""

import time

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    OrderSignal, OrderType, Position, PriceData,
    TokenType, TradeSide,
)
from polymoney.core.strategy import BaseStrategy, register_strategy
from polymoney.strategy.builtin.position_arbitrage import (
    InternalPosition,
    LimitOrder,
)

logger = get_logger("strategy.position_arbitrage_v2")


class _State:
    IDLE = "idle"
    PROBING = "probing"
    FIRST_FILL = "first_fill"
    TAKER_SENT = "taker_sent"
    HEDGED = "hedged"
    SCRATCHED = "scratched"


@register_strategy("position-arbitrage-v2")
class PositionArbitrageV2(BaseStrategy):
    """Taker-Hybrid Position Arbitrage.

    State machine:
        IDLE → PROBING → FIRST_FILL → HEDGED
                                     → TAKER_SENT → HEDGED
                                     → SCRATCHED
    """

    @property
    def strategy_type(self) -> str:
        return "position_arbitrage_v2"

    def __init__(
        self,
        strategy_id: str = "pos-arb-v2",
        name: str = "position-arbitrage-v2",
        position_size: float = 100.0,
        params: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(strategy_id, name, position_size, params)

        p = self.params

        # --- Core pricing ---
        self.target_cost: float = p.get("target_cost", 0.96)
        self.min_maker_discount: float = p.get("min_maker_discount", 0.015)
        self.min_order_shares: float = p.get("min_order_shares", 5)
        self.batch_ratio: float = p.get("batch_ratio", 0.001)
        self.batch_size: float = p.get(
            "batch_size", max(position_size * self.batch_ratio, 0.10)
        )
        self.low_prob_threshold: float = p.get("low_prob_threshold", 0.05)

        # --- Taker-hybrid parameters ---
        self.max_taker_pair_cost: float = p.get("max_taker_pair_cost", 0.995)
        self.maker_window_seconds: float = p.get("maker_window_seconds", 30.0)
        self.max_scratch_loss_pct: float = p.get("max_scratch_loss_pct", 0.05)
        self.taker_slippage_buffer: float = p.get("taker_slippage_buffer", 0.005)

        # --- Adaptive target ---
        self.enable_adaptive_target: bool = p.get("enable_adaptive_target", True)
        self.adaptive_target_min: float = p.get("adaptive_target_min", 0.94)
        self.adaptive_target_max: float = p.get("adaptive_target_max", 0.96)
        self._last_up_spread: float = 0.0
        self._last_down_spread: float = 0.0

        # --- Timing ---
        self.order_timeout: float = p.get("order_timeout", 60)
        self.market_duration: float = p.get("market_duration", 900)

        # --- Internal state ---
        self.up_position = InternalPosition()
        self.down_position = InternalPosition()
        self.pending_orders: List[LimitOrder] = []
        self._exit_mode: bool = False

        self._state: str = _State.IDLE
        self._first_fill_side: Optional[str] = None
        self._first_fill_time: float = 0.0
        self._taker_attempt_time: float = 0.0
        self._scratch_sent: bool = False

        self.current_up_price: Optional[float] = None
        self.current_down_price: Optional[float] = None
        self.market_start_time: Optional[datetime] = None
        self.market_settlement_time: Optional[float] = None

        # Binance feed (set externally by runner if available)
        self.binance_delta: Optional[float] = None
        self.binance_price: Optional[float] = None

    def reset(self):
        self.up_position.reset()
        self.down_position.reset()
        self.pending_orders.clear()
        self._exit_mode = False
        self._state = _State.IDLE
        self._first_fill_side = None
        self._first_fill_time = 0.0
        self._taker_attempt_time = 0.0
        self._scratch_sent = False
        self.current_up_price = None
        self.current_down_price = None

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def on_market_start(self, market_id: str, market_info: Dict[str, Any]) -> None:
        self.reset()
        self.market_start_time = datetime.now()
        self.market_settlement_time = market_info.get("settlement_time")

        api_min = market_info.get("min_order_size")
        if api_min and api_min > 0:
            self.min_order_shares = api_min

        logger.info(f"[{self.name}] Market started: {market_id}")

    def on_market_end(
        self, market_id: str, winner: Optional[str]
    ) -> Tuple[float, float, str]:
        up_sh = self.up_position.shares
        down_sh = self.down_position.shares
        total_cost = self.up_position.cost + self.down_position.cost

        if up_sh == 0 and down_sh == 0:
            return 0.0, 0.0, "empty"

        if winner in ("up", "yes"):
            final_value = up_sh
            outcome = "up"
        elif winner in ("down", "no"):
            final_value = down_sh
            outcome = "down"
        else:
            final_value = (up_sh + down_sh) / 2
            outcome = "unknown"

        pnl = final_value - total_cost

        logger.info(
            f"[{self.name}] Settlement: {outcome} | "
            f"UP={up_sh:.1f} DOWN={down_sh:.1f} | "
            f"Cost=${total_cost:.2f} Val=${final_value:.2f} PnL=${pnl:+.2f} "
            f"state={self._state}"
        )
        return total_cost, pnl, outcome

    def get_status(self) -> Dict[str, Any]:
        up = self.up_position.shares
        down = self.down_position.shares
        total_cost = self.up_position.cost + self.down_position.cost
        hedged = min(up, down)
        ecr = total_cost / hedged if hedged > 0 else float("inf")
        bal = min(up, down) / max(up, down) if max(up, down) > 0 else 0.0

        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "type": self.strategy_type,
            "position_size": self.position_size,
            "params": {
                "target_cost": self.target_cost,
                "max_taker_pair_cost": self.max_taker_pair_cost,
                "maker_window_seconds": self.maker_window_seconds,
                "batch_size": self.batch_size,
            },
            "up_position": {
                "shares": up,
                "cost": self.up_position.cost,
                "avg_price": self.up_position.avg_price,
            },
            "down_position": {
                "shares": down,
                "cost": self.down_position.cost,
                "avg_price": self.down_position.avg_price,
            },
            "effective_cost_rate": min(ecr, 999.99),
            "balance_ratio": bal,
            "hedged_position": hedged,
            "v2_state": self._state,
            "first_fill_side": self._first_fill_side,
        }

    # ------------------------------------------------------------------
    # Runner compatibility methods
    # ------------------------------------------------------------------

    def update_spread_info(self, up_spread: float, down_spread: float) -> None:
        """Called by MarketContext to feed orderbook spread data."""
        if up_spread > 0:
            self._last_up_spread = up_spread
        if down_spread > 0:
            self._last_down_spread = down_spread

    def record_fill_event(self, side: str, is_cancelled: bool) -> None:
        """Called by runner on fill/cancel events. V2 doesn't use EMAs."""
        pass

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
        if price_data.up_price <= 0 or price_data.down_price <= 0:
            return []

        price_sum = price_data.up_price + price_data.down_price
        if price_sum < 0.9 or price_sum > 1.1:
            return []

        self.current_up_price = price_data.up_price
        self.current_down_price = price_data.down_price

        up_sh = self.up_position.shares
        down_sh = self.down_position.shares
        has_up = up_sh >= self.min_order_shares * 0.5
        has_down = down_sh >= self.min_order_shares * 0.5

        if has_up and has_down:
            if self._state not in (_State.HEDGED, _State.SCRATCHED):
                ecr = (self.up_position.cost + self.down_position.cost) / min(up_sh, down_sh)
                bal = min(up_sh, down_sh) / max(up_sh, down_sh)
                logger.info(
                    f"[{self.name}] HEDGED: U={up_sh:.1f}/D={down_sh:.1f} "
                    f"ECR={ecr:.4f} BAL={bal:.2f}"
                )
                self._state = _State.HEDGED
            return []

        if self._state == _State.IDLE:
            return self._enter_market(price_data)

        if self._state == _State.PROBING:
            if has_up or has_down:
                self._state = _State.FIRST_FILL
                self._first_fill_side = "up" if has_up else "down"
                self._first_fill_time = time.time()
                other_side = "down" if has_up else "up"
                self._cancel_pending_side(other_side)
                logger.info(
                    f"[{self.name}] First fill: {self._first_fill_side.upper()} "
                    f"shares={up_sh if has_up else down_sh:.1f} "
                    f"(cancelled {other_side.upper()} maker)"
                )
            else:
                return self._refresh_stale_orders(price_data)

        if self._state == _State.FIRST_FILL:
            return self._handle_first_fill(price_data)

        if self._state == _State.TAKER_SENT:
            elapsed = time.time() - self._taker_attempt_time
            if elapsed > 5.0:
                logger.warning(
                    f"[{self.name}] Taker order not filled after {elapsed:.0f}s, "
                    f"falling back to scratch"
                )
                self._state = _State.FIRST_FILL
                return self._handle_first_fill(price_data)
            return []

        return []

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _enter_market(self, price_data: PriceData) -> List[OrderSignal]:
        """IDLE → PROBING: place maker orders on both sides."""
        up_price = price_data.up_price
        down_price = price_data.down_price

        if up_price < self.low_prob_threshold or down_price < self.low_prob_threshold:
            return []

        effective_target = self._get_adaptive_target()
        price_sum = up_price + down_price

        available_discount = price_sum - effective_target
        min_required = 1.0 - effective_target
        if available_discount < min_required * 0.5:
            return []

        up_limit, down_limit = self._calculate_maker_limits(
            up_price, down_price, effective_target
        )

        order_shares = max(self.min_order_shares, self.batch_size / max(up_limit, 0.01))

        signals = []
        for side, limit_price, mkt_price in [
            ("up", up_limit, up_price),
            ("down", down_limit, down_price),
        ]:
            if limit_price < self.low_prob_threshold:
                continue
            token_type = TokenType.YES if side == "up" else TokenType.NO
            sig = OrderSignal(
                side=TradeSide.BUY,
                token_type=token_type,
                target_price=round(limit_price, 4),
                size=round(order_shares, 1),
                order_type=OrderType.GTC,
                is_taker=False,
            )
            signals.append(sig)
            self._add_pending(side, round(limit_price, 4), round(order_shares, 1), mkt_price)

        if signals:
            self._state = _State.PROBING
            logger.info(
                f"[{self.name}] PROBE: UP@{up_limit:.3f} DOWN@{down_limit:.3f} "
                f"({order_shares:.1f}sh, target={effective_target:.3f})"
            )

        return signals

    def _handle_first_fill(self, price_data: PriceData) -> List[OrderSignal]:
        """FIRST_FILL: assess taker completion or scratch."""
        filled_side = self._first_fill_side
        other_side = "down" if filled_side == "up" else "up"
        filled_pos = self.up_position if filled_side == "up" else self.down_position
        other_price = price_data.down_price if other_side == "down" else price_data.up_price

        maker_avg = filled_pos.avg_price
        taker_fee = self._taker_fee_rate(other_price)
        taker_effective_price = other_price / (1 - taker_fee)
        pair_cost = maker_avg + taker_effective_price

        if pair_cost < self.max_taker_pair_cost:
            self._cancel_pending_side(other_side)
            taker_limit = min(other_price + self.taker_slippage_buffer, 0.99)
            token_type = TokenType.YES if other_side == "up" else TokenType.NO
            signal = OrderSignal(
                side=TradeSide.BUY,
                token_type=token_type,
                target_price=round(taker_limit, 4),
                size=round(filled_pos.shares, 1),
                order_type=OrderType.FOK,
                is_taker=True,
            )
            self._add_pending(other_side, round(taker_limit, 4), round(filled_pos.shares, 1), other_price)
            self._state = _State.TAKER_SENT
            self._taker_attempt_time = time.time()
            logger.info(
                f"[{self.name}] TAKER COMPLETE: {other_side.upper()}@{taker_limit:.3f} "
                f"(market={other_price:.3f}, fee={taker_fee:.4f}, "
                f"pair_cost={pair_cost:.4f})"
            )
            return [signal]

        elapsed = time.time() - self._first_fill_time
        if elapsed > self.maker_window_seconds:
            return self._scratch_position(price_data)

        return self._refresh_maker_on_other_side(other_side, price_data)

    def _scratch_position(self, price_data: PriceData) -> List[OrderSignal]:
        """Sell back the filled side to exit flat."""
        if self._scratch_sent:
            return []

        filled_side = self._first_fill_side
        filled_pos = self.up_position if filled_side == "up" else self.down_position
        market_price = (
            price_data.up_price if filled_side == "up" else price_data.down_price
        )

        sell_discount = 0.005
        sell_price = max(0.01, market_price * (1 - sell_discount))
        token_type = TokenType.YES if filled_side == "up" else TokenType.NO

        signal = OrderSignal(
            side=TradeSide.SELL,
            token_type=token_type,
            target_price=round(sell_price, 4),
            size=round(filled_pos.shares, 1),
        )
        self._add_pending(filled_side, round(sell_price, 4), round(filled_pos.shares, 1), market_price)
        self._scratch_sent = True
        self._state = _State.SCRATCHED

        scratch_loss = filled_pos.shares * (filled_pos.avg_price - sell_price)
        logger.info(
            f"[{self.name}] SCRATCH: sell {filled_side.upper()} "
            f"{filled_pos.shares:.1f}sh @{sell_price:.3f} "
            f"(avg_cost={filled_pos.avg_price:.3f}, loss≈${scratch_loss:.2f})"
        )
        return [signal]

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def _cancel_pending_side(self, side: str) -> None:
        """Remove all pending orders on a given side to prevent double fills."""
        before = len(self.pending_orders)
        self.pending_orders = [
            o for o in self.pending_orders if o.side != side
        ]
        removed = before - len(self.pending_orders)
        if removed > 0:
            logger.info(
                f"[{self.name}] Cancelled {removed} pending {side.upper()} maker(s)"
            )

    def _add_pending(
        self, side: str, price: float, shares: float, market_price: float
    ) -> None:
        """Create a LimitOrder in pending_orders (required by runner/context)."""
        order = LimitOrder(
            order_id=f"{self.name}_{side}_{time.time():.0f}",
            side=side,
            price=price,
            shares=shares,
            cost=shares * price,
            status="pending",
            created_at=datetime.now(),
            created_market_price=market_price,
            timestamp=time.time(),
        )
        self.pending_orders.append(order)

    def _refresh_stale_orders(self, price_data: PriceData) -> List[OrderSignal]:
        """Re-enter if all pending orders were cancelled by the runner."""
        has_active = any(o.status == "pending" for o in self.pending_orders)
        if not has_active:
            self._state = _State.IDLE
            return self._enter_market(price_data)
        return []

    def _refresh_maker_on_other_side(
        self, other_side: str, price_data: PriceData
    ) -> List[OrderSignal]:
        """Keep a fresh maker order on the unfilled side."""
        has_pending = any(
            o.side == other_side and o.status == "pending"
            for o in self.pending_orders
        )
        if has_pending:
            return []

        other_price = (
            price_data.down_price if other_side == "down" else price_data.up_price
        )
        effective_target = self._get_adaptive_target()

        filled_side = self._first_fill_side
        filled_pos = self.up_position if filled_side == "up" else self.down_position
        max_other_limit = effective_target - filled_pos.avg_price
        other_limit = min(
            other_price * (1 - self.min_maker_discount),
            max_other_limit,
        )
        other_limit = max(other_limit, self.low_prob_threshold)

        order_shares = max(self.min_order_shares, filled_pos.shares)

        token_type = TokenType.YES if other_side == "up" else TokenType.NO
        sig = OrderSignal(
            side=TradeSide.BUY,
            token_type=token_type,
            target_price=round(other_limit, 4),
            size=round(order_shares, 1),
            order_type=OrderType.GTC,
            is_taker=False,
        )
        self._add_pending(other_side, round(other_limit, 4), round(order_shares, 1), other_price)
        return [sig]

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    def _calculate_maker_limits(
        self,
        up_price: float,
        down_price: float,
        effective_target: float,
    ) -> Tuple[float, float]:
        price_sum = up_price + down_price
        if price_sum <= effective_target:
            return up_price, down_price

        scale = effective_target / price_sum
        up_limit = up_price * scale
        down_limit = down_price * scale

        up_limit = min(up_limit, up_price * (1 - self.min_maker_discount))
        down_limit = min(down_limit, down_price * (1 - self.min_maker_discount))

        pair = up_limit + down_limit
        if pair > effective_target:
            s = effective_target / pair
            up_limit *= s
            down_limit *= s

        return up_limit, down_limit

    def _get_adaptive_target(self) -> float:
        if not self.enable_adaptive_target:
            return self.target_cost

        spreads = []
        if self._last_up_spread > 0:
            spreads.append(self._last_up_spread)
        if self._last_down_spread > 0:
            spreads.append(self._last_down_spread)

        if not spreads:
            return self.adaptive_target_max

        avg_spread = sum(spreads) / len(spreads)
        spread_low = 0.01
        spread_high = 0.05

        if avg_spread <= spread_low:
            return self.adaptive_target_max
        if avg_spread >= spread_high:
            return self.adaptive_target_min

        ratio = (avg_spread - spread_low) / (spread_high - spread_low)
        return self.adaptive_target_max - ratio * (
            self.adaptive_target_max - self.adaptive_target_min
        )

    @staticmethod
    def _taker_fee_rate(price: float) -> float:
        if price <= 0 or price >= 1:
            return 0.0
        return 0.25 * (price * (1 - price)) ** 2

    # ------------------------------------------------------------------
    # Properties for runner compatibility
    # ------------------------------------------------------------------

    @property
    def effective_cost_rate(self) -> float:
        hedged = min(self.up_position.shares, self.down_position.shares)
        if hedged <= 0:
            return float("inf")
        return (self.up_position.cost + self.down_position.cost) / hedged

    @property
    def balance_ratio(self) -> float:
        up = self.up_position.shares
        down = self.down_position.shares
        if max(up, down) <= 0:
            return 0.0
        return min(up, down) / max(up, down)

    @property
    def hedged_position(self) -> float:
        return min(self.up_position.shares, self.down_position.shares)

    @property
    def realized_ecr(self) -> float:
        return self.effective_cost_rate
