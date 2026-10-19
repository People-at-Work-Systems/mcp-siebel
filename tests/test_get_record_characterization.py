"""Characterization tests for the 2 record tools being migrated to the
``bc_read`` kind: get_record, get_record_with_children.

These pin the current output (prose), the get_record fallback chain (direct
read -> describe BC -> curated preferred fields / ranked fallback -> scoped
query), and the error paths (fallback failure, BO not found, unauthenticated
session) by mocking at the SiebelClient / SchemaCache level
(``session.client.get_record`` / ``describe_bc`` / ``query`` /
``get_record_with_children`` and ``cache.resolve_business_object``) and driving
each tool through ``ToolRegistry.call`` — so they are agnostic to the tool's
``kind`` and pass unchanged once the tools move from kind "python" to the
``bc_read`` executor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import (
    SiebelBCDescriptor,
    SiebelError,
    SiebelFieldDescriptor,
    SiebelQueryResult,
)
from mcp_siebel.tools import SessionContext, ToolRegistry

_MASKED = "[INTERNAL - do not display verbatim] The Siebel operation could not be completed."

# A 500 that get_record rethrows (not a 404) so the scoped-query fallback runs.
# Mirrors the SBL-DAT-00398 misconfiguration observed on the demo instance.
_BROKEN_FIELD_ERROR = SiebelError(
    "Siebel API returned 500",
    500,
    "{\"ERROR\":\"Method 'FieldValue' of business component 'Contact' returned the "
    "following error:\\\"Field 'Primary Address Integration Id' does not exist in "
    "definition for business component 'Contact'.(SBL-DAT-00398)\\\"(SBL-EAI-04376)\"}",
)


def _client() -> AsyncMock:
    return AsyncMock()


def _session(client: AsyncMock | None, *, resolve: str | None = "Account") -> SessionContext:
    if client is None:
        # Unauthenticated: no client, no cache.
        return SessionContext(session_id="s1", username="tester")
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=resolve)
    return SessionContext(session_id="s1", username="tester", client=client, cache=cache)


def _descriptor(*names: str, types: dict[str, str] | None = None, bc: str = "Contact") -> SiebelBCDescriptor:
    types = types or {}
    return SiebelBCDescriptor(
        bc,
        bc,
        [SiebelFieldDescriptor(name=n, type=types.get(n, "DTYPE_TEXT")) for n in names],
    )


def _query_result(record: dict[str, str]) -> SiebelQueryResult:
    return SiebelQueryResult(
        items=[record], total_count=1, has_more=False, next_start_row=None, offset=0, page_size=1
    )


# --- get_record: direct read success ---------------------------------------


@pytest.mark.asyncio
async def test_get_record_success_without_fields() -> None:
    client = _client()
    client.get_record = AsyncMock(return_value={"Name": "Acme", "Id": "1-ABC"})
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("get_record", {"business_object": "Account", "id": "1-ABC"}, _session(client))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Account/Account record 1-ABC:\n  Name: Acme\n  Id: 1-ABC"
    # The direct read asks for the whole record (no field list); no fallback query.
    assert client.get_record.call_args.args == ("Account", "Account", "1-ABC", None)
    client.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_record_success_with_fields() -> None:
    client = _client()
    client.get_record = AsyncMock(return_value={"Name": "Acme", "Id": "1-ABC"})
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "get_record",
        {"business_object": "Account", "id": "1-ABC", "fields": ["Name", "Id"]},
        _session(client),
    )
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Account/Account record 1-ABC:\n  Name: Acme\n  Id: 1-ABC"
    # The caller's field list is forwarded verbatim; no fallback query.
    assert client.get_record.call_args.args == ("Account", "Account", "1-ABC", ["Name", "Id"])
    client.query.assert_not_awaited()


# --- get_record: SiebelError -> scoped-query fallback ----------------------


@pytest.mark.asyncio
async def test_get_record_fallback_uses_curated_preferred_fields_for_known_bc() -> None:
    """A known BC (Contact) falls back to the curated preferred-field list
    (preferred ∩ available, in preferred order, with Id appended) — not the
    ranked heuristic."""
    client = _client()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock(
        return_value=_descriptor(
            "Accomplishments", "Last Name", "First Name", "Email Address", "Primary City", "Id", "Surprise"
        )
    )
    client.query = AsyncMock(return_value=_query_result({"Last Name": "Taylor", "Surprise": "extra"}))
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "get_record", {"business_object": "Contact", "id": "7SIA-5TYFM"}, _session(client, resolve="Contact")
    )
    assert resp.get("isError") is not True
    # Curated order (preferred ∩ available) + Id; "Accomplishments"/"Surprise" are not preferred.
    assert client.query.call_args.kwargs["fields"] == [
        "Last Name",
        "First Name",
        "Email Address",
        "Primary City",
        "Id",
    ]
    assert client.query.call_args.kwargs["search_spec"] == "[Id]='7SIA-5TYFM'"
    assert client.query.call_args.kwargs["page_size"] == 1
    assert "Last Name: Taylor" in resp["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_record_fallback_ranks_for_unknown_bc() -> None:
    """An unknown BC (no curated list) falls back to the ranked heuristic —
    identifying fields beat alphabetical filler."""
    client = _client()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock(
        return_value=_descriptor("Accomplishments", "Widget Name", "Id", bc="Widget")
    )
    client.query = AsyncMock(return_value=_query_result({"Widget Name": "W1"}))
    reg = ToolRegistry(AppSettings())
    await reg.call("get_record", {"business_object": "Widget", "id": "1"}, _session(client, resolve="Widget"))
    requested = list(client.query.call_args.kwargs["fields"])
    # "Widget Name" (identifying) is kept; "Accomplishments" (filler) is dropped
    # once the cap bites — here all three are under the cap, but the ranked path
    # is what ran (curated would have been empty / produced nothing).
    assert "Widget Name" in requested
    assert "Id" in requested


@pytest.mark.asyncio
async def test_get_record_fallback_respects_the_max_fields_cap() -> None:
    """The ranked fallback never asks for more than _MAX_FALLBACK_FIELDS (40)."""
    client = _client()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    fields = ["Id", *(f"Field {i:02d}" for i in range(44))]  # 45 described fields
    client.describe_bc = AsyncMock(return_value=_descriptor(*fields, bc="Widget"))
    client.query = AsyncMock(return_value=_query_result({"Id": "1"}))
    reg = ToolRegistry(AppSettings())
    await reg.call("get_record", {"business_object": "Widget", "id": "1"}, _session(client, resolve="Widget"))
    assert len(client.query.call_args.kwargs["fields"]) == 40


@pytest.mark.asyncio
async def test_get_record_fallback_demotes_bool_and_id_fields() -> None:
    """A DTYPE_BOOL field whose name contains a contact hint is demoted by its
    type, so it drops out at the cap instead of being promoted by the hint."""
    client = _client()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    # 41 fields: one identifying + 39 filler + one bool whose name contains "email".
    names = ["Last Name", *(f"Filler {i:02d}" for i in range(39)), "Email SR Updates Flag"]
    types = {"Email SR Updates Flag": "DTYPE_BOOL"}
    client.describe_bc = AsyncMock(return_value=_descriptor(*names, types=types, bc="Widget"))
    client.query = AsyncMock(return_value=_query_result({"Last Name": "Taylor"}))
    reg = ToolRegistry(AppSettings())
    await reg.call("get_record", {"business_object": "Widget", "id": "1"}, _session(client, resolve="Widget"))
    requested = list(client.query.call_args.kwargs["fields"])
    assert "Last Name" in requested
    # Demoted to the bottom tier, the bool is the one field the cap drops.
    assert "Email SR Updates Flag" not in requested
    assert len(requested) == 40


@pytest.mark.asyncio
async def test_get_record_fallback_reuses_caller_supplied_fields() -> None:
    """When the caller named its fields, the fallback reuses them and does not
    describe the BC again."""
    client = _client()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock()
    client.query = AsyncMock(return_value=_query_result({"Last Name": "Taylor"}))
    reg = ToolRegistry(AppSettings())
    await reg.call(
        "get_record",
        {"business_object": "Contact", "id": "7SIA-5TYFM", "fields": ["Last Name"]},
        _session(client, resolve="Contact"),
    )
    client.describe_bc.assert_not_awaited()
    assert client.query.call_args.kwargs["fields"] == ["Last Name"]


@pytest.mark.asyncio
async def test_get_record_fallback_failure_surfaces_masked_error() -> None:
    """When the scoped-query fallback also fails, the Siebel error propagates
    out of the handler and the registry masks it for the caller."""
    client = _client()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock(side_effect=SiebelError("nope", 500, "{}"))
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "get_record", {"business_object": "Contact", "id": "7SIA-5TYFM"}, _session(client, resolve="Contact")
    )
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


# --- get_record: BO not found / unauthenticated ---------------------------


@pytest.mark.asyncio
async def test_get_record_bo_not_found() -> None:
    client = _client()
    client.get_record = AsyncMock()
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("get_record", {"business_object": "Nope", "id": "1"}, _session(client, resolve=None))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.get_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_record_unauthenticated_returns_error() -> None:
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("get_record", {"business_object": "Account", "id": "1"}, _session(None))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


# --- get_record_with_children ---------------------------------------------


@pytest.mark.asyncio
async def test_get_record_with_children_success_prose() -> None:
    client = _client()
    # The client prefixes __childrenAvailable; the handler's hierarchical
    # formatter strips __-prefixed keys, so the prose shows only the record.
    client.get_record_with_children = AsyncMock(
        return_value={"__childrenAvailable": True, "Name": "Acme", "Id": "1-ABC"}
    )
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "get_record_with_children", {"business_object": "Account", "id": "1-ABC"}, _session(client)
    )
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Account/Account record 1-ABC:\n  Name: Acme\n  Id: 1-ABC"
    assert client.get_record_with_children.call_args.args == ("Account", "Account", "1-ABC")


@pytest.mark.asyncio
async def test_get_record_with_children_siebel_error_is_masked() -> None:
    client = _client()
    # A 500 without SBL-EAI-04392 propagates (no flat-record fallback).
    client.get_record_with_children = AsyncMock(side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "get_record_with_children", {"business_object": "Account", "id": "1-ABC"}, _session(client)
    )
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_get_record_with_children_bo_not_found() -> None:
    client = _client()
    client.get_record_with_children = AsyncMock()
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "get_record_with_children", {"business_object": "Nope", "id": "1"}, _session(client, resolve=None)
    )
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.get_record_with_children.assert_not_awaited()
