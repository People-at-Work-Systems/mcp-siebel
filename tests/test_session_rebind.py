"""Finding 7: the session-establishment tool can't rebind an existing session to
a different identity (session hijack via ``authenticate``).

Fix at the transport layer: both transports always create a FRESH session for the
session tool, ignoring any caller-supplied session_id for that call. The caller
gets the new session_id back; the old id keeps working for its original owner.
Belt-and-braces in ``_authenticate``: an already-authenticated session is refused
rather than rebound.
"""

from __future__ import annotations

import json
from importlib.resources import files
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import SessionContext, ToolRegistry


def _spec_with_session_renamed(tmp_path, new_name: str) -> str:
    spec = json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))
    tools = spec["tools"]
    tools[new_name] = tools.pop("authenticate")
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return str(path)


def _demo_registry(tmp_path, new_name: str = "login") -> ToolRegistry:
    settings = AppSettings(demo_enabled=True)
    settings.tools_spec_path = _spec_with_session_renamed(tmp_path, new_name)
    return ToolRegistry(settings)


def _session_id(result: object) -> str:
    for block in getattr(result, "content", []):  # type: ignore[arg-type]
        text = getattr(block, "text", "")
        if "session_id:" in text:
            return text.split("session_id:", 1)[1].strip()
    raise AssertionError(f"no session_id in result: {result}")


@pytest.mark.asyncio
async def test_authenticate_always_creates_new_session_no_rebind(tmp_path, monkeypatch) -> None:
    """Calling the session tool again (with A's session_id and B's creds) creates
    a NEW session for B; A's session is not rebound to B."""
    import mcp_siebel.stdio as stdio

    reg = _demo_registry(tmp_path, "login")
    monkeypatch.setattr(stdio, "registry", reg)

    # Establish session for user A.
    result_a = await stdio._call_tool(
        SimpleNamespace(), SimpleNamespace(name="login", arguments={"username": "A", "password": "x"})
    )
    sid_a = _session_id(result_a)
    session_a = stdio.sessions.get(sid_a)
    assert session_a is not None and session_a.username == "A"

    # Call login again, WITH A's session_id and B's creds.
    result_b = await stdio._call_tool(
        SimpleNamespace(),
        SimpleNamespace(name="login", arguments={"username": "B", "password": "y", "session_id": sid_a}),
    )
    sid_b = _session_id(result_b)

    # A NEW session is returned — not sid_a.
    assert sid_b != sid_a
    # A's original session still resolves to A (not rebound to B).
    session_a_after = stdio.sessions.get(sid_a)
    assert session_a_after is not None
    assert session_a_after.username == "A"
    # B's new session is B.
    session_b = stdio.sessions.get(sid_b)
    assert session_b is not None
    assert session_b.username == "B"


@pytest.mark.asyncio
async def test_authenticate_refuses_already_authenticated_session(tmp_path) -> None:
    """Belt-and-braces: calling authenticate on a session that already has a
    client is refused (no rebind), even outside the transport always-create path."""
    reg = ToolRegistry(AppSettings())
    existing = SessionContext(
        session_id="s1", username="A", client=AsyncMock(), cache=AsyncMock()
    )
    result = await reg.call("authenticate", {"username": "B", "password": "y"}, existing)
    assert result.get("isError") is True
    assert "already authenticated" in result["content"][0]["text"].lower()  # type: ignore[index]
    # The existing session was not mutated.
    assert existing.username == "A"
