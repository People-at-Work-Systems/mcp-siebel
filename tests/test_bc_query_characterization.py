"""Characterization tests for the 5 query tools being migrated to bc_query.

These pin the current output (prose + JSON) by mocking at the SiebelClient
level (session.client.query / query_child_records) and driving each tool through
``ToolRegistry.call`` — so they are agnostic to the tool's ``kind`` and pass
unchanged once the tools move from kind "python" to the "bc_query" executor.

Covered: normal result, empty result, the field-error -> retry-with-["Id"] path,
the placeholder guard, the 404 -> empty path, count_records reading totalCount
with page_size=1, and the unauthenticated-session error.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError, SiebelQueryResult
from mcp_siebel.tools import SessionContext, ToolRegistry

_FIELD_ERROR_BODY = '{"ERROR":"...SBL-EAI-50258 Invalid fields..."}'


def _client(
    query_result: SiebelQueryResult | None = None,
    *,
    child_result: SiebelQueryResult | None = None,
) -> AsyncMock:
    client = AsyncMock()
    if query_result is not None:
        client.query = AsyncMock(return_value=query_result)
    if child_result is not None:
        client.query_child_records = AsyncMock(return_value=child_result)
    return client


def _reg_and_session(client: AsyncMock | None, **settings_overrides) -> tuple[ToolRegistry, SessionContext]:
    settings = AppSettings(**settings_overrides)
    if client is None:
        # Unauthenticated session: no client, no cache.
        session = SessionContext(session_id="s1", username="tester")
        return ToolRegistry(settings), session
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    session = SessionContext(session_id="s1", username="tester", client=client, cache=cache)
    return ToolRegistry(settings), session


def _two_record_result() -> SiebelQueryResult:
    return SiebelQueryResult(
        items=[{"Id": "1", "Name": "Acme"}, {"Id": "2", "Name": "Beta"}],
        total_count=2,
        has_more=False,
        offset=0,
        page_size=20,
    )


# --- query_data (prose) -----------------------------------------------------


@pytest.mark.asyncio
async def test_query_data_normal_prose() -> None:
    reg, session = _reg_and_session(_client(_two_record_result()))
    resp = await reg.call("query_data", {"business_object": "Account", "business_component": "Account"}, session)
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == (
        "Total: 2 | Page size: 20 | Has more: NO (end of results)\n\n"
        "Record 1:\n  Id: 1\n  Name: Acme\n\n"
        "Record 2:\n  Id: 2\n  Name: Beta"
    )


@pytest.mark.asyncio
async def test_query_data_empty_prose() -> None:
    reg, session = _reg_and_session(
        _client(SiebelQueryResult(items=[], total_count=0, has_more=False, offset=0, page_size=20))
    )
    resp = await reg.call("query_data", {"business_object": "Account"}, session)
    assert resp["content"][0]["text"] == (
        "Total: 0 | Page size: 20 | Has more: NO (end of results)\n\n(no records found)"
    )


@pytest.mark.asyncio
async def test_query_data_has_more_points_at_next_start_row() -> None:
    reg, session = _reg_and_session(
        _client(
            SiebelQueryResult(
                items=[{"Id": "1", "Name": "Acme"}, {"Id": "2", "Name": "Beta"}],
                total_count=102,
                has_more=True,
                offset=100,
                page_size=2,
                next_start_row=102,
            )
        )
    )
    resp = await reg.call(
        "query_data",
        {"business_object": "Account", "business_component": "Account", "page_size": "2", "offset": "100"},
        session,
    )
    assert resp["content"][0]["text"] == (
        "Total: 102 | Page size: 2 | Has more: YES - call again with offset=102\n\n"
        "Record 101:\n  Id: 1\n  Name: Acme\n\n"
        "Record 102:\n  Id: 2\n  Name: Beta"
    )


@pytest.mark.asyncio
async def test_query_data_field_error_retries_with_id() -> None:
    """A 50258 field error with fields present retries with fields=['Id']."""
    retried = SiebelQueryResult(items=[{"Id": "1-5GZE"}], total_count=1, has_more=False, offset=0, page_size=20)
    client = AsyncMock()
    client.query = AsyncMock(side_effect=[SiebelError("bad field", 500, _FIELD_ERROR_BODY), retried])
    reg, session = _reg_and_session(client)
    resp = await reg.call(
        "query_data",
        {"business_object": "Account", "business_component": "Account", "fields": "NonExistentField"},
        session,
    )
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == (
        "Total: 1 | Page size: 20 | Has more: NO (end of results)\n\nRecord 1:\n  Id: 1-5GZE"
    )
    assert client.query.call_args_list[1].kwargs["fields"] == ["Id"]


@pytest.mark.asyncio
async def test_query_data_placeholder_guard_does_not_call_siebel() -> None:
    client = AsyncMock()
    client.query = AsyncMock(return_value=_two_record_result())
    reg, session = _reg_and_session(client)
    resp = await reg.call(
        "query_data", {"business_object": "Account", "business_component": "Account", "fields": "<?>"}, session
    )
    assert resp.get("isError") is True
    assert "placeholder" in resp["content"][0]["text"].lower()
    client.query.assert_not_called()


@pytest.mark.asyncio
async def test_query_data_404_returns_empty() -> None:
    client = AsyncMock()
    client.query = AsyncMock(side_effect=SiebelError("not found", 404, '{"ERROR":"404"}'))
    reg, session = _reg_and_session(client)
    resp = await reg.call("query_data", {"business_object": "Account"}, session)
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == (
        "Total: 0 | Page size: 20 | Has more: NO (end of results)\n\n(no records found)"
    )


# --- query_records_json -----------------------------------------------------


@pytest.mark.asyncio
async def test_query_records_json_normal() -> None:
    reg, session = _reg_and_session(_client(_two_record_result()))
    resp = await reg.call("query_records_json", {"business_object": "Account"}, session)
    assert resp.get("isError") is not True
    payload = json.loads(resp["content"][0]["text"])
    assert payload == {
        "items": [{"Id": "1", "Name": "Acme"}, {"Id": "2", "Name": "Beta"}],
        "totalCount": 2,
        "hasMore": False,
        "nextStartRow": None,
        "offset": 0,
        "pageSize": 20,
    }


# --- count_records ----------------------------------------------------------


@pytest.mark.asyncio
async def test_count_records_reads_total_count_with_page_size_one() -> None:
    client = AsyncMock()
    client.query = AsyncMock(
        return_value=SiebelQueryResult(items=[{"Id": "1"}], total_count=42, has_more=False, offset=0, page_size=1)
    )
    reg, session = _reg_and_session(client)
    resp = await reg.call("count_records", {"business_object": "Account"}, session)
    assert resp["content"][0]["text"] == "Total records: **42**"
    # count_records forces page_size=1 regardless of any supplied page_size.
    assert client.query.call_args.kwargs["page_size"] == 1


@pytest.mark.asyncio
async def test_count_records_no_total_count() -> None:
    reg, session = _reg_and_session(
        _client(SiebelQueryResult(items=[{"Id": "1"}], total_count=None, has_more=False, offset=0, page_size=1))
    )
    resp = await reg.call("count_records", {"business_object": "Account"}, session)
    assert resp["content"][0]["text"] == "Count not available."


# --- query_child_records (prose) -------------------------------------------


@pytest.mark.asyncio
async def test_query_child_records_normal_prose() -> None:
    reg, session = _reg_and_session(
        _client(
            child_result=SiebelQueryResult(
                items=[{"Id": "1", "Name": "Contact A"}], total_count=1, has_more=False, offset=0, page_size=20
            )
        )
    )
    resp = await reg.call(
        "query_child_records",
        {
            "business_object": "Account",
            "business_component": "Account",
            "parent_id": "1-ABC",
            "child_business_component": "Contact",
        },
        session,
    )
    assert resp["content"][0]["text"] == "Record 1:\n  Id: 1\n  Name: Contact A"


@pytest.mark.asyncio
async def test_query_child_records_empty_prose() -> None:
    reg, session = _reg_and_session(
        _client(child_result=SiebelQueryResult(items=[], total_count=0, has_more=False, offset=0, page_size=20))
    )
    resp = await reg.call(
        "query_child_records",
        {"business_object": "Account", "parent_id": "1-ABC", "child_business_component": "Contact"},
        session,
    )
    assert resp["content"][0]["text"] == "(no child records found)"


@pytest.mark.asyncio
async def test_query_child_records_404_returns_empty() -> None:
    client = AsyncMock()
    client.query_child_records = AsyncMock(side_effect=SiebelError("not found", 404, '{"ERROR":"404"}'))
    reg, session = _reg_and_session(client)
    resp = await reg.call(
        "query_child_records",
        {"business_object": "Account", "parent_id": "1-ABC", "child_business_component": "Contact"},
        session,
    )
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "(no child records found)"


@pytest.mark.asyncio
async def test_query_child_records_placeholder_guard() -> None:
    client = AsyncMock()
    client.query_child_records = AsyncMock(
        return_value=SiebelQueryResult(items=[], total_count=0, has_more=False, offset=0, page_size=20)
    )
    reg, session = _reg_and_session(client)
    resp = await reg.call(
        "query_child_records",
        {"business_object": "Account", "parent_id": "1-ABC", "child_business_component": "Contact", "fields": "<?>"},
        session,
    )
    assert resp.get("isError") is True
    assert "placeholder" in resp["content"][0]["text"].lower()
    client.query_child_records.assert_not_called()


# --- query_child_records_json ----------------------------------------------


@pytest.mark.asyncio
async def test_query_child_records_json_normal() -> None:
    reg, session = _reg_and_session(
        _client(
            child_result=SiebelQueryResult(
                items=[{"Id": "1", "Name": "Contact A"}], total_count=1, has_more=False, offset=0, page_size=20
            )
        )
    )
    resp = await reg.call(
        "query_child_records_json",
        {"business_object": "Account", "parent_id": "1-ABC", "child_business_component": "Contact"},
        session,
    )
    payload = json.loads(resp["content"][0]["text"])
    assert payload["items"] == [{"Id": "1", "Name": "Contact A"}]
    assert payload["totalCount"] == 1


# --- unauthenticated session ------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_session_returns_error() -> None:
    """No client/cache on the session -> a clear auth error, no Siebel call."""
    reg, session = _reg_and_session(None)
    resp = await reg.call("query_data", {"business_object": "Account"}, session)
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."
