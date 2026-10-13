"""create_record_json — the JSON-returning twin of create_record.

A caller may need the new record's Id programmatically. create_record returns
_text(...) (prose for LLM consumption); create_record_json returns
_json_text(result) so the caller can parse the created record straight out of
the MCP text content. Mirrors the query_records_json / query_data split.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import SessionContext, ToolRegistry


def test_create_record_json_is_registered_with_typed_schema():
    reg = ToolRegistry(AppSettings(allow_write=True))
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
async def test_create_record_json_returns_json_with_new_id():
    reg = ToolRegistry(AppSettings(allow_write=True))
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
async def test_create_record_json_rejects_empty_data():
    reg = ToolRegistry(AppSettings(allow_write=True))
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
