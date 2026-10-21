"""Characterization tests for the 5 write/delete tools being migrated to the
``bc_upsert`` / ``bc_delete`` kinds: create_record, create_record_json,
update_record, create_child_record, delete_record.

These pin the current output (prose + JSON), the error paths (Siebel error,
BO-not-found, unauthenticated session) and the ``enabled`` gate, by mocking at
the SiebelClient level (session.client.create_record / update_record /
delete_record / create_child_record) and driving each tool through
``ToolRegistry.call`` — so they are agnostic to the tool's ``kind`` and pass
unchanged once the tools move from kind "python" to the new executors.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError
from mcp_siebel.tools import SessionContext, ToolRegistry

_MASKED = "[INTERNAL - do not display verbatim] The Siebel operation could not be completed."


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _registry_with_enabled(tmp_path, *names: str) -> ToolRegistry:
    """Bundled spec with the named tools flipped to ``enabled: true``. The
    bundled spec disables the 7 write/delete/external tools by default
    (read-only by default); flipping ``enabled: true`` on an entry in an
    override spec is how a tool is turned on — no env var."""
    spec = deepcopy(_bundled_spec())
    tools = spec["tools"]  # type: ignore[index]
    for n in names:
        tools[n] = {**tools[n], "enabled": True}  # type: ignore[index]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(path)
    return ToolRegistry(settings)


_WRITE_TOOLS = ("create_record", "create_record_json", "update_record", "create_child_record")


def _write_registry(tmp_path) -> ToolRegistry:
    return _registry_with_enabled(tmp_path, *_WRITE_TOOLS)


def _delete_registry(tmp_path) -> ToolRegistry:
    return _registry_with_enabled(tmp_path, "delete_record")


def _session(client: AsyncMock | None) -> SessionContext:
    if client is None:
        # Unauthenticated: no client, no cache.
        return SessionContext(session_id="s1", username="tester")
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    return SessionContext(session_id="s1", username="tester", client=client, cache=cache)


def _client() -> AsyncMock:
    return AsyncMock()


# --- create_record (prose) --------------------------------------------------


@pytest.mark.asyncio
async def test_create_record_success_prose(tmp_path) -> None:
    client = _client()
    client.create_record = AsyncMock(return_value={"Id": "1-ABC", "Name": "Acme"})
    reg = _write_registry(tmp_path)
    resp = await reg.call("create_record", {"business_object": "Account", "data": {"Name": "Acme"}}, _session(client))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Created record in Account/Account:\n  Id: 1-ABC\n  Name: Acme"
    # resolved BO/BC and the data payload reach the client.
    args = client.create_record.call_args.args
    assert args[0] == "Account"  # business_object
    assert args[1] == "Account"  # business_component (defaults to BO)
    assert args[2] == {"Name": "Acme"}


@pytest.mark.asyncio
async def test_create_record_rejects_empty_data(tmp_path) -> None:
    client = _client()
    client.create_record = AsyncMock(return_value={"Id": "1"})
    reg = _write_registry(tmp_path)
    resp = await reg.call("create_record", {"business_object": "Account", "data": {}}, _session(client))
    assert resp.get("isError") is True
    assert "data" in resp["content"][0]["text"].lower()
    client.create_record.assert_not_called()


@pytest.mark.asyncio
async def test_create_record_siebel_error_is_masked(tmp_path) -> None:
    client = _client()
    client.create_record = AsyncMock(side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    reg = _write_registry(tmp_path)
    resp = await reg.call("create_record", {"business_object": "Account", "data": {"Name": "x"}}, _session(client))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_create_record_bo_not_found(tmp_path) -> None:
    client = _client()
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=None)
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)
    reg = _write_registry(tmp_path)
    resp = await reg.call("create_record", {"business_object": "Nope", "data": {"Name": "x"}}, session)
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.create_record.assert_not_called()


# --- create_record_json -----------------------------------------------------


@pytest.mark.asyncio
async def test_create_record_json_success_returns_json(tmp_path) -> None:
    client = _client()
    client.create_record = AsyncMock(return_value={"Id": "1-ABC", "Name": "Acme"})
    reg = _write_registry(tmp_path)
    resp = await reg.call(
        "create_record_json", {"business_object": "Account", "data": {"Name": "Acme"}}, _session(client)
    )
    assert resp.get("isError") is not True
    assert json.loads(resp["content"][0]["text"]) == {"Id": "1-ABC", "Name": "Acme"}


@pytest.mark.asyncio
async def test_create_record_json_rejects_empty_data(tmp_path) -> None:
    client = _client()
    client.create_record = AsyncMock(return_value={"Id": "1"})
    reg = _write_registry(tmp_path)
    resp = await reg.call("create_record_json", {"business_object": "Account", "data": {}}, _session(client))
    assert resp.get("isError") is True
    client.create_record.assert_not_called()


# --- update_record ----------------------------------------------------------


@pytest.mark.asyncio
async def test_update_record_success_prose(tmp_path) -> None:
    client = _client()
    client.update_record = AsyncMock(return_value={"Id": "1-XYZ", "Name": "New"})
    reg = _write_registry(tmp_path)
    resp = await reg.call(
        "update_record", {"business_object": "Account", "id": "1-XYZ", "data": {"Name": "New"}}, _session(client)
    )
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Updated record 1-XYZ in Account/Account:\n  Id: 1-XYZ\n  Name: New"
    args = client.update_record.call_args.args
    assert args[0] == "Account"
    assert args[1] == "Account"
    assert args[2] == "1-XYZ"
    assert args[3] == {"Name": "New"}


@pytest.mark.asyncio
async def test_update_record_expired_session_error(tmp_path) -> None:
    """A 401 SiebelError surfaces the session-expired message, not the generic mask."""
    client = _client()
    client.update_record = AsyncMock(side_effect=SiebelError("unauth", 401, ""))
    reg = _write_registry(tmp_path)
    resp = await reg.call(
        "update_record", {"business_object": "Account", "id": "1-XYZ", "data": {"Name": "x"}}, _session(client)
    )
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "The Siebel session has expired. Refresh the page or log in again."


# --- create_child_record ----------------------------------------------------


@pytest.mark.asyncio
async def test_create_child_record_success_prose(tmp_path) -> None:
    client = _client()
    client.create_child_record = AsyncMock(return_value={"Id": "1-C", "Last Name": "Smith"})
    reg = _write_registry(tmp_path)
    resp = await reg.call(
        "create_child_record",
        {
            "business_object": "Account",
            "parent_id": "1-P",
            "child_business_component": "Contact",
            "data": {"Last Name": "Smith"},
        },
        _session(client),
    )
    assert resp.get("isError") is not True
    expected = "Created child record(s) under Account/Account/1-P:\n  Id: 1-C\n  Last Name: Smith"
    assert resp["content"][0]["text"] == expected
    args = client.create_child_record.call_args.args
    assert args[0] == "Account"
    assert args[1] == "Account"
    assert args[2] == "1-P"
    assert args[3] == "Contact"
    assert args[4] == {"Last Name": "Smith"}


@pytest.mark.asyncio
async def test_create_child_record_bo_not_found(tmp_path) -> None:
    client = _client()
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=None)
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)
    reg = _write_registry(tmp_path)
    resp = await reg.call(
        "create_child_record",
        {"business_object": "Nope", "parent_id": "1-P", "child_business_component": "Contact", "data": {"x": "y"}},
        session,
    )
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.create_child_record.assert_not_called()


# --- delete_record ----------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_record_success_prose(tmp_path) -> None:
    client = _client()
    client.delete_record = AsyncMock(return_value=None)
    reg = _delete_registry(tmp_path)
    resp = await reg.call("delete_record", {"business_object": "Account", "id": "1-DEL"}, _session(client))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Deleted record 1-DEL from Account/Account."
    args = client.delete_record.call_args.args
    assert args[0] == "Account"
    assert args[1] == "Account"
    assert args[2] == "1-DEL"


@pytest.mark.asyncio
async def test_delete_record_siebel_error_is_masked(tmp_path) -> None:
    client = _client()
    client.delete_record = AsyncMock(side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    reg = _delete_registry(tmp_path)
    resp = await reg.call("delete_record", {"business_object": "Account", "id": "1-DEL"}, _session(client))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_delete_record_bo_not_found(tmp_path) -> None:
    client = _client()
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=None)
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)
    reg = _delete_registry(tmp_path)
    resp = await reg.call("delete_record", {"business_object": "Nope", "id": "1-DEL"}, session)
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.delete_record.assert_not_called()


# --- unauthenticated session ------------------------------------------------


@pytest.mark.asyncio
async def test_write_tools_unauthenticated_returns_error(tmp_path) -> None:
    reg = _write_registry(tmp_path)
    for tool in ("create_record", "create_record_json", "update_record", "create_child_record"):
        resp = await reg.call(tool, {"business_object": "Account", "data": {"x": "y"}}, _session(None))
        assert resp.get("isError") is True, tool
        assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


@pytest.mark.asyncio
async def test_delete_record_unauthenticated_returns_error(tmp_path) -> None:
    reg = _delete_registry(tmp_path)
    resp = await reg.call("delete_record", {"business_object": "Account", "id": "1"}, _session(None))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


# --- access gating ----------------------------------------------------------


@pytest.mark.asyncio
async def test_write_tools_refused_and_hidden_when_disabled() -> None:
    reg = ToolRegistry(AppSettings())  # bundled spec: write tools are enabled: false
    names = {t["name"] for t in reg.list_tools()}
    for tool in ("create_record", "create_record_json", "update_record", "create_child_record"):
        assert tool not in names
        resp = await reg.call(tool, {"business_object": "Account", "data": {"x": "y"}}, _session(_client()))
        assert resp.get("isError") is True, tool
        assert "disabled" in resp["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_delete_record_refused_and_hidden_when_disabled() -> None:
    reg = ToolRegistry(AppSettings())  # bundled spec: delete_record is enabled: false
    names = {t["name"] for t in reg.list_tools()}
    assert "delete_record" not in names
    resp = await reg.call("delete_record", {"business_object": "Account", "id": "1"}, _session(_client()))
    assert resp.get("isError") is True
    assert "disabled" in resp["content"][0]["text"].lower()
