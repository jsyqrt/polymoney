"""
PositionRedeemer - Automatic redemption of settled Polymarket positions.

After a prediction market resolves, winning conditional tokens must be
explicitly redeemed to recover USDC collateral. This module automates
that process so funds can be recycled into new markets.

Provides two redeemer implementations:

  PositionRedeemer (live mode):
    Two redemption backends via polymarket-apis:
    1. Gasless (PolymarketGaslessWeb3Client) - no gas fees, uses Builder Relayer
       Supports: Magic (sig_type=1) and Safe (sig_type=2) wallets
    2. Gas (PolymarketWeb3Client) - pays POL gas, direct on-chain tx
       Supports: all wallet types including EOA (sig_type=0)

  PaperRedeemer (paper mode):
    Simulates redemption with a configurable delay to model the real-world
    latency between settlement detection and USDC availability.

Detection of redeemable positions uses the Polymarket Data API directly
(GET https://data-api.polymarket.com/positions?redeemable=true) so that
the core detection path has zero extra package dependencies.

Usage in TradingRunner:
    redeemer = PositionRedeemer(private_key, funder, signature_type)
    await redeemer.start_background_scan(interval=300)  # every 5 min
    ...
    results = await redeemer.redeem_market(condition_id)  # on settlement
    if not all(r.success for r in results):
        # handle failure - stop trading
    ...
    await redeemer.stop()
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from polymoney.core.logging import get_logger

logger = get_logger("execution.redeemer")

DATA_API_BASE = "https://data-api.polymarket.com"


@dataclass
class RedeemablePosition:
    """A position that can be redeemed for USDC."""

    condition_id: str
    asset: str  # token ID
    outcome: str  # "Yes" / "No"
    outcome_index: int  # 0 or 1
    size: float  # number of tokens
    current_value: float  # estimated USDC value
    title: str
    slug: str
    negative_risk: bool = False


@dataclass
class RedemptionResult:
    """Result of a single redemption attempt."""

    condition_id: str
    success: bool
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    value_redeemed: float = 0.0


class PositionRedeemer:
    """
    Automatic position redemption for settled Polymarket markets.

    Detects redeemable positions via the Data API, then redeems them
    using polymarket-apis (gasless when possible, falls back to gas).
    """

    def __init__(
        self,
        private_key: str,
        funder: Optional[str] = None,
        signature_type: int = 0,
        scan_interval: float = 300.0,
    ):
        """
        Args:
            private_key: Wallet private key (hex string with or without 0x prefix)
            funder: Proxy/funder wallet address (required for Magic/Safe wallets)
            signature_type: 0=EOA, 1=Magic, 2=Safe/Gnosis
            scan_interval: Seconds between background scans for redeemable positions
        """
        self._private_key = private_key
        self._funder = funder
        self._signature_type = signature_type
        self._scan_interval = scan_interval

        self._proxy_address: Optional[str] = funder
        self._eoa_address: Optional[str] = None

        # Web3 clients (lazy-initialized)
        self._web3_client = None
        self._gasless_client = None
        self._clients_initialized = False

        # Background scan state
        self._scan_task: Optional[asyncio.Task] = None
        self._running = False

        # Track recently redeemed conditions to avoid double-redemption
        self._recently_redeemed: Dict[str, float] = {}
        self._redeem_cooldown = 600.0  # 10 min cooldown

        # Stats
        self.total_redeemed: int = 0
        self.total_value_redeemed: float = 0.0
        self.total_failures: int = 0

    # ------------------------------------------------------------------
    # Client initialization
    # ------------------------------------------------------------------

    def _init_clients(self) -> bool:
        """
        Lazy-initialize polymarket-apis clients.

        Returns True if at least one client was initialized.
        """
        if self._clients_initialized:
            return self._web3_client is not None or self._gasless_client is not None

        self._clients_initialized = True

        try:
            from polymarket_apis import PolymarketWeb3Client

            self._web3_client = PolymarketWeb3Client(
                private_key=self._private_key,
                signature_type=self._signature_type,
            )
            self._eoa_address = self._web3_client.address
            if not self._proxy_address:
                self._proxy_address = self._eoa_address

            logger.info(
                f"Web3 redeemer initialized: EOA={self._eoa_address}, "
                f"proxy={self._proxy_address}, sig_type={self._signature_type}"
            )
        except ImportError:
            logger.warning(
                "polymarket-apis not installed. "
                "Install with: pip install polymarket-apis"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to initialize Web3 redeemer: {e}")
            return False

        # Try gasless client for Magic/Safe wallets (no gas fees)
        if self._signature_type in (1, 2):
            try:
                from polymarket_apis import PolymarketGaslessWeb3Client

                self._gasless_client = PolymarketGaslessWeb3Client(
                    private_key=self._private_key,
                    signature_type=self._signature_type,
                )
                logger.info("Gasless redeemer initialized (no gas fees)")
            except Exception as e:
                logger.warning(
                    f"Gasless redeemer unavailable, falling back to gas mode: {e}"
                )

        return True

    # ------------------------------------------------------------------
    # Detection: find redeemable positions via Data API
    # ------------------------------------------------------------------

    async def get_redeemable_positions(
        self, condition_id: Optional[str] = None
    ) -> List[RedeemablePosition]:
        """
        Query the Polymarket Data API for redeemable positions.

        Args:
            condition_id: If specified, filter to this market only.

        Returns:
            List of redeemable positions.
        """
        if not self._proxy_address:
            if not self._init_clients():
                logger.error("Cannot detect redeemable positions: no wallet address")
                return []

        params: Dict[str, Any] = {
            "user": self._proxy_address,
            "redeemable": "true",
            "sizeThreshold": 0,
            "limit": 500,
        }
        if condition_id:
            params["market"] = condition_id

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{DATA_API_BASE}/positions", params=params
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Data API request failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error querying redeemable positions: {e}")
            return []

        positions = []
        for item in data:
            try:
                pos = RedeemablePosition(
                    condition_id=item.get("conditionId", ""),
                    asset=item.get("asset", ""),
                    outcome=item.get("outcome", ""),
                    outcome_index=int(item.get("outcomeIndex", 0)),
                    size=float(item.get("size", 0)),
                    current_value=float(item.get("currentValue", 0)),
                    title=item.get("title", ""),
                    slug=item.get("slug", ""),
                    negative_risk=bool(item.get("negativeRisk", False)),
                )
                if pos.size > 0:
                    positions.append(pos)
            except (ValueError, TypeError) as e:
                logger.warning(f"Skipping malformed position: {e}")

        return positions

    # ------------------------------------------------------------------
    # Redemption: convert conditional tokens -> USDC
    # ------------------------------------------------------------------

    async def redeem_position(
        self, position: RedeemablePosition
    ) -> RedemptionResult:
        """
        Redeem a single position.

        Tries gasless first (if available), falls back to gas-based.
        """
        cid = position.condition_id

        # Check cooldown (only successful redemptions trigger cooldown)
        last_success = self._recently_redeemed.get(cid, 0)
        if time.time() - last_success < self._redeem_cooldown:
            return RedemptionResult(
                condition_id=cid,
                success=False,
                error="Recently redeemed, in cooldown",
            )

        amounts = [0, 0]
        amounts[position.outcome_index] = position.size

        return await self._execute_redeem(
            condition_id=cid,
            amounts=amounts,
            neg_risk=position.negative_risk,
            label=f"{position.title} ({position.outcome}, {position.size:.2f} shares)",
            value=position.current_value,
        )

    async def redeem_condition(
        self,
        condition_id: str,
        amounts: List[float],
        neg_risk: bool = False,
        label: str = "",
        value: float = 0.0,
    ) -> RedemptionResult:
        """
        Redeem both outcomes of a condition in a single call.

        This is more efficient than calling redeem_position twice because
        the CTF contract processes both outcomes atomically.

        Args:
            condition_id: Market condition ID.
            amounts: [yes_shares, no_shares] to redeem.
            neg_risk: Whether this is a negative-risk market.
            label: Human-readable label for logging.
            value: Expected USDC value to recover.
        """
        last_success = self._recently_redeemed.get(condition_id, 0)
        if time.time() - last_success < self._redeem_cooldown:
            return RedemptionResult(
                condition_id=condition_id,
                success=False,
                error="Recently redeemed, in cooldown",
            )

        return await self._execute_redeem(
            condition_id=condition_id,
            amounts=amounts,
            neg_risk=neg_risk,
            label=label or condition_id[:16] + "...",
            value=value,
        )

    async def _execute_redeem(
        self,
        condition_id: str,
        amounts: List[float],
        neg_risk: bool,
        label: str,
        value: float,
    ) -> RedemptionResult:
        """Core redemption logic with gasless-first strategy and 429 retry."""
        if not self._init_clients():
            return RedemptionResult(
                condition_id=condition_id,
                success=False,
                error="polymarket-apis not available",
            )

        # Try gasless first (with 429 retry)
        if self._gasless_client is not None:
            result = await self._redeem_gasless(
                condition_id, amounts, neg_risk, label, value
            )
            if result.success:
                self._recently_redeemed[condition_id] = time.time()
                return result
            logger.warning(
                f"Gasless redeem failed for {condition_id[:16]}...: "
                f"{result.error}. Falling back to gas-based redeem."
            )

        # Fall back to gas-based
        if self._web3_client is not None:
            result = await self._redeem_with_gas(
                condition_id, amounts, neg_risk, label, value
            )
            if result.success:
                self._recently_redeemed[condition_id] = time.time()
            return result

        return RedemptionResult(
            condition_id=condition_id,
            success=False,
            error="No redemption client available",
        )

    async def _redeem_gasless(
        self,
        condition_id: str,
        amounts: List[float],
        neg_risk: bool,
        label: str,
        value: float,
    ) -> RedemptionResult:
        """Redeem via the gasless relayer with retry on 429 rate limiting."""
        max_retries = 4
        for attempt in range(max_retries):
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._gasless_client.redeem_position(
                        condition_id=condition_id,
                        amounts=amounts,
                        neg_risk=neg_risk,
                    ),
                )
                tx_hash = str(result) if result else None
                logger.info(f"Gasless redeem OK: {label} tx={tx_hash}")
                self.total_redeemed += 1
                self.total_value_redeemed += value
                return RedemptionResult(
                    condition_id=condition_id,
                    success=True,
                    tx_hash=tx_hash,
                    value_redeemed=value,
                )
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and attempt < max_retries - 1:
                    wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                    logger.info(
                        f"Gasless relayer rate-limited (429), "
                        f"retry in {wait}s ({attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait)
                    continue
                return RedemptionResult(
                    condition_id=condition_id,
                    success=False,
                    error=f"Gasless redeem error: {e}",
                )
        return RedemptionResult(
            condition_id=condition_id,
            success=False,
            error="Gasless redeem: max retries exhausted (429)",
        )

    async def _redeem_with_gas(
        self,
        condition_id: str,
        amounts: List[float],
        neg_risk: bool,
        label: str,
        value: float,
    ) -> RedemptionResult:
        """Redeem via direct on-chain tx (pays POL gas)."""
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._web3_client.redeem_position(
                    condition_id=condition_id,
                    amounts=amounts,
                    neg_risk=neg_risk,
                ),
            )
            tx_hash = str(result) if result else None
            logger.info(f"Gas redeem OK: {label} tx={tx_hash}")
            self.total_redeemed += 1
            self.total_value_redeemed += value
            return RedemptionResult(
                condition_id=condition_id,
                success=True,
                tx_hash=tx_hash,
                value_redeemed=value,
            )
        except Exception as e:
            self.total_failures += 1
            err_str = str(e)
            if "insufficient funds" in err_str:
                return RedemptionResult(
                    condition_id=condition_id,
                    success=False,
                    error="No POL for gas fees (use gasless with Builder keys instead)",
                )
            return RedemptionResult(
                condition_id=condition_id,
                success=False,
                error=f"Gas redeem error: {e}",
            )

    # ------------------------------------------------------------------
    # Targeted redemption (called after settlement)
    # ------------------------------------------------------------------

    async def redeem_market(
        self, condition_id: str, settlement_value: float = 0.0
    ) -> List[RedemptionResult]:
        """
        Redeem all positions for a specific settled market.

        Called by TradingRunner._finalize_market() after a market resolves.

        Args:
            condition_id: The condition ID of the settled market.
            settlement_value: Expected value to recover (used by PaperRedeemer).

        Returns:
            List of redemption results (one per position/outcome).
        """
        if not condition_id:
            return []

        # Short delay to let the Data API reflect the settlement
        await asyncio.sleep(5.0)

        positions = await self.get_redeemable_positions(condition_id)
        if not positions:
            logger.info(
                f"No redeemable positions for condition {condition_id[:16]}..."
            )
            return []

        logger.info(
            f"Found {len(positions)} redeemable position(s) for "
            f"condition {condition_id[:16]}..."
        )

        results = []
        for pos in positions:
            result = await self.redeem_position(pos)
            results.append(result)
            if not result.success:
                logger.warning(
                    f"Redeem failed for {pos.title}: {result.error}"
                )
            # Small delay between redemptions to avoid rate limiting
            await asyncio.sleep(2.0)

        return results

    # ------------------------------------------------------------------
    # Background scan: catch any missed redemptions
    # ------------------------------------------------------------------

    async def start_background_scan(
        self, interval: Optional[float] = None
    ) -> None:
        """Start periodic background scan for redeemable positions."""
        if self._scan_task is not None:
            return

        self._running = True
        scan_interval = interval or self._scan_interval
        self._scan_task = asyncio.create_task(
            self._scan_loop(scan_interval)
        )
        logger.info(
            f"Redeemer background scan started (interval={scan_interval}s)"
        )

    async def stop(self) -> None:
        """Stop the background scan."""
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None
        logger.info(
            f"Redeemer stopped: {self.total_redeemed} redeemed, "
            f"${self.total_value_redeemed:.2f} recovered"
        )

    async def _scan_loop(self, interval: float) -> None:
        """Periodically scan for and redeem outstanding positions."""
        await asyncio.sleep(30.0)

        while self._running:
            try:
                await self._scan_and_redeem()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Background scan error: {e}", exc_info=True)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _scan_and_redeem(self) -> None:
        """Single scan iteration: find and redeem all redeemable positions."""
        positions = await self.get_redeemable_positions()
        if not positions:
            return

        by_condition: Dict[str, List[RedeemablePosition]] = {}
        for pos in positions:
            by_condition.setdefault(pos.condition_id, []).append(pos)

        total_value = sum(p.current_value for p in positions)
        logger.info(
            f"Background scan: found {len(positions)} redeemable position(s) "
            f"across {len(by_condition)} market(s), "
            f"total value ~${total_value:.2f}"
        )

        for cid, cid_positions in by_condition.items():
            for pos in cid_positions:
                result = await self.redeem_position(pos)
                if result.success:
                    logger.info(
                        f"Background redeem OK: {pos.title} "
                        f"(${result.value_redeemed:.2f})"
                    )
                else:
                    logger.warning(
                        f"Background redeem failed: {pos.title}: {result.error}"
                    )
                await asyncio.sleep(2.0)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def clear_cooldown(self, condition_id: str) -> None:
        """Clear the redemption cooldown for a specific condition."""
        self._recently_redeemed.pop(condition_id, None)

    @property
    def is_available(self) -> bool:
        """Check if redemption capability is available."""
        if self._clients_initialized:
            return self._web3_client is not None or self._gasless_client is not None
        try:
            import polymarket_apis  # noqa: F401
            return True
        except ImportError:
            return False


class PaperRedeemer:
    """
    Simulated redeemer for paper trading mode.

    Mimics the real redemption process with a configurable delay to model
    the real-world latency between settlement detection and USDC availability.
    Always succeeds (no external dependencies required).

    The delay models: settlement → Data API reflects → redeem tx confirms → USDC credited.
    Typical real-world latency is 30-120 seconds.
    """

    def __init__(self, redemption_delay: float = 60.0):
        """
        Args:
            redemption_delay: Seconds to simulate between settlement and capital release.
                             0 = instant (unrealistic), 60 = typical real-world latency.
        """
        self._delay = redemption_delay
        self.total_redeemed: int = 0
        self.total_value_redeemed: float = 0.0
        self.total_failures: int = 0

    @property
    def is_available(self) -> bool:
        return True

    async def redeem_market(
        self, condition_id: str, settlement_value: float = 0.0
    ) -> List[RedemptionResult]:
        """
        Simulate redemption with delay.

        Args:
            condition_id: The condition ID of the settled market.
            settlement_value: USDC value of winning tokens to recover.

        Returns:
            List with a single successful RedemptionResult.
        """
        if self._delay > 0:
            logger.info(
                f"Paper redeem: simulating {self._delay:.0f}s redemption delay "
                f"(condition={condition_id[:16]}..., value=${settlement_value:.2f})"
            )
            await asyncio.sleep(self._delay)

        self.total_redeemed += 1
        self.total_value_redeemed += settlement_value

        logger.info(
            f"Paper redeem complete: ${settlement_value:.2f} recovered "
            f"(condition={condition_id[:16]}...)"
        )

        return [RedemptionResult(
            condition_id=condition_id,
            success=True,
            value_redeemed=settlement_value,
        )]

    async def start_background_scan(self, **kwargs) -> None:
        """No-op for paper mode (no real positions to scan)."""
        pass

    async def stop(self) -> None:
        """Log final stats."""
        if self.total_redeemed > 0:
            logger.info(
                f"Paper redeemer stopped: {self.total_redeemed} redeemed, "
                f"${self.total_value_redeemed:.2f} recovered"
            )
