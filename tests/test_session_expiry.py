"""Tests for the mcp-siebel session expiry mechanism (UC-42).

Sessions expire absolutely ``session_timeout_ms`` after creation and are
evicted lazily on lookup + swept on every new session, so the in-memory
map can't grow unbounded. Expiry is driven off ``time.monotonic``; tests
manipulate ``SessionContext.created_at`` to fast-forward deterministically
rather than sleeping.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from mcp_siebel.config import AppSettings
from mcp_siebel.http_server import app, sessions, settings
from mcp_siebel.tools import SessionStore


def _store(timeout_s: float = 10.0) -> SessionStore:
    return SessionStore(timeout_s)


def test_get_returns_session_while_fresh_then_evicts_on_expiry() -> None:
    store = _store(timeout_s=10.0)
    session = store.create()
    assert store.get(session.session_id) is session  # fresh → found

    # Fast-forward past the timeout → treated as not found and removed.
    session.created_at = time.monotonic() - 11
    assert store.get(session.session_id) is None
    assert session.session_id not in store._sessions


def test_create_sweeps_expired_sessions() -> None:
    store = _store(timeout_s=10.0)
    expired = store.create()
    expired.created_at = time.monotonic() - 20

    # Creating a new session prunes the expired one.
    fresh = store.create()
    assert store.get(expired.session_id) is None
    assert expired.session_id not in store._sessions
    assert store.get(fresh.session_id) is fresh


def test_sweep_removes_only_expired_and_count_reflects_live() -> None:
    store = _store(timeout_s=10.0)
    a, b, c = store.create(), store.create(), store.create()
    a.created_at = time.monotonic() - 100
    b.created_at = time.monotonic() - 100

    removed = store.sweep()
    assert removed == 2
    assert store.count() == 1
    assert store.get(c.session_id) is c


def test_timeout_is_wired_from_config() -> None:
    # The previously-dead session_timeout_ms now drives the store's TTL.
    assert sessions._timeout_s == settings.session_timeout_ms / 1000
    assert isinstance(AppSettings().session_timeout_ms, int)


def test_get_session_endpoint_returns_404_when_expired() -> None:
    client = TestClient(app)
    session = sessions.create()
    # Mark it authenticated so a fresh session would pass the 200 check —
    # that way the 404 below is provably from expiry, not "not authenticated".
    session.demo_authenticated = True

    fresh = client.get(f"/sessions/{session.session_id}")
    assert fresh.status_code == 200

    # Age it past the default 30-min timeout.
    session.created_at = time.monotonic() - (settings.session_timeout_ms / 1000 + 1)
    expired = client.get(f"/sessions/{session.session_id}")
    assert expired.status_code == 404
    # And it has been evicted from the store, not just hidden.
    assert sessions.get(session.session_id) is None
