"""Tests for the API-token guard (findings 1 & 2).

Finding 1: ``MCP_API_TOKEN`` (and the legacy ``SIEBEL_API_TOKEN`` form) populate
``AppSettings.api_token``; the HTTP server warns at startup when no token is set.
Finding 2: the MCP SSE transport endpoints (``/sse`` and ``/messages/``) are
guarded by the same token as the REST surface — the mounted ``/messages/`` sub-app
can't take FastAPI route dependencies, so the guard is enforced via middleware.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mcp_siebel.http_server as http_server
from mcp_siebel.config import AppSettings

client = TestClient(http_server.app)


# --------------------------------------------------------------------------- #
# Finding 1 — env-var population
# --------------------------------------------------------------------------- #


def test_mcp_api_token_env_populates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    monkeypatch.delenv("SIEBEL_API_TOKEN", raising=False)
    monkeypatch.setenv("MCP_API_TOKEN", "x")
    assert AppSettings(_env_file=None).api_token == "x"


def test_siebel_api_token_env_still_populates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: the prefixed SIEBEL_API_TOKEN form must keep working
    after the MCP_API_TOKEN alias is added (a single alias would have dropped it)."""
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    monkeypatch.delenv("SIEBEL_API_TOKEN", raising=False)
    monkeypatch.setenv("SIEBEL_API_TOKEN", "y")
    assert AppSettings(_env_file=None).api_token == "y"


def test_no_token_leaves_field_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    monkeypatch.delenv("SIEBEL_API_TOKEN", raising=False)
    assert AppSettings(_env_file=None).api_token == ""


# --------------------------------------------------------------------------- #
# Finding 1 & 2 — the guard on the REST + SSE surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def with_token() -> str:
    """Configure the server's module-level settings with a token for the test,
    then restore the previous value."""
    saved = http_server.settings.api_token
    http_server.settings.api_token = "secret"
    try:
        yield "secret"
    finally:
        http_server.settings.api_token = saved


def test_tools_reject_missing_token(with_token: str) -> None:
    assert client.get("/tools").status_code == 401


def test_tools_accept_valid_bearer(with_token: str) -> None:
    r = client.get("/tools", headers={"Authorization": f"Bearer {with_token}"})
    assert r.status_code == 200


def test_tools_reject_wrong_token(with_token: str) -> None:
    r = client.get("/tools", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_sse_reject_missing_token(with_token: str) -> None:
    # No header -> the SSE-transport middleware short-circuits with 401 before
    # any streaming starts (so the TestClient does not hang).
    assert client.get("/sse").status_code == 401


def test_messages_reject_missing_token(with_token: str) -> None:
    r = client.post("/messages/?session_id=x", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    assert r.status_code == 401


def test_messages_accepts_valid_token(with_token: str) -> None:
    # A valid token passes the guard; the sub-app then rejects the bogus session
    # (400/etc.), which is fine — the point is it is NOT 401.
    r = client.post(
        "/messages/?session_id=x",
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Authorization": f"Bearer {with_token}"},
    )
    assert r.status_code != 401


def test_no_token_means_open_server() -> None:
    """When no token is configured the guard is a no-op and routes are open."""
    saved = http_server.settings.api_token
    http_server.settings.api_token = ""
    try:
        assert client.get("/tools").status_code == 200
    finally:
        http_server.settings.api_token = saved
