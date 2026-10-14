"""Unit tests for the ``bc_upsert`` and ``bc_delete`` kinds: load-time option
validation, ``bind``, ``allowed_fields``, and the runtime dispatch — exercised
with test-only fixture specs (the bundled tools are migrated in a later commit).

A fixture "JSON-only write tool" (bound BO/BC, ``allowed_fields``, ``create_json``
render) proves such a tool is gated by ``allow_write`` with no framework code
change — the gate keys off the spec entry's ``access``, not the kind.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError
from mcp_siebel.tools import SessionContext, ToolRegistry, _SpecError

_MASKED = "[INTERNAL - do not display verbatim] The Siebel operation could not be completed."


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _spec_path(tmp_path, extra: dict[str, dict[str, object]]) -> str:
    spec = deepcopy(_bundled_spec())
    spec["tools"].update(extra)  # type: ignore[union-attr]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return str(path)


def _upsert(
    *,
    operation: str = "create",
    scope: str = "root",
    render: str = "create_prose",
    props: dict[str, object] | None = None,
    required: list[str] | None = None,
    bind: dict[str, object] | None = None,
    allowed_fields: list[str] | None = None,
    access: str = "write",
) -> dict[str, object]:
    entry: dict[str, object] = {
        "description": "fixture bc_upsert tool",
        "kind": "bc_upsert",
        "access": access,
        "operation": operation,
        "scope": scope,
        "render": render,
        "input_schema": {"type": "object", "properties": props or {}},
    }
    if required:
        entry["input_schema"]["required"] = required  # type: ignore[index]
    if bind is not None:
        entry["bind"] = bind
    if allowed_fields is not None:
        entry["allowed_fields"] = allowed_fields
    return entry


def _delete(
    *,
    render: str = "delete_prose",
    props: dict[str, object] | None = None,
    required: list[str] | None = None,
    bind: dict[str, object] | None = None,
    access: str = "delete",
) -> dict[str, object]:
    entry: dict[str, object] = {
        "description": "fixture bc_delete tool",
        "kind": "bc_delete",
        "access": access,
        "render": render,
        "input_schema": {"type": "object", "properties": props or {}},
    }
    if required:
        entry["input_schema"]["required"] = required  # type: ignore[index]
    if bind is not None:
        entry["bind"] = bind
    return entry


def _reg(tmp_path, extra: dict[str, dict[str, object]], **settings_overrides) -> ToolRegistry:
    settings = AppSettings(**settings_overrides)
    settings.tools_spec_path = _spec_path(tmp_path, extra)
    return ToolRegistry(settings)


def _names(reg: ToolRegistry) -> set[str]:
    return {t["name"] for t in reg.list_tools()}


def _session(client: AsyncMock | None = None) -> tuple[SessionContext, AsyncMock]:
    client = client or AsyncMock()
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    return SessionContext(session_id="s1", username="t", client=client, cache=cache), client


def _no_client_session() -> SessionContext:
    return SessionContext(session_id="s1", username="t")


# ---------------------------------------------------------------------------
# bc_upsert — load-time validation
# ---------------------------------------------------------------------------


def test_upsert_access_must_be_write(tmp_path) -> None:
    with pytest.raises(_SpecError, match="permits access"):
        _reg(tmp_path, {"x": _upsert(access="read")})


def test_upsert_invalid_operation_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="invalid operation"):
        _reg(tmp_path, {"x": _upsert(operation="bogus")})


def test_upsert_invalid_scope_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="invalid scope"):
        _reg(tmp_path, {"x": _upsert(scope="bogus")})


def test_upsert_unknown_render_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="unknown render"):
        _reg(tmp_path, {"x": _upsert(render="nope")})


def test_upsert_allowed_fields_must_be_list_of_strings(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be a list of strings"):
        _reg(tmp_path, {"x": _upsert(allowed_fields="Name", props={"data": {"type": "object"}}, required=["data"])})


def test_upsert_allowed_fields_non_string_element_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be a list of strings"):
        _reg(
            tmp_path,
            {"x": _upsert(allowed_fields=["Name", 123], props={"data": {"type": "object"}}, required=["data"])},
        )


def test_upsert_create_root_requires_business_object(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(tmp_path, {"x": _upsert(props={"data": {"type": "object"}}, required=["data"])})


def test_upsert_create_child_requires_parent_and_child_bc(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(
            tmp_path,
            {
                "x": _upsert(
                    operation="create",
                    scope="child",
                    props={"business_object": {"type": "string"}},
                    required=["business_object"],
                )
            },
        )


def test_upsert_update_requires_id(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(
            tmp_path,
            {
                "x": _upsert(
                    operation="update",
                    scope="root",
                    props={"business_object": {"type": "string"}, "data": {"type": "object"}},
                    required=["business_object", "data"],
                )
            },
        )


def test_upsert_unknown_bind_key_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="not known executor parameters"):
        _reg(tmp_path, {"x": _upsert(bind={"bogus": "x"}, props={"data": {"type": "object"}}, required=["data"])})


def test_upsert_bind_overlap_with_input_schema_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="also in input_schema"):
        _reg(
            tmp_path,
            {
                "x": _upsert(
                    bind={"business_object": "Account"},
                    props={"business_object": {"type": "string"}, "data": {"type": "object"}},
                    required=["data"],
                )
            },
        )


# ---------------------------------------------------------------------------
# bc_upsert — a JSON-only write tool is gated by allow_write (no code change)
# ---------------------------------------------------------------------------


_JSON_ONLY_WRITE: dict[str, object] = _upsert(
    bind={"business_object": "Account", "business_component": "Account"},
    props={"data": {"type": "object"}},
    required=["data"],
    allowed_fields=["Name", "Status"],
    render="create_json",
)


def test_json_only_write_tool_hidden_when_allow_write_off(tmp_path) -> None:
    reg = _reg(tmp_path, {"acct_create_json": deepcopy(_JSON_ONLY_WRITE)})
    assert "acct_create_json" not in _names(reg)


def test_json_only_write_tool_listed_when_allow_write_on(tmp_path) -> None:
    reg = _reg(tmp_path, {"acct_create_json": deepcopy(_JSON_ONLY_WRITE)}, allow_write=True)
    assert "acct_create_json" in _names(reg)


@pytest.mark.asyncio
async def test_json_only_write_tool_call_refused_when_gated(tmp_path) -> None:
    reg = _reg(tmp_path, {"acct_create_json": deepcopy(_JSON_ONLY_WRITE)})
    session, client = _session()
    resp = await reg.call("acct_create_json", {"data": {"Name": "x"}}, session)
    assert resp.get("isError") is True
    assert "disabled" in resp["content"][0]["text"].lower()
    client.create_record.assert_not_called()


# ---------------------------------------------------------------------------
# bc_upsert — runtime dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_bind_supplies_bo_bc(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create": _upsert(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"data": {"type": "object"}},
                required=["data"],
                render="create_prose",
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.create_record = AsyncMock(return_value={"Id": "1", "Name": "Acme"})
    session, _ = _session(client)
    resp = await reg.call("acct_create", {"data": {"Name": "Acme"}}, session)
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Created record in Account/Account:\n  Id: 1\n  Name: Acme"
    args = client.create_record.call_args.args
    assert args[0] == "Account"
    assert args[1] == "Account"
    assert args[2] == {"Name": "Acme"}


@pytest.mark.asyncio
async def test_upsert_create_json_render(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create_json": _upsert(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"data": {"type": "object"}},
                required=["data"],
                render="create_json",
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.create_record = AsyncMock(return_value={"Id": "1-ABC", "Name": "Acme"})
    session, _ = _session(client)
    resp = await reg.call("acct_create_json", {"data": {"Name": "Acme"}}, session)
    assert json.loads(resp["content"][0]["text"]) == {"Id": "1-ABC", "Name": "Acme"}


@pytest.mark.asyncio
async def test_upsert_update_root(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_update": _upsert(
                operation="update",
                scope="root",
                render="update_prose",
                bind={"business_object": "Account", "business_component": "Account"},
                props={"id": {"type": "string"}, "data": {"type": "object"}},
                required=["id", "data"],
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.update_record = AsyncMock(return_value={"Id": "1-XYZ", "Name": "New"})
    session, _ = _session(client)
    resp = await reg.call("acct_update", {"id": "1-XYZ", "data": {"Name": "New"}}, session)
    assert resp["content"][0]["text"] == "Updated record 1-XYZ in Account/Account:\n  Id: 1-XYZ\n  Name: New"
    args = client.update_record.call_args.args
    assert args[2] == "1-XYZ"
    assert args[3] == {"Name": "New"}


@pytest.mark.asyncio
async def test_upsert_create_child(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_child": _upsert(
                operation="create",
                scope="child",
                render="child_create_prose",
                bind={"business_object": "Account", "business_component": "Account"},
                props={
                    "parent_id": {"type": "string"},
                    "child_business_component": {"type": "string"},
                    "data": {"type": "object"},
                },
                required=["parent_id", "child_business_component", "data"],
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.create_child_record = AsyncMock(return_value={"Id": "1-C", "Last Name": "Smith"})
    session, _ = _session(client)
    resp = await reg.call(
        "acct_child",
        {"parent_id": "1-P", "child_business_component": "Contact", "data": {"Last Name": "Smith"}},
        session,
    )
    expected = "Created child record(s) under Account/Account/1-P:\n  Id: 1-C\n  Last Name: Smith"
    assert resp["content"][0]["text"] == expected
    args = client.create_child_record.call_args.args
    assert args[2] == "1-P"
    assert args[3] == "Contact"


@pytest.mark.asyncio
async def test_upsert_allowed_fields_rejects_unknown_field(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create": _upsert(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"data": {"type": "object"}},
                required=["data"],
                allowed_fields=["Name", "Status"],
                render="create_prose",
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.create_record = AsyncMock(return_value={"Id": "1"})
    session, _ = _session(client)
    resp = await reg.call("acct_create", {"data": {"Name": "Acme", "Bogus": "x"}}, session)
    assert resp.get("isError") is True
    assert "Bogus" in resp["content"][0]["text"]
    client.create_record.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_allowed_fields_accepts_listed_fields(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create": _upsert(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"data": {"type": "object"}},
                required=["data"],
                allowed_fields=["Name", "Status"],
                render="create_prose",
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.create_record = AsyncMock(return_value={"Id": "1", "Name": "Acme", "Status": "Active"})
    session, _ = _session(client)
    resp = await reg.call("acct_create", {"data": {"Name": "Acme", "Status": "Active"}}, session)
    assert resp.get("isError") is not True
    client.create_record.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_rejects_empty_data(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create": _upsert(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"data": {"type": "object"}},
                required=["data"],
                render="create_prose",
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.create_record = AsyncMock(return_value={"Id": "1"})
    session, _ = _session(client)
    resp = await reg.call("acct_create", {"data": {}}, session)
    assert resp.get("isError") is True
    assert "data" in resp["content"][0]["text"].lower()
    client.create_record.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_bo_not_found(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create": _upsert(
                bind={"business_object": "Nope", "business_component": "Nope"},
                props={"data": {"type": "object"}},
                required=["data"],
                render="create_prose",
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=None)
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)
    resp = await reg.call("acct_create", {"data": {"Name": "x"}}, session)
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.create_record.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_siebel_error_is_masked(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create": _upsert(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"data": {"type": "object"}},
                required=["data"],
                render="create_prose",
            )
        },
        allow_write=True,
    )
    client = AsyncMock()
    client.create_record = AsyncMock(side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    session, _ = _session(client)
    resp = await reg.call("acct_create", {"data": {"Name": "x"}}, session)
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_upsert_unauthenticated_returns_error(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_create": _upsert(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"data": {"type": "object"}},
                required=["data"],
                render="create_prose",
            )
        },
        allow_write=True,
    )
    resp = await reg.call("acct_create", {"data": {"Name": "x"}}, _no_client_session())
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


# ---------------------------------------------------------------------------
# bc_delete — load-time validation
# ---------------------------------------------------------------------------


def test_delete_access_must_be_delete(tmp_path) -> None:
    with pytest.raises(_SpecError, match="permits access"):
        _reg(tmp_path, {"x": _delete(access="read")})


def test_delete_unknown_render_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="unknown render"):
        _reg(tmp_path, {"x": _delete(render="nope")})


def test_delete_requires_id(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(tmp_path, {"x": _delete(props={"business_object": {"type": "string"}}, required=["business_object"])})


def test_delete_unknown_bind_key_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="not known executor parameters"):
        _reg(tmp_path, {"x": _delete(bind={"bogus": "x"}, props={"id": {"type": "string"}}, required=["id"])})


# ---------------------------------------------------------------------------
# bc_delete — runtime + gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_success(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_delete": _delete(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"id": {"type": "string"}},
                required=["id"],
            )
        },
        allow_delete=True,
    )
    client = AsyncMock()
    client.delete_record = AsyncMock(return_value=None)
    session, _ = _session(client)
    resp = await reg.call("acct_delete", {"id": "1-DEL"}, session)
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Deleted record 1-DEL from Account/Account."
    args = client.delete_record.call_args.args
    assert args[0] == "Account"
    assert args[1] == "Account"
    assert args[2] == "1-DEL"


@pytest.mark.asyncio
async def test_delete_bo_not_found(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_delete": _delete(
                bind={"business_object": "Nope", "business_component": "Nope"},
                props={"id": {"type": "string"}},
                required=["id"],
            )
        },
        allow_delete=True,
    )
    client = AsyncMock()
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=None)
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)
    resp = await reg.call("acct_delete", {"id": "1-DEL"}, session)
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.delete_record.assert_not_called()


@pytest.mark.asyncio
async def test_delete_unauthenticated_returns_error(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_delete": _delete(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"id": {"type": "string"}},
                required=["id"],
            )
        },
        allow_delete=True,
    )
    resp = await reg.call("acct_delete", {"id": "1"}, _no_client_session())
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."


def test_delete_tool_gated_by_allow_delete(tmp_path) -> None:
    fixture = _delete(
        bind={"business_object": "Account", "business_component": "Account"},
        props={"id": {"type": "string"}},
        required=["id"],
    )
    reg_off = _reg(tmp_path, {"acct_delete": deepcopy(fixture)})
    assert "acct_delete" not in _names(reg_off)
    reg_on = _reg(tmp_path, {"acct_delete": deepcopy(fixture)}, allow_delete=True)
    assert "acct_delete" in _names(reg_on)


@pytest.mark.asyncio
async def test_delete_call_refused_when_gated(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "acct_delete": _delete(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"id": {"type": "string"}},
                required=["id"],
            )
        },
    )
    session, client = _session()
    resp = await reg.call("acct_delete", {"id": "1"}, session)
    assert resp.get("isError") is True
    assert "disabled" in resp["content"][0]["text"].lower()
    client.delete_record.assert_not_called()
