"""Tests for ToolRegistry.call error handling and _error_result (follow-up #2).

Tool handlers (or the Siebel client beneath them) can raise SiebelError.
ToolRegistry.call must convert that into a structured isError result instead of
letting it propagate as an HTTP 500 (which aborts the SSE stream and hides the
real error from the caller). _error_result must flag failures with
isError: true (it previously returned _text, which has no isError).
"""

from __future__ import annotations

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.handlers._common import _error_result
from mcp_siebel.siebel_client import SiebelError
from mcp_siebel.tools import SessionContext, ToolRegistry, ToolSpec

_SIMPLE_SCHEMA = {"type": "object", "properties": {}}


def _registry() -> ToolRegistry:
    return ToolRegistry(AppSettings())


@pytest.mark.asyncio
async def test_registry_call_converts_siebel_error_to_iserror_result():
    """A handler raising SiebelError (e.g. SBL-EAI-50257 BC-not-mapped) must
    come back as isError, not propagate as a 500."""

    async def boom(args, ctx):  # noqa: ANN001
        raise SiebelError("BC not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}')

    reg = _registry()
    reg._tools["boom"] = ToolSpec("boom", "d", _SIMPLE_SCHEMA, boom)

    result = await reg.call("boom", {}, SessionContext(session_id="s1"))

    assert result.get("isError") is True
    assert "Siebel operation could not be completed" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_registry_call_converts_unexpected_exception_to_iserror_result():
    """Any handler crash (not just SiebelError) becomes an isError result."""

    async def kaboom(args, ctx):  # noqa: ANN001
        raise RuntimeError("handler bug")

    reg = _registry()
    reg._tools["kaboom"] = ToolSpec("kaboom", "d", _SIMPLE_SCHEMA, kaboom)

    result = await reg.call("kaboom", {}, SessionContext(session_id="s1"))
    assert result.get("isError") is True


@pytest.mark.asyncio
async def test_registry_call_still_returns_successful_result_unchanged():
    """The wrapper only intercepts exceptions; successful results pass through."""

    async def ok(args, ctx):  # noqa: ANN001
        return {"content": [{"type": "text", "text": "ok"}]}

    reg = _registry()
    reg._tools["ok"] = ToolSpec("ok", "d", _SIMPLE_SCHEMA, ok)

    result = await reg.call("ok", {}, SessionContext(session_id="s1"))
    assert result.get("isError") is None
    assert result["content"][0]["text"] == "ok"


def test_error_result_401_is_session_expired_error():
    res = _error_result(SiebelError("unauthorized", 401, ""), "query_data")
    assert res["isError"] is True
    assert "expired" in res["content"][0]["text"].lower()


def test_error_result_500_is_iserror_not_text():
    """Before the fix this returned _text (no isError). Now it's a proper error."""
    res = _error_result(SiebelError("boom", 500, '{"ERROR":"..."}'), "query_data")
    assert res["isError"] is True
    assert "Siebel operation could not be completed" in res["content"][0]["text"]


def test_error_result_unexpected_exception_is_iserror():
    res = _error_result(RuntimeError("boom"), "query_data")
    assert res["isError"] is True
