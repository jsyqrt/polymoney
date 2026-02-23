"""
LiveExecutor - Real order execution via Polymarket CLOB API.

Submits and manages real orders on Polymarket using py-clob-client.
Tracks fills by polling order status AND verifying via Trades API.

Requires:
- Configured ClobClient with valid private key
- Market registration with correct token IDs
- FillManager for fill tracking and reconciliation

Fill verification:
  When get_order() reports size_matched > 0, it also returns
  ``associate_trades`` — the definitive list of trade IDs for this order.
  The executor uses these IDs to look up exact execution prices via
  get_trades().  If verification fails (API delay), fills are accepted
  at the limit price rather than discarded — get_order() is authoritative
  and USDC balance changes confirm real execution.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from polymoney.core.logging import get_logger
from polymoney.execution.executor import (
    ExecutionOrder,
    FillEvent,
    MarketExecutionConfig,
    OrderExecutor,
    OrderResult,
    OrderResultStatus,
)

logger = get_logger("execution.live")

# Trade verification timeout: if get_order() reports a fill but
# get_trades() can't confirm it within this window, we ACCEPT the fill
# at the limit price (get_order is the source of truth).
_FILL_VERIFY_TIMEOUT = 30.0  # seconds


@dataclass
class LivePendingOrder:
    """Tracks a live order submitted to the exchange."""
    order_id: str           # Internal order ID
    exchange_order_id: str  # Polymarket CLOB order ID
    market_id: str
    side: str               # "up" or "down" — token selection
    token_id: str           # Actual Polymarket token ID
    price: float
    size: float             # Original size
    filled_size: float = 0.0
    created_at: float = 0.0
    last_checked: float = 0.0
    is_taker: bool = False
    is_sell: bool = False   # True for sell (exit) orders
    # Trade IDs from get_order().associate_trades — the definitive link
    # between this order and its matched trades.
    associate_trades: List[str] = field(default_factory=list)
    # Fill verification: tracks unverified fills detected by get_order()
    # but not yet confirmed by get_trades().
    unverified_fill_size: float = 0.0
    unverified_since: float = 0.0


class LiveExecutor(OrderExecutor):
    """
    Real order execution via Polymarket CLOB.
    
    Uses py-clob-client for:
    - Order creation and signing (EIP-712)
    - Order submission (POST /order)
    - Order cancellation (DELETE /order)
    - Order status polling (GET /order)
    - Trade verification (GET /trades) for accurate fill prices
    
    Token ID resolution:
    - Markets are registered with up_token_id and down_token_id
    - submit_order() resolves (market_id, side) -> token_id
    
    Fill tracking:
    - Polls order status every 2s for active orders
    - Recently submitted orders polled more frequently (500ms for 5s)
    - Verifies fills via get_trades() for actual execution prices
    - Accepts unverified fills at limit price if trades can't be verified
    
    Order lifecycle management:
    - Timeout: cancels orders after order_timeout seconds (default 30s)
    - Stale detection: cancels when market moves > stale_order_threshold
      away from order price (default 20%)
    - Matches SimulatedExecutor behavior for consistency
    """

    def __init__(self, clob_client=None):
        """
        Initialize live executor.
        
        Args:
            clob_client: Initialized py-clob-client ClobClient instance.
                         Can be set later via set_client().
        """
        self._client = clob_client
        self._markets: Dict[str, MarketExecutionConfig] = {}
        self._pending: Dict[str, LivePendingOrder] = {}  # order_id -> order
        self._max_retries = 3
        self._retry_delay = 1.0  # seconds, exponential backoff
        self._maker_address: Optional[str] = None
        # Cache of recently verified trades to avoid re-fetching
        self._verified_trades: Dict[str, float] = {}  # order_id -> verified_size
        # Fills discovered during cancel — emitted on next check_fills()
        self._deferred_fills: List[FillEvent] = []
        # Track trade IDs already claimed by an order to prevent
        # the same trade being attributed to multiple bot orders.
        self._claimed_trade_ids: Dict[str, str] = {}  # trade_id -> order_id

    def set_client(self, clob_client) -> None:
        """Set or update the CLOB client."""
        self._client = clob_client
        self._maker_address = self._extract_maker_address(clob_client)
        if self._maker_address:
            logger.info(
                f"CLOB client configured for live execution "
                f"(maker_address={self._maker_address})"
            )
        else:
            logger.error(
                "CLOB client configured but maker address could NOT be extracted. "
                "Trade verification will be UNAVAILABLE — positions will be inaccurate!"
            )

    @staticmethod
    def _extract_maker_address(client) -> Optional[str]:
        """Extract the maker wallet address from the CLOB client.

        The address used for trade filtering is the *funder* address
        (``client.builder.funder``), which equals the signer address for
        EOA wallets and the proxy wallet address for Magic/browser wallets.
        ``client.get_address()`` returns the signer address directly.
        """
        if client is None:
            return None
        try:
            # Best: builder.funder — the actual on-chain address that holds funds.
            # For EOA wallets this equals the signer address; for proxy wallets
            # it is the separate funder address passed at ClobClient init.
            if hasattr(client, "builder") and client.builder:
                funder = getattr(client.builder, "funder", None)
                if funder:
                    return funder

            # Fallback: get_address() returns signer address (works for EOA).
            if hasattr(client, "get_address"):
                addr = client.get_address()
                if addr:
                    return addr
        except Exception as e:
            logger.warning(f"Could not extract maker address: {e}")
        return None

    @property
    def is_ready(self) -> bool:
        """Check if executor has a configured client."""
        return self._client is not None

    def _get_conditional_balance(self, token_id: str) -> Optional[Tuple[float, float]]:
        """Query on-chain conditional token balance and allowance.

        Returns (balance, allowance) in token units, or None on failure.
        Conditional tokens on Polymarket are ERC-1155 with 6 decimals.
        """
        if not self._client:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            resp = self._client.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            if resp and isinstance(resp, dict):
                raw_bal = resp.get("balance", "0")
                raw_allow = resp.get("allowance", "0")
                balance = float(raw_bal) / 1e6
                allowance = float(raw_allow) / 1e6
                return balance, allowance
        except Exception as e:
            logger.warning(f"Failed to query conditional token balance: {e}")
        return None

    def _update_conditional_allowance(self, token_id: str) -> bool:
        """Refresh token approval so the CLOB can spend conditional tokens."""
        if not self._client:
            return False
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            self._client.update_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            logger.info(f"Updated conditional token allowance for {token_id[:16]}...")
            return True
        except Exception as e:
            logger.warning(f"Failed to update conditional token allowance: {e}")
            return False

    # ------------------------------------------------------------------
    # OrderExecutor interface
    # ------------------------------------------------------------------

    async def submit_order(self, order: ExecutionOrder) -> OrderResult:
        """Submit a real order to Polymarket CLOB."""
        if not self._client:
            return OrderResult(
                status=OrderResultStatus.REJECTED,
                order_id=order.order_id or f"live_{uuid.uuid4().hex[:8]}",
                error="CLOB client not configured",
            )

        market_config = self._markets.get(order.market_id)
        if not market_config:
            return OrderResult(
                status=OrderResultStatus.REJECTED,
                order_id=order.order_id or f"live_{uuid.uuid4().hex[:8]}",
                error=f"Market {order.market_id} not registered",
            )

        # Resolve token ID from (market_id, side)
        token_id = self._resolve_token_id(market_config, order.side)
        if not token_id:
            return OrderResult(
                status=OrderResultStatus.REJECTED,
                order_id=order.order_id or f"live_{uuid.uuid4().hex[:8]}",
                error=f"Cannot resolve token_id for {order.market_id} {order.side}",
            )

        order_id = order.order_id or f"live_{uuid.uuid4().hex[:8]}"

        # Pre-sell check: verify we actually hold the conditional tokens
        is_sell = order.trade_side == "sell"
        actual_size = order.size
        if is_sell:
            bal_result = self._get_conditional_balance(token_id)
            if bal_result is not None:
                balance, allowance = bal_result
                if balance < 0.01:
                    logger.warning(
                        f"SELL rejected: no on-chain balance for token "
                        f"{token_id[:16]}... (balance={balance:.4f})"
                    )
                    return OrderResult(
                        status=OrderResultStatus.REJECTED,
                        order_id=order_id,
                        error="No on-chain conditional token balance",
                    )
                if actual_size > balance:
                    logger.warning(
                        f"SELL size capped: {actual_size:.2f} → {balance:.2f} "
                        f"(on-chain balance) for token {token_id[:16]}..."
                    )
                    actual_size = balance
                if allowance < actual_size:
                    logger.info(
                        f"Refreshing allowance for token {token_id[:16]}... "
                        f"(allowance={allowance:.2f}, need={actual_size:.2f})"
                    )
                    self._update_conditional_allowance(token_id)

        # Polymarket price range: [0.01, 0.99]
        clamped_price = max(0.01, min(0.99, round(order.price, 2)))
        if clamped_price != round(order.price, 2):
            logger.warning(
                f"Price clamped: {order.price:.6f} → {clamped_price} "
                f"(Polymarket range [0.01, 0.99])"
            )

        for attempt in range(self._max_retries):
            try:
                from py_clob_client.clob_types import OrderArgs
                from py_clob_client.order_builder.constants import BUY, SELL

                clob_side = SELL if is_sell else BUY
                order_args = OrderArgs(
                    price=clamped_price,
                    size=actual_size,
                    side=clob_side,
                    token_id=token_id,
                )

                signed_order = self._client.create_order(order_args)
                response = self._client.post_order(signed_order, "GTC")

                if not response or not isinstance(response, dict):
                    raise RuntimeError(f"post_order returned invalid response: {response}")

                if not response.get("success", True):
                    error_msg = response.get("errorMsg", "Unknown error")
                    logger.error(f"Order rejected by exchange: {error_msg}")
                    return OrderResult(
                        status=OrderResultStatus.REJECTED,
                        order_id=order_id,
                        error=error_msg,
                    )

                exchange_order_id = response.get("orderID", "")
                if not exchange_order_id:
                    raise RuntimeError(
                        f"post_order returned no orderID: {response}"
                    )

                pending = LivePendingOrder(
                    order_id=order_id,
                    exchange_order_id=exchange_order_id,
                    market_id=order.market_id,
                    side=order.side,
                    token_id=token_id,
                    price=clamped_price,
                    size=actual_size,
                    created_at=time.time(),
                    last_checked=time.time(),
                    is_taker=order.is_taker,
                    is_sell=is_sell,
                )
                self._pending[order_id] = pending

                trade_label = "SELL" if is_sell else "BUY"
                logger.info(
                    f"Live order placed: {order_id} {trade_label} "
                    f"{order.side.upper()} {actual_size}@{clamped_price} "
                    f"token={token_id[:16]}... exchange_id={exchange_order_id}"
                )

                return OrderResult(
                    status=OrderResultStatus.PENDING,
                    order_id=order_id,
                    exchange_order_id=exchange_order_id,
                )

            except Exception as e:
                err_str = str(e)
                # Don't retry balance/allowance failures — they won't self-resolve
                if "not enough balance" in err_str or "allowance" in err_str:
                    logger.error(
                        f"Order rejected (balance/allowance): {err_str}"
                    )
                    return OrderResult(
                        status=OrderResultStatus.REJECTED,
                        order_id=order_id,
                        error=err_str,
                    )
                delay = self._retry_delay * (2 ** attempt)
                logger.warning(
                    f"Order submission failed (attempt {attempt+1}/{self._max_retries}): {e}"
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Order submission exhausted retries: {e}")
                    return OrderResult(
                        status=OrderResultStatus.REJECTED,
                        order_id=order_id,
                        error=err_str,
                    )

        # Should not reach here
        return OrderResult(
            status=OrderResultStatus.REJECTED,
            order_id=order_id,
            error="Unknown error",
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a live order on the exchange.

        Checks for partial fills before cancelling so no executed
        shares are lost from position tracking.
        """
        if not self._client:
            return False

        pending = self._pending.get(order_id)
        if not pending:
            return False

        try:
            # Check for partial fills before cancelling
            try:
                order_status = self._client.get_order(pending.exchange_order_id)
                if order_status:
                    raw = order_status.get("size_matched") or 0
                    api_filled = float(raw)
                    if api_filled > pending.filled_size + 0.01:
                        new_fill = api_filled - pending.filled_size
                        logger.warning(
                            f"Partial fill detected on cancel: {order_id} "
                            f"{pending.side.upper()} {new_fill:.2f} "
                            f"(total filled={api_filled:.2f})"
                        )
                        self._deferred_fills.append(FillEvent(
                            order_id=order_id,
                            market_id=pending.market_id,
                            side=pending.side,
                            fill_price=pending.price,
                            fill_size=new_fill,
                            is_taker=pending.is_taker,
                            is_partial=True,
                            remaining_size=max(0, pending.size - api_filled),
                            timestamp=time.time(),
                        ))
            except Exception as e:
                logger.warning(f"Could not check fills before cancel: {e}")

            self._client.cancel(pending.exchange_order_id)
            del self._pending[order_id]
            logger.info(f"Live order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def check_fills(
        self, market_id: str, up_price: float, down_price: float
    ) -> List[FillEvent]:
        """
        Poll exchange for fill updates on pending orders.
        
        Order lifecycle management (matching SimulatedExecutor):
        1. Timeout: cancel orders older than order_timeout (default 30s)
        2. Stale detection: cancel when market moves > stale_order_threshold
        3. Fill detection via get_order() + get_trades() verification
        
        Fill detection two-phase:
        1. get_order() detects that size_matched increased (fast, but
           returns the limit price, not the actual fill price).
        2. get_trades() verifies the fill on-chain and returns the real
           execution price (slower, but authoritative).
        
        If get_order() reports a fill but get_trades() finds no matching
        confirmed trades within _FILL_VERIFY_TIMEOUT, the fill is ACCEPTED
        at the limit price — get_order() is authoritative and USDC balance
        changes prove real execution.
        """
        if not self._client:
            return []

        events: List[FillEvent] = []
        now = time.time()
        to_remove: List[str] = []

        # Emit fills discovered during cancel_order
        if self._deferred_fills:
            deferred = [f for f in self._deferred_fills if f.market_id == market_id]
            self._deferred_fills = [
                f for f in self._deferred_fills if f.market_id != market_id
            ]
            events.extend(deferred)

        # Get market config for timeout/stale thresholds
        market_config = self._markets.get(market_id)
        order_timeout = market_config.order_timeout if market_config else 30.0
        stale_threshold = (
            market_config.stale_order_threshold if market_config else 0.20
        )

        for order_id, pending in self._pending.items():
            if pending.market_id != market_id:
                continue

            age = now - pending.created_at
            market_price = up_price if pending.side == "up" else down_price
            has_partial_fill = pending.filled_size >= 0.01

            # --- Timeout check: cancel orders that have been pending too long ---
            # Unfilled orders: cancel after order_timeout.
            # Partially-filled orders: cancel after 2x order_timeout to give
            # the verification loop time to reconcile, but don't let them
            # linger forever.
            effective_timeout = order_timeout if not has_partial_fill else order_timeout * 2
            if age > effective_timeout:
                tag = "partial-timeout" if has_partial_fill else "timeout"
                logger.info(
                    f"Live order {tag}: {order_id} {pending.side.upper()} "
                    f"@{pending.price:.4f} age={age:.0f}s > {effective_timeout:.0f}s "
                    f"filled={pending.filled_size:.2f}/{pending.size:.2f} "
                    f"(market={market_price:.4f})"
                )
                try:
                    self._client.cancel(pending.exchange_order_id)
                except Exception as e:
                    logger.warning(f"Cancel on {tag} failed for {order_id}: {e}")
                events.append(FillEvent(
                    order_id=order_id,
                    market_id=market_id,
                    side=pending.side,
                    fill_price=0.0,
                    fill_size=0.0,
                    is_cancelled=True,
                    cancel_reason=f"{tag}: {age:.0f}s > {effective_timeout:.0f}s",
                    timestamp=now,
                ))
                to_remove.append(order_id)
                continue

            # --- Stale order check: cancel when market moved too far ---
            # Applies to unfilled orders immediately, and to partially-filled
            # orders after a grace period (order_timeout) so the fill
            # verification loop has time to finish.
            stale_eligible = (
                not has_partial_fill or age > order_timeout
            )
            if market_price > 0 and stale_eligible:
                if pending.is_sell:
                    price_diff = (
                        (pending.price - market_price) / market_price
                    )
                else:
                    price_diff = (
                        (market_price - pending.price) / market_price
                    )
                if price_diff > stale_threshold:
                    tag = "partial-stale" if has_partial_fill else "stale"
                    logger.info(
                        f"Live order {tag}: {order_id} {pending.side.upper()} "
                        f"@{pending.price:.4f} market={market_price:.4f} "
                        f"diff={price_diff:.1%} > {stale_threshold:.0%} "
                        f"filled={pending.filled_size:.2f}/{pending.size:.2f}"
                    )
                    try:
                        self._client.cancel(pending.exchange_order_id)
                    except Exception as e:
                        logger.warning(
                            f"Cancel on {tag} failed for {order_id}: {e}"
                        )
                    events.append(FillEvent(
                        order_id=order_id,
                        market_id=market_id,
                        side=pending.side,
                        fill_price=0.0,
                        fill_size=0.0,
                        is_cancelled=True,
                        cancel_reason=(
                            f"{tag}: market={market_price:.4f}, "
                            f"diff={price_diff:.1%}"
                        ),
                        timestamp=now,
                    ))
                    to_remove.append(order_id)
                    continue

            # Adaptive polling interval
            interval = 0.5 if age < 5.0 else 2.0 if age < 60.0 else 10.0
            if now - pending.last_checked < interval:
                continue

            pending.last_checked = now

            try:
                # Phase 1: poll order status
                order_status = self._client.get_order(pending.exchange_order_id)
                if not order_status:
                    continue

                status = order_status.get("status", "")
                raw_matched = order_status.get("size_matched") or 0
                try:
                    api_filled = float(raw_matched)
                except (TypeError, ValueError):
                    logger.error(
                        f"Invalid size_matched value '{raw_matched}' "
                        f"for order {order_id}, treating as 0"
                    )
                    api_filled = 0.0

                # Update associate_trades — the definitive link to matched trades
                assoc = order_status.get("associate_trades")
                if assoc and isinstance(assoc, list):
                    pending.associate_trades = assoc

                if status in ("CANCELLED", "EXPIRED"):
                    # Check if there were any partial fills before cancellation
                    if api_filled > pending.filled_size + 0.01:
                        fill_event = self._verify_and_build_fill(
                            pending, api_filled, now, is_final=True
                        )
                        if fill_event:
                            events.append(fill_event)
                    events.append(FillEvent(
                        order_id=order_id,
                        market_id=market_id,
                        side=pending.side,
                        fill_price=0.0,
                        fill_size=0.0,
                        is_cancelled=True,
                        cancel_reason=f"exchange: {status}",
                        timestamp=now,
                    ))
                    to_remove.append(order_id)
                    continue

                is_full = status == "MATCHED" or api_filled >= pending.size - 0.01
                has_new_fill = api_filled > pending.filled_size + 0.01

                if is_full or has_new_fill:
                    fill_event = self._verify_and_build_fill(
                        pending, api_filled, now, is_final=is_full
                    )
                    if fill_event:
                        events.append(fill_event)
                        if is_full:
                            to_remove.append(order_id)
                    elif is_full:
                        if pending.unverified_since == 0:
                            pending.unverified_fill_size = api_filled
                            pending.unverified_since = now
                            logger.warning(
                                f"Fill unverified: {order_id} "
                                f"{pending.side.upper()} {api_filled:.1f} — "
                                f"awaiting trade confirmation"
                            )
                        elif now - pending.unverified_since > _FILL_VERIFY_TIMEOUT:
                            fill_event = self._accept_unverified_fill(
                                pending, api_filled, now, is_final=True,
                            )
                            events.append(fill_event)
                            to_remove.append(order_id)

                # Accept fills stuck in unverified state after timeout
                elif (pending.unverified_since > 0
                      and now - pending.unverified_since > _FILL_VERIFY_TIMEOUT):
                    latest_fill = max(api_filled, pending.unverified_fill_size)
                    fill_event = self._accept_unverified_fill(
                        pending, latest_fill, now,
                        is_final=True,
                    )
                    events.append(fill_event)
                    to_remove.append(order_id)

            except Exception as e:
                logger.error(f"Failed to check order {order_id}: {e}")

        for order_id in to_remove:
            self._pending.pop(order_id, None)
            self._verified_trades.pop(order_id, None)

        return events

    def _verify_and_build_fill(
        self,
        pending: LivePendingOrder,
        api_filled: float,
        now: float,
        is_final: bool,
    ) -> Optional[FillEvent]:
        """
        Verify a fill via the Trades API and build a FillEvent with
        the actual execution price.
        
        Returns None if trades are not yet confirmed (will be retried).
        """
        verified_size, vwap = self._fetch_verified_trades(pending)

        if verified_size < 0.01:
            return None

        # Hard cap: verified_size must never exceed ordered amount
        if verified_size > pending.size:
            logger.warning(
                f"Hard cap: verified_size {verified_size:.2f} > "
                f"order size {pending.size:.2f} for {pending.order_id}"
            )
            verified_size = pending.size

        new_fill = verified_size - pending.filled_size
        if new_fill < 0.01:
            return None

        pending.unverified_since = 0
        pending.unverified_fill_size = 0

        is_partial = not is_final or verified_size < pending.size - 0.01
        remaining = max(0, pending.size - verified_size)

        pending.filled_size = verified_size
        self._verified_trades[pending.order_id] = verified_size

        if pending.is_sell:
            improvement = (vwap - pending.price) / pending.price if pending.price > 0 else 0
        else:
            improvement = (pending.price - vwap) / pending.price if pending.price > 0 else 0
        logger.info(
            f"Live fill (verified): {pending.order_id} "
            f"{'SELL' if pending.is_sell else 'BUY'} "
            f"{pending.side.upper()} {new_fill:.2f}@{vwap:.4f} "
            f"(limit={pending.price:.4f}, improvement={improvement:.1%})"
        )

        return FillEvent(
            order_id=pending.order_id,
            market_id=pending.market_id,
            side=pending.side,
            fill_price=vwap,
            fill_size=new_fill,
            is_taker=pending.is_taker,
            is_partial=is_partial,
            remaining_size=remaining,
            timestamp=now,
            is_sell=pending.is_sell,
        )

    def _accept_unverified_fill(
        self,
        pending: LivePendingOrder,
        api_filled: float,
        now: float,
        is_final: bool,
    ) -> FillEvent:
        """Accept a fill that get_order() confirms but get_trades() cannot match.

        This should rarely happen — it means get_order() returned
        associate_trades but none of them matched in get_trades(), or
        associate_trades was empty.  get_order() is authoritative for
        fill status (USDC balance confirms execution), so we accept
        using the limit price.  The periodic reconciliation loop will
        correct share counts if needed.
        """
        # Cap api_filled at the ordered amount
        api_filled = min(api_filled, pending.size)
        new_fill = max(0, api_filled - pending.filled_size)
        if new_fill < 0.01:
            # Rounding noise — nothing new to report
            if pending.filled_size > 0:
                return FillEvent(
                    order_id=pending.order_id,
                    market_id=pending.market_id,
                    side=pending.side,
                    fill_price=0.0,
                    fill_size=0.0,
                    is_cancelled=True,
                    cancel_reason="fill already counted, rounding noise",
                    timestamp=now,
                )
            new_fill = api_filled

        pending.filled_size = api_filled
        pending.unverified_since = 0
        pending.unverified_fill_size = 0

        logger.warning(
            f"FILL ACCEPTED AT LIMIT PRICE (API propagation delay): "
            f"{pending.order_id} {pending.side.upper()} "
            f"{new_fill:.2f}@{pending.price:.4f} | "
            f"exchange_id={pending.exchange_order_id}, "
            f"associate_trades={pending.associate_trades} | "
            f"maker_address={self._maker_address}, "
            f"asset_id={pending.token_id[:20]}... | "
            f"get_trades() had no matches after {_FILL_VERIFY_TIMEOUT:.0f}s"
        )

        return FillEvent(
            order_id=pending.order_id,
            market_id=pending.market_id,
            side=pending.side,
            fill_price=pending.price,
            fill_size=new_fill,
            is_taker=pending.is_taker,
            is_partial=not is_final,
            remaining_size=max(0, pending.size - api_filled),
            timestamp=now,
            is_sell=pending.is_sell,
        )

    def _fetch_verified_trades(
        self, pending: LivePendingOrder
    ) -> Tuple[float, float]:
        """Fetch confirmed trades for a pending order from the Trades API.

        Uses ``associate_trades`` from ``get_order()`` — the definitive
        list of trade IDs linked to this order by the exchange itself.

        Strategy:
        1. Query ``/trades`` with ``maker_address`` + ``asset_id``.
        2. Match trades by ``associate_trades`` IDs, ``taker_order_id``,
           or ``maker_orders[].order_id``.
        3. Skip trades already claimed by another order (prevents double-counting
           when multiple bot orders match against the same counterparty trade).
        4. Cap total verified size at ``pending.size`` — the Trades API ``size``
           field can represent the counterparty's full trade, not our portion.
        5. Claim matched trade IDs so subsequent orders cannot re-use them.

        Returns (total_size, vwap) or (0.0, 0.0) if nothing found yet.
        """
        if not self._maker_address:
            return 0.0, 0.0

        try:
            from py_clob_client.clob_types import TradeParams

            assoc_ids = set(pending.associate_trades) if pending.associate_trades else set()
            exchange_oid = pending.exchange_order_id
            order_id = pending.order_id
            max_size = pending.size

            _ACCEPTED_STATUSES = {
                "CONFIRMED", "MINED", "MATCHED",
                "TRADE_STATUS_CONFIRMED", "TRADE_STATUS_MINED", "TRADE_STATUS_MATCHED",
            }

            def _correct_trade_price(
                api_price: float, limit_price: float, is_sell: bool,
            ) -> float:
                """Correct for Polymarket neg_risk complement pricing.

                In neg_risk binary markets, complementary order matching can
                cause the trade API's ``price`` field to return the complement
                (1 - actual_cost) instead of the true fill price.

                Detection: for BUY orders the fill price must be <= limit;
                for SELL orders >= limit.  When the raw API price violates
                this constraint but ``1 - api_price`` satisfies it, the trade
                was complement-matched and we use the corrected price.

                If both interpretations satisfy the constraint, we pick the
                one closest to the limit (most likely correct).
                """
                complement = 1.0 - api_price
                tolerance = 0.02  # 2 cents tolerance for rounding

                if is_sell:
                    api_ok = api_price >= limit_price - tolerance
                    comp_ok = complement >= limit_price - tolerance
                else:
                    api_ok = api_price <= limit_price + tolerance
                    comp_ok = complement <= limit_price + tolerance

                if api_ok and not comp_ok:
                    return api_price
                if comp_ok and not api_ok:
                    return complement
                # Both satisfy or neither — pick closer to limit
                if abs(api_price - limit_price) <= abs(complement - limit_price):
                    return api_price
                return complement

            def _match_trades(trades: list) -> Tuple[float, float, List[str]]:
                """Match our trades, skipping already-claimed ones and capping at order size."""
                total_size = 0.0
                total_value = 0.0
                matched: List[str] = []
                for trade in trades:
                    if trade.get("status", "") not in _ACCEPTED_STATUSES:
                        continue
                    trade_id = trade.get("id", "")

                    # Skip trades already claimed by a different order
                    claimed_by = self._claimed_trade_ids.get(trade_id)
                    if claimed_by is not None and claimed_by != order_id:
                        continue

                    is_ours = trade_id in assoc_ids if assoc_ids else False

                    if not is_ours:
                        taker_oid = trade.get("taker_order_id", "")
                        if taker_oid and taker_oid == exchange_oid:
                            is_ours = True

                    if not is_ours:
                        for mo in trade.get("maker_orders", []):
                            if mo.get("order_id", "") == exchange_oid:
                                is_ours = True
                                break

                    if not is_ours:
                        continue

                    try:
                        size = float(trade.get("size", 0))
                        raw_price = float(trade.get("price", 0))
                    except (ValueError, TypeError):
                        continue
                    if size <= 0 or raw_price <= 0:
                        continue

                    price = _correct_trade_price(
                        raw_price, pending.price, pending.is_sell,
                    )
                    if abs(price - raw_price) > 0.005:
                        logger.debug(
                            f"Complement price correction for {order_id}: "
                            f"API={raw_price:.4f} → {price:.4f} "
                            f"(limit={pending.price:.4f}, "
                            f"sell={pending.is_sell})"
                        )

                    # Cap contribution so total never exceeds ordered amount
                    remaining = max_size - total_size
                    if remaining <= 0.001:
                        break
                    capped = min(size, remaining)

                    total_size += capped
                    total_value += capped * price
                    matched.append(trade_id)
                return total_size, total_value, matched

            def _finalize(sz: float, val: float, ids: List[str], tag: str) -> Tuple[float, float]:
                """Cap, claim, log and return."""
                if sz > max_size:
                    logger.warning(
                        f"Capping verified size {sz:.2f} -> {max_size:.2f} "
                        f"for {order_id} (ordered {max_size:.2f})"
                    )
                    vwap = val / sz
                    sz = max_size
                    val = sz * vwap
                else:
                    vwap = val / sz if sz > 0 else 0.0

                for tid in ids:
                    self._claimed_trade_ids[tid] = order_id

                logger.info(
                    f"Verified trades{tag} for {order_id}: "
                    f"size={sz:.2f}, vwap={vwap:.4f}, trades={ids}"
                )
                return sz, vwap

            # --- Attempt 1: bulk query with maker_address + asset_id ---
            params = TradeParams(
                maker_address=self._maker_address,
                asset_id=pending.token_id,
            )
            trades = self._client.get_trades(params=params)

            if trades and isinstance(trades, list):
                sz, val, ids = _match_trades(trades)
                if sz > 0:
                    return _finalize(sz, val, ids, "")
                trade_ids_in_response = [
                    t.get("id", "?") for t in trades[:5]
                ]
                trade_statuses = [
                    t.get("status", "?") for t in trades[:5]
                ]
                logger.debug(
                    f"get_trades returned {len(trades)} trades but no match "
                    f"for {order_id} "
                    f"(exchange_id={exchange_oid}, assoc={assoc_ids}, "
                    f"returned_ids={trade_ids_in_response}, "
                    f"returned_statuses={trade_statuses})"
                )
            else:
                logger.warning(
                    f"get_trades returned empty for {order_id} "
                    f"(maker_address={self._maker_address}, "
                    f"asset_id={pending.token_id[:20]}...)"
                )

            # --- Attempt 2: fetch each associate_trade by ID ---
            if assoc_ids:
                all_fetched: list = []
                for tid in assoc_ids:
                    try:
                        id_params = TradeParams(
                            id=tid,
                            maker_address=self._maker_address,
                        )
                        id_trades = self._client.get_trades(params=id_params)
                        if id_trades and isinstance(id_trades, list):
                            all_fetched.extend(id_trades)
                    except Exception as e:
                        logger.debug(f"Fetch trade {tid} failed: {e}")
                if all_fetched:
                    sz, val, ids = _match_trades(all_fetched)
                    if sz > 0:
                        return _finalize(sz, val, ids, " (by ID)")

            logger.debug(
                f"No verified trades for {order_id} "
                f"(exchange_id={exchange_oid}, assoc={assoc_ids})"
            )
            return 0.0, 0.0

        except ImportError:
            logger.error("py_clob_client.clob_types not available — cannot verify trades")
            return 0.0, 0.0
        except Exception as e:
            logger.warning(
                f"Trade verification failed for {pending.order_id}: {e}"
            )
            return 0.0, 0.0

    async def cancel_all(self, market_id: Optional[str] = None) -> int:
        """Cancel all pending live orders."""
        count = 0
        to_cancel = [
            oid
            for oid, p in self._pending.items()
            if market_id is None or p.market_id == market_id
        ]
        for order_id in to_cancel:
            if await self.cancel_order(order_id):
                count += 1
        return count

    def register_market(self, config: MarketExecutionConfig) -> None:
        """Register a market with its token IDs."""
        self._markets[config.market_id] = config
        logger.info(
            f"Market registered for live trading: {config.market_id} "
            f"(up={config.up_token_id[:16]}..., down={config.down_token_id[:16]}...)"
        )

    def unregister_market(self, market_id: str) -> None:
        """Unregister a market and cancel its orders.

        Checks each pending order for partial fills before removing,
        emitting deferred FillEvents for any unreported fills.
        """
        self._markets.pop(market_id, None)
        to_remove = [
            oid for oid, p in self._pending.items()
            if p.market_id == market_id
        ]
        for oid in to_remove:
            pending = self._pending.get(oid)
            if pending and self._client:
                try:
                    order_status = self._client.get_order(pending.exchange_order_id)
                    if order_status:
                        raw = order_status.get("size_matched") or 0
                        api_filled = float(raw)
                        if api_filled > pending.filled_size + 0.01:
                            new_fill = api_filled - pending.filled_size
                            logger.warning(
                                f"Partial fill on unregister: {oid} "
                                f"{pending.side.upper()} {new_fill:.2f}"
                            )
                            self._deferred_fills.append(FillEvent(
                                order_id=oid,
                                market_id=pending.market_id,
                                side=pending.side,
                                fill_price=pending.price,
                                fill_size=new_fill,
                                is_taker=pending.is_taker,
                                is_partial=True,
                                remaining_size=max(0, pending.size - api_filled),
                                timestamp=time.time(),
                            ))
                except Exception as e:
                    logger.warning(f"Could not check fills on unregister: {e}")
            self._pending.pop(oid, None)

        # Clean up claimed trade IDs for this market's orders
        stale = [tid for tid, oid in self._claimed_trade_ids.items() if oid in to_remove]
        for tid in stale:
            del self._claimed_trade_ids[tid]

    def get_pending_count(self, market_id: Optional[str] = None) -> int:
        """Get number of pending orders."""
        if market_id:
            return sum(
                1 for p in self._pending.values() if p.market_id == market_id
            )
        return len(self._pending)

    def get_pending_order_ids(self, market_id: Optional[str] = None) -> set:
        """Return the set of order IDs currently tracked as pending."""
        if market_id:
            return {
                oid for oid, p in self._pending.items()
                if p.market_id == market_id
            }
        return set(self._pending.keys())

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_token_id(
        self, config: MarketExecutionConfig, side: str
    ) -> Optional[str]:
        """Resolve (market, side) to actual Polymarket token ID."""
        if side in ("up", "yes"):
            return config.up_token_id
        elif side in ("down", "no"):
            return config.down_token_id
        return None
