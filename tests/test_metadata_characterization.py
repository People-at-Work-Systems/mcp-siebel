"""Characterization tests for the 7 metadata tools being migrated to the
``bc_describe`` / ``static_text`` kinds: list_business_objects,
list_business_objects_json, describe_business_object,
describe_business_object_json, describe_repository, describe_repository_json,
get_query_syntax.

These pin the current output (prose + JSON) and error paths by mocking at the
SiebelClient / SchemaCache level (cache.resolve_business_object /
get_descriptor / get_business_objects / field_summary, and
client.describe_repository_bc) and driving each tool through
``ToolRegistry.call`` — so they are agnostic to the tool's ``kind`` and pass
unchanged once the tools move from kind "python" to the new executors.

One test — ``test_describe_business_object_json_50257_no_fallback_current`` —
pins the CURRENT behavior of describe_business_object_json on SBL-EAI-50257
(today: no fallback, masked error). Task C intentionally adds the child-BC
fallback to that tool and UPDATES this test to the new behavior in the same
commit.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.schema_cache import SchemaCache
from mcp_siebel.siebel_client import SiebelBCDescriptor, SiebelError, SiebelFieldDescriptor
from mcp_siebel.tools import SessionContext, ToolRegistry

_MASKED = "[INTERNAL - do not display verbatim] The Siebel operation could not be completed."
_BO_NOT_FOUND = 'Business Object "{bo}" not found. Use list_business_objects to see available options.'


def _cache(
    *,
    business_objects: list[str] | None = None,
    resolve: str | None | object = "Account",
    descriptor: SiebelBCDescriptor | None = None,
    descriptor_side_effect: object | None = None,
) -> AsyncMock:
    """A SchemaCache-level mock. ``resolve`` is the return value of
    resolve_business_object; pass a side_effect via ``descriptor_side_effect`` for
    the fallback path. field_summary is the real static method so the prose
    output is the real format (the bc_describe renderer must match it)."""
    cache = AsyncMock()
    default_bos = business_objects if business_objects is not None else ["Account"]
    cache.get_business_objects = AsyncMock(return_value=default_bos)
    if isinstance(resolve, dict):
        cache.resolve_business_object = AsyncMock(side_effect=lambda name: resolve.get(name))
    else:
        cache.resolve_business_object = AsyncMock(return_value=resolve)
    if descriptor_side_effect is not None:
        cache.get_descriptor = AsyncMock(side_effect=descriptor_side_effect)
    else:
        cache.get_descriptor = AsyncMock(return_value=descriptor)
    cache.field_summary = SchemaCache.field_summary
    return cache


def _client(*, repo_fields: list | None = None, repo_side_effect: object | None = None) -> AsyncMock:
    client = AsyncMock()
    if repo_side_effect is not None:
        client.describe_repository_bc = AsyncMock(side_effect=repo_side_effect)
    else:
        client.describe_repository_bc = AsyncMock(return_value=repo_fields or [])
    return client


def _auth_session(cache: AsyncMock, client: AsyncMock | None = None) -> SessionContext:
    return SessionContext(session_id="s1", username="t", client=client or _client(), cache=cache)


def _unauth_session() -> SessionContext:
    return SessionContext(session_id="s1", username="t")


# --- list_business_objects (prose) ------------------------------------------


@pytest.mark.asyncio
async def test_list_business_objects_prose() -> None:
    cache = _cache(business_objects=["Account", "Contact"])
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("list_business_objects", {}, _auth_session(cache))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Available Business Objects (2):\n  - Account\n  - Contact"


@pytest.mark.asyncio
async def test_list_business_objects_json() -> None:
    cache = _cache(business_objects=["Account", "Contact"])
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("list_business_objects_json", {}, _auth_session(cache))
    assert json.loads(resp["content"][0]["text"]) == {"business_objects": ["Account", "Contact"]}


@pytest.mark.asyncio
async def test_list_business_objects_unauthenticated() -> None:
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("list_business_objects", {}, _unauth_session())
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


# --- describe_business_object (prose) --------------------------------------


@pytest.mark.asyncio
async def test_describe_business_object_prose() -> None:
    descriptor = SiebelBCDescriptor(
        "Account",
        "Account",
        [SiebelFieldDescriptor("Name", "Text"), SiebelFieldDescriptor("Id", "Text", required=True)],
    )
    cache = _cache(descriptor=descriptor)
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_business_object", {"business_object": "Account"}, _auth_session(cache))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == (
        "Account / Account\n\n  - Name: Text\n  - Id: Text (required)\n\nTotal fields: 2"
    )


@pytest.mark.asyncio
async def test_describe_business_object_bo_not_found() -> None:
    cache = _cache(resolve=None)
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_business_object", {"business_object": "Nope"}, _auth_session(cache))
    assert resp.get("isError") is not True  # prose variant: _text, not _error
    assert resp["content"][0]["text"] == _BO_NOT_FOUND.format(bo="Nope")


@pytest.mark.asyncio
async def test_describe_business_object_siebel_error_is_masked() -> None:
    cache = _cache(descriptor_side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_business_object", {"business_object": "Account"}, _auth_session(cache))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_describe_business_object_50257_child_bc_fallback() -> None:
    """On SBL-EAI-50257 (the BC isn't a child of the resolved BO), re-resolve the
    BC's own BO and retry — the header reflects the BC's own BO."""
    own = SiebelBCDescriptor("Contact", "Contact", [SiebelFieldDescriptor("Last Name", "Text")])
    cache = _cache(
        resolve={"Account": "Account", "Contact": "Contact"},
        descriptor_side_effect=[
            SiebelError("not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}'),
            own,
        ],
    )
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "describe_business_object",
        {"business_object": "Account", "business_component": "Contact"},
        _auth_session(cache),
    )
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Contact / Contact\n\n  - Last Name: Text\n\nTotal fields: 1"


@pytest.mark.asyncio
async def test_describe_business_object_unauthenticated() -> None:
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_business_object", {"business_object": "Account"}, _unauth_session())
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


# --- describe_business_object_json -----------------------------------------


@pytest.mark.asyncio
async def test_describe_business_object_json() -> None:
    descriptor = SiebelBCDescriptor(
        "Account", "Account", [SiebelFieldDescriptor("Name", "Text", max_length=30)]
    )
    cache = _cache(descriptor=descriptor)
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_business_object_json", {"business_object": "Account"}, _auth_session(cache))
    assert json.loads(resp["content"][0]["text"]) == {
        "business_object": "Account",
        "business_component": "Account",
        "fields": [
            {"name": "Name", "type": "Text", "required": False, "read_only": False, "max_length": 30}
        ],
    }


@pytest.mark.asyncio
async def test_describe_business_object_json_bo_not_found() -> None:
    cache = _cache(resolve=None)
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_business_object_json", {"business_object": "Nope"}, _auth_session(cache))
    assert resp.get("isError") is True  # json variant: _error, not _text
    assert resp["content"][0]["text"] == _BO_NOT_FOUND.format(bo="Nope")


@pytest.mark.asyncio
async def test_describe_business_object_json_50257_child_bc_fallback() -> None:
    """describe_business_object_json now applies the same SBL-EAI-50257 child-BC
    fallback as the prose variant: re-resolve the BC's own BO and retry, so the
    JSON carries the BC's own BO. (Task C added this fallback; the prior behavior
    was a masked error with no fallback.)"""
    own = SiebelBCDescriptor("Contact", "Contact", [SiebelFieldDescriptor("Last Name", "Text", max_length=50)])
    cache = _cache(
        resolve={"Account": "Account", "Contact": "Contact"},
        descriptor_side_effect=[
            SiebelError("not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}'),
            own,
        ],
    )
    reg = ToolRegistry(AppSettings())
    resp = await reg.call(
        "describe_business_object_json",
        {"business_object": "Account", "business_component": "Contact"},
        _auth_session(cache),
    )
    assert resp.get("isError") is not True
    assert json.loads(resp["content"][0]["text"]) == {
        "business_object": "Contact",
        "business_component": "Contact",
        "fields": [
            {"name": "Last Name", "type": "Text", "required": False, "read_only": False, "max_length": 50}
        ],
    }


# --- describe_repository (prose) -------------------------------------------


@pytest.mark.asyncio
async def test_describe_repository_prose() -> None:
    repo_fields = [
        {"Name": "Status", "Type": "Text"},
        {"Name": "Name", "Type": "Text", "Length": 30, "Required": "Y", "ReadOnly": "N", "Calculated": "N"},
    ]
    client = _client(repo_fields=repo_fields)
    cache = _cache(resolve="Account")
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_repository", {"business_object": "Account"}, _auth_session(cache, client))
    # fields sorted by Name: Name first, then Status.
    assert resp["content"][0]["text"] == (
        "Repository metadata: Account / Account\nTotal fields: 2\n\n"
        "  Name (Text)  [len=30, Required]\n  Status (Text)"
    )


@pytest.mark.asyncio
async def test_describe_repository_empty_fields() -> None:
    client = _client(repo_fields=[])
    cache = _cache(resolve="Account")
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_repository", {"business_object": "Account"}, _auth_session(cache, client))
    assert resp["content"][0]["text"] == (
        "No repository metadata found for Account/Account. "
        "The workspace metadata API may not be accessible. "
        "Use describe_business_object for the REST field list instead."
    )


@pytest.mark.asyncio
async def test_describe_repository_bo_not_found() -> None:
    cache = _cache(resolve=None)
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_repository", {"business_object": "Nope"}, _auth_session(cache))
    assert resp.get("isError") is not True  # prose: _text
    assert resp["content"][0]["text"] == _BO_NOT_FOUND.format(bo="Nope")


@pytest.mark.asyncio
async def test_describe_repository_siebel_error_is_masked() -> None:
    client = _client(repo_side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    cache = _cache(resolve="Account")
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_repository", {"business_object": "Account"}, _auth_session(cache, client))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_describe_repository_unauthenticated() -> None:
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_repository", {"business_object": "Account"}, _unauth_session())
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


# --- describe_repository_json ----------------------------------------------


@pytest.mark.asyncio
async def test_describe_repository_json() -> None:
    repo_fields = [
        {"Name": "Name", "Type": "Text", "Length": 30, "Required": "Y", "LOVType": "ACCOUNT_STATUS"},
        {"Name": "Status", "Type": "Text"},
    ]
    client = _client(repo_fields=repo_fields)
    cache = _cache(resolve="Account")
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_repository_json", {"business_object": "Account"}, _auth_session(cache, client))
    assert json.loads(resp["content"][0]["text"]) == {
        "business_object": "Account",
        "business_component": "Account",
        "fields": [
            {
                "name": "Name",
                "type": "Text",
                "lov_type": "ACCOUNT_STATUS",
                "required": True,
                "read_only": False,
                "calculated": False,
                "max_length": 30,
            },
            {
                "name": "Status",
                "type": "Text",
                "lov_type": "",
                "required": False,
                "read_only": False,
                "calculated": False,
                "max_length": None,
            },
        ],
    }


@pytest.mark.asyncio
async def test_describe_repository_json_bo_not_found() -> None:
    cache = _cache(resolve=None)
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("describe_repository_json", {"business_object": "Nope"}, _auth_session(cache))
    assert resp.get("isError") is True  # json: _error
    assert resp["content"][0]["text"] == _BO_NOT_FOUND.format(bo="Nope")


# --- get_query_syntax (static text, no auth) --------------------------------


@pytest.mark.asyncio
async def test_get_query_syntax_returns_static_text() -> None:
    reg = ToolRegistry(AppSettings())
    resp = await reg.call("get_query_syntax", {"user_question": "how?"}, _unauth_session())
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == (
        "Use <> for not equal; never use !=.\n"
        "Field names go in square brackets: [Status] = 'Open'.\n"
        "Use LIKE with * wildcards: [Name] LIKE 'Acme*'.\n"
        "Combine with AND/OR and parenthesize compound expressions.\n"
        "Dates may use Today(), Timestamp(), and arithmetic such as Today()-30.\n"
        "Siebel may return 404 when a query has no records."
    )
