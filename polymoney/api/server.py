"""
API Server - FastAPI REST server for monitoring and control.

Provides endpoints for strategies, markets, orders, metrics, and system health.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from polymoney.core.config import Config
from polymoney.core.logging import get_logger

logger = get_logger("api.server")

# API Key security (optional)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# Request/Response Models
class StrategyCreateRequest(BaseModel):
    """Request to create a strategy instance."""

    strategy_type: str = Field(description="Registered strategy type name")
    strategy_id: str = Field(description="Unique strategy instance ID")
    position_size: float = Field(default=100.0, description="Maximum position size")
    params: Dict[str, Any] = Field(default_factory=dict, description="Strategy parameters")


class StrategyConfigUpdate(BaseModel):
    """Request to update strategy configuration."""

    params: Optional[Dict[str, Any]] = Field(default=None)
    position_size: Optional[float] = Field(default=None)


class StrategyStartRequest(BaseModel):
    """Request to start a strategy."""

    markets: List[str] = Field(default_factory=list, description="Market IDs to subscribe")


class MarketSubscribeRequest(BaseModel):
    """Request to subscribe to a market."""

    token_ids: List[str] = Field(description="Token IDs for YES/NO")
    title: str = Field(default="", description="Market title")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(description="healthy, degraded, or unhealthy")
    timestamp: str = Field(description="Current timestamp")
    components: Dict[str, str] = Field(description="Component statuses")


class ErrorResponse(BaseModel):
    """Error response model."""

    error: str
    detail: Optional[str] = None


def create_app(
    config: Config = None,
    strategy_engine=None,
    market_data_service=None,
    data_storage=None,
) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Args:
        config: Application configuration
        strategy_engine: Strategy engine instance
        market_data_service: Market data service instance
        data_storage: Data storage instance

    Returns:
        Configured FastAPI app
    """
    app = FastAPI(
        title="Polymoney API",
        description="Polymarket Trading Bot Framework API",
        version="0.1.0",
    )

    # CORS middleware
    origins = config.api.cors_origins if config else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store dependencies
    app.state.config = config
    app.state.engine = strategy_engine
    app.state.market_data = market_data_service
    app.state.storage = data_storage

    # API key validation dependency
    async def verify_api_key(api_key: str = Security(api_key_header)):
        if config and config.api.api_key:
            if not api_key:
                raise HTTPException(status_code=401, detail="API key required")
            if api_key != config.api.api_key:
                raise HTTPException(status_code=403, detail="Invalid API key")
        return api_key

    # ==================== Strategy Endpoints ====================

    @app.get("/strategies", tags=["Strategies"])
    async def list_strategies(api_key: str = Depends(verify_api_key)):
        """List all strategy instances."""
        if not app.state.engine:
            return {"strategies": [], "available_types": []}

        engine = app.state.engine
        status = engine.get_status()

        return {
            "strategies": status.get("strategies", {}),
            "available_types": engine.list_available_strategies(),
            "trading_mode": status.get("trading_mode", "paper"),
        }

    @app.get("/strategies/{strategy_id}", tags=["Strategies"])
    async def get_strategy(strategy_id: str, api_key: str = Depends(verify_api_key)):
        """Get strategy details."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        strategy = app.state.engine.get_strategy(strategy_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")

        return {
            "strategy_id": strategy.strategy_id,
            "name": strategy.name,
            "type": strategy.strategy_type,
            "state": strategy.state.value,
            "position_size": strategy.position_size,
            "params": strategy.params,
            "metrics": strategy.metrics.model_dump(),
            "status": strategy.get_status(),
        }

    @app.post("/strategies", tags=["Strategies"])
    async def create_strategy(
        request: StrategyCreateRequest, api_key: str = Depends(verify_api_key)
    ):
        """Create a new strategy instance."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        strategy = app.state.engine.add_strategy(
            strategy_type=request.strategy_type,
            strategy_id=request.strategy_id,
            position_size=request.position_size,
            params=request.params,
        )

        if not strategy:
            raise HTTPException(status_code=400, detail="Unknown strategy type")

        return {"strategy_id": strategy.strategy_id, "created": True}

    @app.post("/strategies/{strategy_id}/start", tags=["Strategies"])
    async def start_strategy(
        strategy_id: str,
        request: StrategyStartRequest = None,
        api_key: str = Depends(verify_api_key),
    ):
        """Start a strategy."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        markets = request.markets if request else []
        success = await app.state.engine.start_strategy(strategy_id, markets)

        if not success:
            raise HTTPException(status_code=400, detail="Failed to start strategy")

        return {"strategy_id": strategy_id, "state": "running"}

    @app.post("/strategies/{strategy_id}/stop", tags=["Strategies"])
    async def stop_strategy(strategy_id: str, api_key: str = Depends(verify_api_key)):
        """Stop a strategy."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        success = await app.state.engine.stop_strategy(strategy_id)

        if not success:
            raise HTTPException(status_code=400, detail="Failed to stop strategy")

        return {"strategy_id": strategy_id, "state": "stopped"}

    @app.post("/strategies/{strategy_id}/pause", tags=["Strategies"])
    async def pause_strategy(strategy_id: str, api_key: str = Depends(verify_api_key)):
        """Pause a strategy."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        success = await app.state.engine.pause_strategy(strategy_id)

        if not success:
            raise HTTPException(status_code=400, detail="Failed to pause strategy")

        return {"strategy_id": strategy_id, "state": "paused"}

    @app.put("/strategies/{strategy_id}/config", tags=["Strategies"])
    async def update_strategy_config(
        strategy_id: str,
        request: StrategyConfigUpdate,
        api_key: str = Depends(verify_api_key),
    ):
        """Update strategy configuration."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        strategy = app.state.engine.get_strategy(strategy_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")

        if request.params:
            strategy.params.update(request.params)
        if request.position_size is not None:
            strategy.position_size = request.position_size

        return {"strategy_id": strategy_id, "updated": True}

    # ==================== Market Endpoints ====================

    @app.get("/markets", tags=["Markets"])
    async def list_markets(api_key: str = Depends(verify_api_key)):
        """List subscribed markets."""
        if not app.state.market_data:
            return {"markets": []}

        markets = []
        for market_id in app.state.market_data.subscribed_markets:
            info = app.state.market_data.get_market_info(market_id)
            price = app.state.market_data.get_latest_price(market_id)

            markets.append({
                "market_id": market_id,
                "title": info.get("title", "") if info else "",
                "up_price": price.up_price if price else None,
                "down_price": price.down_price if price else None,
            })

        return {"markets": markets}

    @app.post("/markets/{market_id}/subscribe", tags=["Markets"])
    async def subscribe_market(
        market_id: str,
        request: MarketSubscribeRequest,
        api_key: str = Depends(verify_api_key),
    ):
        """Subscribe to a market."""
        if not app.state.market_data:
            raise HTTPException(status_code=503, detail="Market data service not available")

        await app.state.market_data.subscribe_market(
            market_id=market_id,
            token_ids=request.token_ids,
            title=request.title,
        )

        return {"market_id": market_id, "subscribed": True}

    @app.delete("/markets/{market_id}/subscribe", tags=["Markets"])
    async def unsubscribe_market(market_id: str, api_key: str = Depends(verify_api_key)):
        """Unsubscribe from a market."""
        if not app.state.market_data:
            raise HTTPException(status_code=503, detail="Market data service not available")

        await app.state.market_data.unsubscribe_market(market_id)

        return {"market_id": market_id, "unsubscribed": True}

    # ==================== Order Endpoints ====================

    @app.get("/orders", tags=["Orders"])
    async def list_orders(
        strategy_id: Optional[str] = Query(None),
        market_id: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        limit: int = Query(100, le=1000),
        api_key: str = Depends(verify_api_key),
    ):
        """Query orders with filters."""
        if not app.state.storage:
            return {"orders": []}

        orders = await app.state.storage.get_orders(
            strategy_id=strategy_id,
            market_id=market_id,
            status=status,
            limit=limit,
        )

        return {"orders": orders}

    @app.get("/orders/{order_id}", tags=["Orders"])
    async def get_order(order_id: str, api_key: str = Depends(verify_api_key)):
        """Get order details."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        order = app.state.engine.order_manager.get_order(order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        return order.model_dump()

    # ==================== Metrics Endpoints ====================

    @app.get("/metrics", tags=["Metrics"])
    async def get_metrics(api_key: str = Depends(verify_api_key)):
        """Get overall metrics."""
        metrics = {
            "timestamp": datetime.now().isoformat(),
            "strategies": {},
            "total_pnl": 0.0,
            "active_strategies": 0,
            "subscribed_markets": 0,
        }

        if app.state.engine:
            status = app.state.engine.get_status()
            metrics["active_strategies"] = len([
                s for s in status.get("strategies", {}).values()
                if s.get("state") == "running"
            ])
            metrics["pending_orders"] = status.get("pending_orders", 0)

        if app.state.market_data:
            metrics["subscribed_markets"] = len(app.state.market_data.subscribed_markets)

        return metrics

    @app.get("/metrics/strategies/{strategy_id}", tags=["Metrics"])
    async def get_strategy_metrics(strategy_id: str, api_key: str = Depends(verify_api_key)):
        """Get strategy-specific metrics."""
        if not app.state.engine:
            raise HTTPException(status_code=503, detail="Engine not available")

        strategy = app.state.engine.get_strategy(strategy_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")

        return {
            "strategy_id": strategy_id,
            "metrics": strategy.metrics.model_dump(),
        }

    # ==================== System Endpoints ====================

    @app.get("/health", tags=["System"], response_model=HealthResponse)
    async def health_check():
        """Health check endpoint."""
        components = {
            "api": "healthy",
            "database": "unknown",
            "websocket": "unknown",
        }

        if app.state.storage:
            try:
                # Simple connectivity check would go here
                components["database"] = "healthy"
            except Exception:
                components["database"] = "unhealthy"

        if app.state.market_data:
            health = app.state.market_data.get_health_status()
            ws_state = health.get("websocket", {}).get("state", "unknown")
            components["websocket"] = "healthy" if ws_state == "connected" else "degraded"

        # Determine overall status
        if all(c == "healthy" for c in components.values()):
            status = "healthy"
        elif "unhealthy" in components.values():
            status = "unhealthy"
        else:
            status = "degraded"

        return HealthResponse(
            status=status,
            timestamp=datetime.now().isoformat(),
            components=components,
        )

    @app.get("/logs", tags=["System"])
    async def get_logs(
        level: str = Query("INFO"),
        limit: int = Query(100, le=1000),
        api_key: str = Depends(verify_api_key),
    ):
        """Get recent log entries (placeholder)."""
        # In production, this would read from a log buffer or file
        return {
            "logs": [],
            "note": "Log streaming not yet implemented",
        }

    @app.get("/config", tags=["System"])
    async def get_config(api_key: str = Depends(verify_api_key)):
        """Get current configuration (sensitive values masked)."""
        if not app.state.config:
            return {}

        config = app.state.config

        return {
            "log_level": config.log_level,
            "trading_mode": config.trading.mode,
            "max_position_size": config.trading.max_position_size,
            "candlestick_intervals": config.data.candlestick_intervals,
            "api_host": config.api.host,
            "api_port": config.api.port,
            "database_url": "***",  # Masked
            "polymarket_host": config.polymarket.host,
            "has_api_key": bool(config.api.api_key),
            "has_private_key": bool(config.polymarket.private_key),
        }

    @app.put("/config", tags=["System"])
    async def update_config(
        updates: Dict[str, Any],
        api_key: str = Depends(verify_api_key),
    ):
        """Update configuration (limited fields)."""
        allowed_updates = ["log_level", "trading_mode"]

        applied = {}
        for key, value in updates.items():
            if key in allowed_updates:
                if key == "log_level" and app.state.config:
                    app.state.config.log_level = value
                    applied[key] = value
                elif key == "trading_mode" and app.state.engine:
                    try:
                        app.state.engine.set_trading_mode(value)
                        applied[key] = value
                    except Exception as e:
                        raise HTTPException(status_code=400, detail=str(e))

        return {"updated": applied}

    return app


# Convenience function to run the server
def run_server(app: FastAPI, host: str = "0.0.0.0", port: int = 8000):
    """Run the API server."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
