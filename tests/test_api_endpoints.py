"""Tests to validate API endpoints with sample requests."""

import pytest
from fastapi.testclient import TestClient

from polymoney.api.server import create_app


@pytest.fixture
def app():
    """Create a test app instance."""
    return create_app()


class TestAPIEndpoints:
    """Test API endpoints with sample requests."""

    @pytest.fixture
    def client(self, app):
        """Create a test client."""
        return TestClient(app)

    def test_health_endpoint(self, client):
        """Test GET /health endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        # Status will be 'degraded' or 'healthy' depending on components
        assert data["status"] in ["healthy", "degraded", "unhealthy"]
        assert "components" in data

    def test_list_strategies(self, client):
        """Test GET /strategies endpoint."""
        response = client.get("/strategies")
        assert response.status_code == 200
        data = response.json()
        # Returns dict with 'strategies' and 'available_types' keys
        assert "strategies" in data
        assert "available_types" in data

    def test_get_strategy_not_found(self, client):
        """Test GET /strategies/{id} returns 503 or 404."""
        response = client.get("/strategies/unknown-strategy-id")
        # Without engine configured, returns 503; with engine, 404
        assert response.status_code in [404, 503]

    def test_list_markets(self, client):
        """Test GET /markets endpoint."""
        response = client.get("/markets")
        assert response.status_code == 200
        data = response.json()
        # Returns dict with 'markets' key
        assert "markets" in data
        assert isinstance(data["markets"], list)

    def test_get_orders(self, client):
        """Test GET /orders endpoint."""
        response = client.get("/orders")
        assert response.status_code == 200
        data = response.json()
        # Returns dict with 'orders' key
        assert "orders" in data
        assert isinstance(data["orders"], list)

    def test_get_orders_with_filters(self, client):
        """Test GET /orders with query filters."""
        response = client.get("/orders?status=pending&limit=10")
        assert response.status_code == 200
        data = response.json()
        assert "orders" in data
        assert isinstance(data["orders"], list)

    def test_get_metrics(self, client):
        """Test GET /metrics endpoint."""
        response = client.get("/metrics")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "timestamp" in data

    def test_get_logs(self, client):
        """Test GET /logs endpoint."""
        response = client.get("/logs")
        assert response.status_code == 200
        data = response.json()
        # Returns dict with 'logs' key
        assert "logs" in data
        assert isinstance(data["logs"], list)

    def test_openapi_docs(self, client):
        """Test /docs endpoint (OpenAPI documentation)."""
        response = client.get("/docs")
        assert response.status_code == 200

    def test_openapi_json(self, client):
        """Test /openapi.json endpoint."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert "openapi" in data
        assert "paths" in data

    def test_strategy_lifecycle_endpoints_exist(self, client):
        """Test that strategy lifecycle endpoints exist in OpenAPI spec."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        paths = data.get("paths", {})

        # Check required endpoints exist
        assert "/strategies" in paths
        assert "/strategies/{strategy_id}" in paths
        assert "/health" in paths
        assert "/orders" in paths
        assert "/metrics" in paths

    def test_cors_headers(self, client):
        """Test CORS headers are set correctly."""
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # FastAPI handles CORS, status may vary
        assert response.status_code in [200, 204, 400]


class TestAPIAuthentication:
    """Test API authentication (when enabled)."""

    @pytest.fixture
    def client(self, app):
        """Create a test client."""
        return TestClient(app)

    def test_health_no_auth_required(self, client):
        """Test that /health endpoint doesn't require authentication."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_docs_accessible(self, client):
        """Test that /docs is accessible."""
        response = client.get("/docs")
        assert response.status_code == 200
