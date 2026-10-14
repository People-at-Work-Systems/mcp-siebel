"""The session-establishment tool is spec-driven.

The spec marks exactly one entry with ``establishes_session: true``; the
transports (http + stdio) read its name from ``registry.session_tool_name``
instead of hard-coding "authenticate". Renaming the session tool in a fixture
spec must keep session creation and session_id injection working in both
transports.
"""

from __future__ import annotations

import json
from importlib.resources import files
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import ToolRegistry


def _spec_with_session_renamed(tmp_path, new_name: str) -> str:
    """The bundled spec with the session tool renamed to ``new_name`` (its
    handler, establishes_session, always_enabled and access are preserved)."""
    spec = json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))
    tools = spec["tools"]
    tools[new_name] = tools.pop("authenticate")
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return str(path)


def _demo_registry(tmp_path, new_name: str = "login") -> ToolRegistry:
    """A registry whose session tool is renamed and whose settings enable demo
    mode (so authenticate succeeds without any Siebel I/O)."""
    settings = AppSettings(demo_enabled=True)
    # tools_spec_path has a pydantic alias, so set it post-construction.
    settings.tools_spec_path = _spec_with_session_renamed(tmp_path, new_name)
    return ToolRegistry(settings)


def test_renamed_session_tool_creates_session_and_injects_id_http(tmp_path, monkeypatch) -> None:
    import mcp_siebel.http_server as http_server

    reg = _demo_registry(tmp_path, "login")
    assert reg.session_tool_name == "login"
    monkeypatch.setattr(http_server, "registry", reg)

    client = TestClient(http_server.app)
    resp = client.post("/tools/login", json={"arguments": {"username": "MSTERN", "password": "x"}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "session_id" in body, body
    # The session was actually created server-side.
    assert http_server.sessions.get(body["session_id"]) is not None


@pytest.mark.asyncio
async def test_renamed_session_tool_creates_session_and_injects_id_stdio(tmp_path, monkeypatch) -> None:
    import mcp_siebel.stdio as stdio

    reg = _demo_registry(tmp_path, "login")
    assert reg.session_tool_name == "login"
    monkeypatch.setattr(stdio, "registry", reg)

    params = SimpleNamespace(name="login", arguments={"username": "MSTERN", "password": "x"})
    result = await stdio._call_tool(SimpleNamespace(), params)  # type: ignore[arg-type]
    texts = [c.text for c in result.content if hasattr(c, "text")]
    assert any("session_id:" in t for t in texts), texts


@pytest.mark.asyncio
async def test_renamed_session_tool_skips_session_id_prop_on_non_session_tools_stdio(tmp_path, monkeypatch) -> None:
    """Only the session tool is exempt from the session_id schema injection —
    every other served tool gets the session_id property."""
    import mcp_siebel.stdio as stdio

    reg = _demo_registry(tmp_path, "login")
    monkeypatch.setattr(stdio, "registry", reg)

    params = SimpleNamespace(name="login", arguments={"username": "MSTERN", "password": "x"})
    await stdio._call_tool(SimpleNamespace(), params)  # type: ignore[arg-type]  # establish a session

    tools = {t.name: t for t in (await stdio._list_tools(SimpleNamespace(), None)).tools}
    assert "session_id" not in tools["login"].input_schema["properties"]
    # a non-session tool still carries the injected session_id property
    assert "session_id" in tools["query_data"].input_schema["properties"]
