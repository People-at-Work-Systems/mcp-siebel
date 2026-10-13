"""Tests for the health endpoint."""

from fastapi.testclient import TestClient

from mcp_siebel.http_server import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_tools_are_listed():
    response = client.get("/tools")
    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()["tools"]}
    assert "authenticate" in tool_names
    assert "query_data" in tool_names
    assert "calculate_date" in tool_names
