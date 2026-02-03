"""
Data Storage Layer - SQLAlchemy models and database operations.

Provides persistence for candlesticks, orders, trades, positions, and configuration.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.future import select
from sqlalchemy.orm import declarative_base, sessionmaker

from polymoney.core.logging import get_logger
from polymoney.core.models import (
    Candlestick as CandlestickModel,
    Order as OrderModel,
    OrderStatus,
    OrderType,
    TokenType,
    TradeResult,
    TradeSide,
)

logger = get_logger("data.storage")

Base = declarative_base()


# SQLAlchemy Models
class CandlestickDB(Base):
    """Candlestick database model."""

    __tablename__ = "candlesticks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(String(128), nullable=False, index=True)
    interval = Column(String(8), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    token_type = Column(String(8), nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_candle_market_interval_time", "market_id", "interval", "timestamp"),
    )


class OrderDB(Base):
    """Order database model."""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    market_id = Column(String(128), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    token_type = Column(String(8), nullable=False)
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    filled_size = Column(Float, default=0)
    status = Column(String(16), nullable=False, default="pending")
    order_type = Column(String(8), default="GTC")
    mode = Column(String(8), default="paper")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expiration = Column(DateTime, nullable=True)


class TradeDB(Base):
    """Trade execution record."""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String(64), unique=True, nullable=False, index=True)
    order_id = Column(String(64), nullable=False, index=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    market_id = Column(String(128), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    token_type = Column(String(8), nullable=False)
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    mode = Column(String(8), default="paper")
    timestamp = Column(DateTime, default=datetime.utcnow)


class PositionSnapshotDB(Base):
    """Position snapshot for recovery and history."""

    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    market_id = Column(String(128), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    up_shares = Column(Float, default=0)
    up_cost = Column(Float, default=0)
    down_shares = Column(Float, default=0)
    down_cost = Column(Float, default=0)
    unrealized_pnl = Column(Float, default=0)
    realized_pnl = Column(Float, default=0)


class StrategyConfigDB(Base):
    """Strategy configuration storage."""

    __tablename__ = "strategy_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String(64), unique=True, nullable=False, index=True)
    strategy_type = Column(String(64), nullable=False)
    params_json = Column(Text, nullable=True)
    trading_mode = Column(String(8), default="paper")
    position_size = Column(Float, default=100.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyMetricsDB(Base):
    """Daily performance metrics."""

    __tablename__ = "daily_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    date = Column(DateTime, nullable=False, index=True)
    pnl = Column(Float, default=0)
    trade_count = Column(Integer, default=0)
    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)
    max_drawdown = Column(Float, default=0)

    __table_args__ = (Index("ix_metrics_strategy_date", "strategy_id", "date"),)


class DataStorage:
    """
    Database access layer for all storage operations.

    Provides async methods for CRUD operations on all entities.
    """

    def __init__(self, database_url: str = "sqlite+aiosqlite:///polymoney.db"):
        """
        Initialize storage.

        Args:
            database_url: SQLAlchemy database URL
        """
        self.database_url = database_url
        self._engine = create_async_engine(database_url, echo=False)
        self._session_factory = sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def initialize(self) -> None:
        """Create database tables."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized")

    async def close(self) -> None:
        """Close database connection."""
        await self._engine.dispose()

    # Candlestick Operations
    async def save_candlestick(self, candle: CandlestickModel) -> None:
        """Save a candlestick to database."""
        async with self._session_factory() as session:
            db_candle = CandlestickDB(
                market_id=candle.market_id,
                interval=candle.interval,
                timestamp=candle.timestamp,
                token_type=candle.token_type.value,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
            session.add(db_candle)
            await session.commit()

    async def get_candlesticks(
        self,
        market_id: str,
        interval: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[CandlestickModel]:
        """Query candlesticks by time range."""
        async with self._session_factory() as session:
            query = select(CandlestickDB).where(
                CandlestickDB.market_id == market_id,
                CandlestickDB.interval == interval,
            )

            if start_time:
                query = query.where(CandlestickDB.timestamp >= start_time)
            if end_time:
                query = query.where(CandlestickDB.timestamp <= end_time)

            query = query.order_by(CandlestickDB.timestamp.asc()).limit(limit)

            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                CandlestickModel(
                    market_id=r.market_id,
                    interval=r.interval,
                    timestamp=r.timestamp,
                    token_type=TokenType(r.token_type),
                    open=r.open,
                    high=r.high,
                    low=r.low,
                    close=r.close,
                    volume=r.volume,
                )
                for r in rows
            ]

    # Trade Operations
    async def save_trade(self, trade: TradeResult) -> None:
        """Save a trade record."""
        async with self._session_factory() as session:
            db_trade = TradeDB(
                trade_id=trade.trade_id,
                order_id=trade.order_id,
                strategy_id=trade.strategy_id,
                market_id=trade.market_id,
                side=trade.side.value if hasattr(trade.side, "value") else trade.side,
                token_type=trade.token_type.value if hasattr(trade.token_type, "value") else trade.token_type,
                price=trade.price,
                size=trade.size,
                mode=trade.mode,
                timestamp=trade.timestamp,
            )
            session.add(db_trade)
            await session.commit()

    async def get_trades(
        self,
        strategy_id: Optional[str] = None,
        market_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[TradeResult]:
        """Query trades with filters."""
        async with self._session_factory() as session:
            query = select(TradeDB)

            if strategy_id:
                query = query.where(TradeDB.strategy_id == strategy_id)
            if market_id:
                query = query.where(TradeDB.market_id == market_id)
            if start_time:
                query = query.where(TradeDB.timestamp >= start_time)
            if end_time:
                query = query.where(TradeDB.timestamp <= end_time)

            query = query.order_by(TradeDB.timestamp.desc()).limit(limit)

            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                TradeResult(
                    trade_id=r.trade_id,
                    order_id=r.order_id,
                    strategy_id=r.strategy_id,
                    market_id=r.market_id,
                    side=TradeSide(r.side),
                    token_type=TokenType(r.token_type),
                    price=r.price,
                    size=r.size,
                    mode=r.mode,
                    timestamp=r.timestamp,
                )
                for r in rows
            ]

    # Order Operations
    async def save_order(self, order: OrderModel) -> None:
        """Save an order record."""
        async with self._session_factory() as session:
            db_order = OrderDB(
                order_id=order.order_id,
                strategy_id=order.strategy_id,
                market_id=order.market_id,
                side=order.side.value if hasattr(order.side, "value") else order.side,
                token_type=order.token_type.value if hasattr(order.token_type, "value") else order.token_type,
                price=order.price,
                size=order.size,
                filled_size=order.filled_size,
                status=order.status.value if hasattr(order.status, "value") else order.status,
                order_type=order.order_type.value if hasattr(order.order_type, "value") else order.order_type,
                created_at=order.created_at,
                updated_at=order.updated_at,
                expiration=order.expiration,
            )
            session.add(db_order)
            await session.commit()

    async def update_order_status(self, order_id: str, status: str, filled_size: float = None) -> None:
        """Update order status."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(OrderDB).where(OrderDB.order_id == order_id)
            )
            order = result.scalar_one_or_none()

            if order:
                order.status = status
                order.updated_at = datetime.utcnow()
                if filled_size is not None:
                    order.filled_size = filled_size
                await session.commit()

    async def get_orders(
        self,
        strategy_id: Optional[str] = None,
        market_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Query orders with filters."""
        async with self._session_factory() as session:
            query = select(OrderDB)

            if strategy_id:
                query = query.where(OrderDB.strategy_id == strategy_id)
            if market_id:
                query = query.where(OrderDB.market_id == market_id)
            if status:
                query = query.where(OrderDB.status == status)

            query = query.order_by(OrderDB.created_at.desc()).limit(limit)

            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                {
                    "order_id": r.order_id,
                    "strategy_id": r.strategy_id,
                    "market_id": r.market_id,
                    "side": r.side,
                    "token_type": r.token_type,
                    "price": r.price,
                    "size": r.size,
                    "filled_size": r.filled_size,
                    "status": r.status,
                    "order_type": r.order_type,
                    "mode": r.mode,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in rows
            ]

    # Position Snapshot Operations
    async def save_position_snapshot(
        self,
        strategy_id: str,
        market_id: str,
        up_shares: float,
        up_cost: float,
        down_shares: float,
        down_cost: float,
        unrealized_pnl: float = 0,
        realized_pnl: float = 0,
    ) -> None:
        """Save a position snapshot."""
        async with self._session_factory() as session:
            snapshot = PositionSnapshotDB(
                strategy_id=strategy_id,
                market_id=market_id,
                up_shares=up_shares,
                up_cost=up_cost,
                down_shares=down_shares,
                down_cost=down_cost,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
            )
            session.add(snapshot)
            await session.commit()

    async def get_latest_position_snapshot(
        self, strategy_id: str, market_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get the latest position snapshot for recovery."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionSnapshotDB)
                .where(
                    PositionSnapshotDB.strategy_id == strategy_id,
                    PositionSnapshotDB.market_id == market_id,
                )
                .order_by(PositionSnapshotDB.timestamp.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()

            if not row:
                return None

            return {
                "strategy_id": row.strategy_id,
                "market_id": row.market_id,
                "timestamp": row.timestamp.isoformat(),
                "up_shares": row.up_shares,
                "up_cost": row.up_cost,
                "down_shares": row.down_shares,
                "down_cost": row.down_cost,
                "unrealized_pnl": row.unrealized_pnl,
                "realized_pnl": row.realized_pnl,
            }

    # Configuration Operations
    async def save_strategy_config(
        self,
        strategy_id: str,
        strategy_type: str,
        params: Dict[str, Any],
        trading_mode: str = "paper",
        position_size: float = 100.0,
    ) -> None:
        """Save strategy configuration."""
        import json

        async with self._session_factory() as session:
            # Check if exists
            result = await session.execute(
                select(StrategyConfigDB).where(StrategyConfigDB.strategy_id == strategy_id)
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.strategy_type = strategy_type
                existing.params_json = json.dumps(params)
                existing.trading_mode = trading_mode
                existing.position_size = position_size
                existing.updated_at = datetime.utcnow()
            else:
                config = StrategyConfigDB(
                    strategy_id=strategy_id,
                    strategy_type=strategy_type,
                    params_json=json.dumps(params),
                    trading_mode=trading_mode,
                    position_size=position_size,
                )
                session.add(config)

            await session.commit()

    async def get_strategy_config(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """Load strategy configuration."""
        import json

        async with self._session_factory() as session:
            result = await session.execute(
                select(StrategyConfigDB).where(StrategyConfigDB.strategy_id == strategy_id)
            )
            row = result.scalar_one_or_none()

            if not row:
                return None

            return {
                "strategy_id": row.strategy_id,
                "strategy_type": row.strategy_type,
                "params": json.loads(row.params_json) if row.params_json else {},
                "trading_mode": row.trading_mode,
                "position_size": row.position_size,
                "is_active": row.is_active,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }

    async def list_strategy_configs(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """List all strategy configurations."""
        import json

        async with self._session_factory() as session:
            query = select(StrategyConfigDB)
            if active_only:
                query = query.where(StrategyConfigDB.is_active == True)

            result = await session.execute(query)
            rows = result.scalars().all()

            return [
                {
                    "strategy_id": r.strategy_id,
                    "strategy_type": r.strategy_type,
                    "params": json.loads(r.params_json) if r.params_json else {},
                    "trading_mode": r.trading_mode,
                    "position_size": r.position_size,
                    "is_active": r.is_active,
                }
                for r in rows
            ]

    # Data Retention
    async def cleanup_old_data(self, retention_days: int = 30) -> Dict[str, int]:
        """Delete data older than retention period."""
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        deleted = {}

        async with self._session_factory() as session:
            # Candlesticks
            result = await session.execute(
                CandlestickDB.__table__.delete().where(CandlestickDB.created_at < cutoff)
            )
            deleted["candlesticks"] = result.rowcount

            # Position snapshots (keep more recent)
            result = await session.execute(
                PositionSnapshotDB.__table__.delete().where(PositionSnapshotDB.timestamp < cutoff)
            )
            deleted["position_snapshots"] = result.rowcount

            await session.commit()

        logger.info(f"Data cleanup complete: {deleted}")
        return deleted
