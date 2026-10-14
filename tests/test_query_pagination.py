"""Regression tests for the query_data/query_child_records pagination bug.

MCP tool schemas type every argument as JSON string, so the LLM always sends
`offset`/`page_size` as strings, e.g. {"offset": "100"}. `page_size` was
correctly cast with int(...), but `offset` was passed straight through to
SiebelClient.query()/query_child_records() uncast. That string offset flowed
into the result dict and blew up with
`TypeError: can only concatenate str (not "int") to str` when numbering the
returned records — crashing every paginated query_data/query_child_records call
with a non-default offset.

These now drive the tools through ``ToolRegistry.call`` (the tools are kind
``bc_query``), so they cover the executor path too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelQueryResult
from mcp_siebel.tools import SessionContext, ToolRegistry


def _make_registry_and_session(query_result: SiebelQueryResult) -> tuple[ToolRegistry, SessionContext, AsyncMock]:
    query_mock = AsyncMock(return_value=query_result)
    client = AsyncMock()
    client.query = query_mock
    client.query_child_records = query_mock

    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")

    session = SessionContext(session_id="s1", username="tester", client=client, cache=cache)
    ctx = ToolRegistry(AppSettings())
    return ctx, session, query_mock


@pytest.mark.asyncio
async def test_query_data_with_string_offset_does_not_crash():
    result = SiebelQueryResult(
        items=[{"Id": "1", "Name": "Acme"}, {"Id": "2", "Name": "Beta"}],
        total_count=102,
        has_more=True,
        offset=100,
        page_size=2,
        next_start_row=102,
    )
    reg, session, query_mock = _make_registry_and_session(result)

    # This is exactly the shape the LLM sends — every field is a JSON string,
    # including offset/page_size, per the tool's declared input_schema.
    args = {
        "business_object": "Account",
        "business_component": "Account",
        "page_size": "2",
        "offset": "100",
    }

    response = await reg.call("query_data", args, session)

    assert response.get("isError") is not True
    text = response["content"][0]["text"]
    assert "Record 101:" in text
    assert "Record 102:" in text

    # The client must receive real ints, not the raw JSON strings.
    _, kwargs = query_mock.call_args
    assert kwargs["offset"] == 100
    assert isinstance(kwargs["offset"], int)
    assert kwargs["page_size"] == 2
    assert isinstance(kwargs["page_size"], int)


@pytest.mark.asyncio
async def test_query_data_with_missing_offset_defaults_to_zero():
    result = SiebelQueryResult(
        items=[{"Id": "1", "Name": "Acme"}],
        total_count=1,
        has_more=False,
        offset=0,
        page_size=20,
    )
    reg, session, query_mock = _make_registry_and_session(result)

    args = {"business_object": "Account", "business_component": "Account"}
    response = await reg.call("query_data", args, session)

    assert "Record 1:" in response["content"][0]["text"]
    _, kwargs = query_mock.call_args
    assert kwargs["offset"] == 0


@pytest.mark.asyncio
async def test_query_child_records_with_string_offset_does_not_crash():
    result = SiebelQueryResult(
        items=[{"Id": "1", "Name": "Contact A"}],
        total_count=51,
        has_more=True,
        offset=50,
        page_size=1,
        next_start_row=51,
    )
    reg, session, query_mock = _make_registry_and_session(result)

    args = {
        "business_object": "Account",
        "business_component": "Account",
        "parent_id": "1-ABC",
        "child_business_component": "Contact",
        "page_size": "1",
        "offset": "50",
    }

    response = await reg.call("query_child_records", args, session)

    text = response["content"][0]["text"]
    assert "Record 51:" in text
    _, kwargs = query_mock.call_args
    assert kwargs["offset"] == 50
