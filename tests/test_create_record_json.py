"""create_record_json — the JSON-returning twin of create_record.

A caller may need the new record's Id programmatically. create_record returns
_text(...) (prose for LLM consumption); create_record_json returns
_json_text(result) so the caller can parse the created record straight out of
the MCP text content. Mirrors the query_records_json / query_data split.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import SessionContext, ToolRegistry


def _registry_with_create_record_json_enabled(tmp_path) -> ToolRegistry:
    """Bundled spec with create_record_json flipped to enabled: true. The
    bundled spec disables it by default (read-only); flipping enabled: true in
    an override spec is how a tool is turned on — no env var."""
    spec = deepcopy(json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8")))
    tools = spec["tools"]  # type: ignore[index]
    tools["create_record_json"] = {**tools["create_record_json"], "enabled": True}  # type: ignore[index]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(path)
    return ToolRegistry(settings)


def test_create_record_json_is_registered_with_typed_schema(tmp_path):
    reg = _registry_with_create_record_json_enabled(tmp_path)
    tool = reg._tools.get("create_record_json")
    assert tool is not None, "create_record_json tool must be registered"
    # The CLAUDE.md invariant: properties must declare a concrete JSON Schema
    # type (string), not empty objects — Claude Desktop silently rejects {}.
    props = tool.input_schema["properties"]
    assert set(props) == {"business_object", "business_component", "data"}
    for name, prop in props.items():
        assert prop.get("type") in ("string", "object"), f"{name} must be typed"
    assert "business_object" in tool.input_schema.get("required", [])
    assert "data" in tool.input_schema.get("required", [])


@pytest.mark.asyncio
async def test_create_record_json_returns_json_with_new_id(tmp_path):
    reg = _registry_with_create_record_json_enabled(tmp_path)
    session = SessionContext(session_id="s1")
    session.client = AsyncMock()
    # Siebel's PUT response is the created record, including its new Id.
    session.client.create_record = AsyncMock(return_value={"Id": "1-ABC", "Name": "Acme"})
    session.cache = AsyncMock()
    session.cache.resolve_business_object = AsyncMock(return_value="Account")

    result = await reg.call(
        "create_record_json",
        {"business_object": "Account", "data": {"Name": "Acme"}},
        session,
    )

    assert result.get("isError") is None
    text = result["content"][0]["text"]
    parsed = json.loads(text)
    assert parsed["Id"] == "1-ABC"
    assert parsed["Name"] == "Acme"
    # The client was called with the resolved BO/BC and the data payload.
    session.client.create_record.assert_awaited_once()
    call_args = session.client.create_record.call_args
    assert call_args.args[0] == "Account"
    assert call_args.args[1] == "Account"
    assert call_args.args[2] == {"Name": "Acme"}


@pytest.mark.asyncio
async def test_create_record_json_rejects_empty_data(tmp_path):
    reg = _registry_with_create_record_json_enabled(tmp_path)
    session = SessionContext(session_id="s1")
    session.client = AsyncMock()
    session.cache = AsyncMock()

    result = await reg.call(
        "create_record_json",
        {"business_object": "Account", "data": {}},
        session,
    )
    assert result.get("isError") is True
    session.client.create_record.assert_not_called()
