"""Tests for the Prometheus /metrics endpoint."""

from fastapi.testclient import TestClient

from mcp_siebel.http_server import app

client = TestClient(app)


def test_metrics_endpoint_returns_200():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text

