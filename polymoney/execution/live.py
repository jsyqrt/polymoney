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
    side: str
    token_id: str           # Actual Polymarket token ID
    price: float
    size: float             # Original size
    filled_size: float = 0.0
    created_at: float = 0.0
    last_checked: float = 0.0
    is_taker: bool = False
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

        for attempt in range(self._max_retries):
            try:
                from py_clob_client.clob_types import OrderArgs
                from py_clob_client.order_builder.constants import BUY

                order_args = OrderArgs(
                    price=order.price,
                    size=order.size,
                    side=BUY,  # Strategy always buys tokens
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

                # Track pending order
                pending = LivePendingOrder(
                    order_id=order_id,
                    exchange_order_id=exchange_order_id,
                    market_id=order.market_id,
                    side=order.side,
                    token_id=token_id,
                    price=order.price,
                    size=order.size,
                    created_at=time.time(),
                    last_checked=time.time(),
                    is_taker=order.is_taker,
                )
                self._pending[order_id] = pending

                logger.info(
                    f"Live order placed: {order_id} {order.side.upper()} "
                    f"{order.size}@{order.price} token={token_id[:16]}... "
                    f"exchange_id={exchange_order_id}"
                )

                return OrderResult(
                    status=OrderResultStatus.PENDING,
                    order_id=order_id,
                    exchange_order_id=exchange_order_id,
                )

            except Exception as e:
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
                        error=str(e),
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
        
        Two-phase fill detection:
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

        for order_id, pending in self._pending.items():
            if pending.market_id != market_id:
                continue

            # Adaptive polling interval
            age = now - pending.created_at
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
                            # get_order() confirms the fill but get_trades()
                            # cannot verify it.  ACCEPT at limit price —
                            # get_order() is the source of truth and USDC
                            # balance changes prove the execution is real.
                            fill_event = self._accept_unverified_fill(
                                pending, api_filled, now, is_final=True,
                            )
                            events.append(fill_event)
                            to_remove.append(order_id)

                # Accept fills stuck in unverified state after timeout
                # Use current api_filled (not stale unverified_fill_size)
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
            # No confirmed trades yet
            return None

        new_fill = verified_size - pending.filled_size
        if new_fill < 0.01:
            return None

        # Reset unverified state on successful verification
        pending.unverified_since = 0
        pending.unverified_fill_size = 0

        is_partial = not is_final or verified_size < pending.size - 0.01
        remaining = max(0, pending.size - verified_size)

        pending.filled_size = verified_size
        self._verified_trades[pending.order_id] = verified_size

        logger.info(
            f"Live fill (verified): {pending.order_id} "
            f"{pending.side.upper()} {new_fill:.2f}@{vwap:.4f} "
            f"(limit={pending.price:.4f}, improvement="
            f"{(pending.price - vwap) / pending.price:.1%})"
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
        )

    def _fetch_verified_trades(
        self, pending: LivePendingOrder
    ) -> Tuple[float, float]:
        """Fetch confirmed trades for a pending order from the Trades API.

        Uses ``associate_trades`` from ``get_order()`` — the definitive
        list of trade IDs linked to this order by the exchange itself.

        Strategy:
        1. Query ``/trades`` with ``maker_address`` + ``asset_id``
           (maker_address is required by the API and returns ALL trades
           for that address, regardless of maker/taker role).
        2. Match trades by ``associate_trades`` IDs, ``taker_order_id``,
           or ``maker_orders[].order_id``.
        3. Fallback: if bulk query finds nothing but ``associate_trades``
           exist, fetch each trade individually by ID.

        Returns (total_size, vwap) or (0.0, 0.0) if nothing found yet.
        """
        if not self._maker_address:
            return 0.0, 0.0

        try:
            from py_clob_client.clob_types import TradeParams

            assoc_ids = set(pending.associate_trades) if pending.associate_trades else set()
            exchange_oid = pending.exchange_order_id

            _ACCEPTED_STATUSES = {
                "CONFIRMED", "MINED", "MATCHED",
                "TRADE_STATUS_CONFIRMED", "TRADE_STATUS_MINED", "TRADE_STATUS_MATCHED",
            }

            def _match_trades(trades: list) -> Tuple[float, float, List[str]]:
                """Match our trades from a list using multiple identifiers."""
                total_size = 0.0
                total_value = 0.0
                matched: List[str] = []
                for trade in trades:
                    if trade.get("status", "") not in _ACCEPTED_STATUSES:
                        continue
                    trade_id = trade.get("id", "")

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
                        price = float(trade.get("price", 0))
                    except (ValueError, TypeError):
                        continue
                    if size > 0 and price > 0:
                        total_size += size
                        total_value += size * price
                        matched.append(trade_id)
                return total_size, total_value, matched

            # --- Attempt 1: bulk query with maker_address + asset_id ---
            params = TradeParams(
                maker_address=self._maker_address,
                asset_id=pending.token_id,
            )
            trades = self._client.get_trades(params=params)

            if trades and isinstance(trades, list):
                sz, val, ids = _match_trades(trades)
                if sz > 0:
                    vwap = val / sz
                    logger.info(
                        f"Verified trades for {pending.order_id}: "
                        f"size={sz:.2f}, vwap={vwap:.4f}, trades={ids}"
                    )
                    return sz, vwap
                trade_ids_in_response = [
                    t.get("id", "?") for t in trades[:5]
                ]
                trade_statuses = [
                    t.get("status", "?") for t in trades[:5]
                ]
                logger.debug(
                    f"get_trades returned {len(trades)} trades but no match "
                    f"for {pending.order_id} "
                    f"(exchange_id={exchange_oid}, assoc={assoc_ids}, "
                    f"returned_ids={trade_ids_in_response}, "
                    f"returned_statuses={trade_statuses})"
                )
            else:
                logger.warning(
                    f"get_trades returned empty for {pending.order_id} "
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
                        vwap = val / sz
                        logger.info(
                            f"Verified trades (by ID) for {pending.order_id}: "
                            f"size={sz:.2f}, vwap={vwap:.4f}, trades={ids}"
                        )
                        return sz, vwap

            logger.debug(
                f"No verified trades for {pending.order_id} "
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

    def get_pending_count(self, market_id: Optional[str] = None) -> int:
        """Get number of pending orders."""
        if market_id:
            return sum(
                1 for p in self._pending.values() if p.market_id == market_id
            )
        return len(self._pending)

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
