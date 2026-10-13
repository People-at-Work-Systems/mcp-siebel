"""Tests for _authenticate's SSO probe (follow-up #1).

In standalone mode _authenticate probes the SSO identity with a real data query
against the configured probe BO/BC and treats ANY failure as auth failure. The
catalogue (/data/describe) is guest-accessible on some Siebel instances and
can't validate the identity — a bogus header still gets a 200 there — so a
data query is required. The previous Basic-auth probe would "proceed anyway"
on a 500 and report a false "Authenticated as …".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError
from mcp_siebel.tools import SessionContext, ToolContext, _authenticate


def _ctx(**settings_overrides) -> ToolContext:
    base = {"mcp_mode": "standalone", "demo_enabled": False}
    base.update(settings_overrides)
    settings = AppSettings(**base)
    session = SessionContext(session_id="s1")
    return ToolContext(settings=settings, session=session)


@pytest.mark.asyncio
async def test_probe_failure_returns_auth_failed_not_authenticated():
    """A Siebel error on the probe query (e.g. a bogus SSO header that Siebel
    rejects) must surface as auth failure, not a false 'Authenticated'."""
    ctx = _ctx()
    client = AsyncMock()
    client.query = AsyncMock(side_effect=SiebelError("login rejected", 500, '{"ERROR":"...SBL-DAT-00569..."}'))

    with patch("mcp_siebel.tools._sso_client", return_value=client):
        result = await _authenticate({"username": "MSTERN", "password": "MSTERN"}, ctx)

    assert result.get("isError") is True
    assert "Authentication failed" in result["content"][0]["text"]
    # no false success: the session must not be marked authenticated
    assert ctx.session.client is None
    assert ctx.session.username is None
    # the probe queried the configured BO/BC
    client.query.assert_awaited_once()
    assert client.query.await_args.args == ("Account", "Account")


@pytest.mark.asyncio
async def test_probe_transport_error_returns_could_not_reach():
    ctx = _ctx()
    client = AsyncMock()
    client.query = AsyncMock(side_effect=RuntimeError("connection reset"))

    with patch("mcp_siebel.tools._sso_client", return_value=client):
        result = await _authenticate({"username": "MSTERN", "password": "MSTERN"}, ctx)

    assert result.get("isError") is True
    assert "could not reach Siebel" in result["content"][0]["text"]
    assert ctx.session.client is None


@pytest.mark.asyncio
async def test_probe_success_authenticates_and_reuses_probed_client():
    """On a successful probe the session uses the SAME client that was probed
    (no redundant second _sso_client), and reports 'Authenticated as …'."""
    ctx = _ctx()
    client = AsyncMock()
    client.query = AsyncMock(return_value=object())  # doesn't raise => success

    with patch("mcp_siebel.tools._sso_client", return_value=client) as mock_sso:
        result = await _authenticate({"username": "MSTERN", "password": "MSTERN"}, ctx)

    assert result.get("isError") is None
    assert "Authenticated as MSTERN" in result["content"][0]["text"]
    assert ctx.session.username == "MSTERN"
    assert ctx.session.client is client  # reused, not a second _sso_client
    assert ctx.session.cache is not None
    mock_sso.assert_called_once()  # _sso_client created exactly once


@pytest.mark.asyncio
async def test_probe_uses_configured_business_object_component():
    """The probe queries SIEBEL_PROBE_BUSINESS_OBJECT/COMPONENT, not a hardcoded
    BO/BC."""
    ctx = _ctx(probe_business_object="Opportunity", probe_business_component="Opportunity")
    client = AsyncMock()
    client.query = AsyncMock(return_value=object())

    with patch("mcp_siebel.tools._sso_client", return_value=client):
        await _authenticate({"username": "MSTERN", "password": "MSTERN"}, ctx)

    assert client.query.await_args.args == ("Opportunity", "Opportunity")


@pytest.mark.asyncio
async def test_standalone_still_requires_password():
    """The password-required guard is unchanged (contract preserved)."""
    ctx = _ctx()
    result = await _authenticate({"username": "MSTERN"}, ctx)
    assert result.get("isError") is True
    assert "password is required" in result["content"][0]["text"]
