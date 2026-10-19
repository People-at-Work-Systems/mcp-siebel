"""Unit tests for the ``bc_read`` kind: load-time option validation, bind, the
single-record direct read, the optional ``curated_query`` fallback (using
preferred_fields from the spec), and the with-children mode — exercised with
test-only fixture specs.

The bundled get_record / get_record_with_children tools migrate to bc_read in
a later commit; these tests cover the executor in isolation first.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelBCDescriptor, SiebelError, SiebelFieldDescriptor, SiebelQueryResult
from mcp_siebel.tools import SessionContext, ToolRegistry, _SpecError

_MASKED = "[INTERNAL - do not display verbatim] The Siebel operation could not be completed."
_BROKEN_FIELD_ERROR = SiebelError("Siebel API returned 500", 500, '{"ERROR":"SBL-DAT-00398"}')

# Shared input_schema fragments for the common (business_object + id) and
# (business_object + id + fields) argument sets — kept as module constants so
# the fixture-spec calls stay under the line length and read at a glance.
_BO_ID_PROPS: dict[str, object] = {"business_object": {"type": "string"}, "id": {"type": "string"}}
_BO_ID_FIELDS_PROPS: dict[str, object] = {
    "business_object": {"type": "string"},
    "id": {"type": "string"},
    "fields": {"type": "array", "items": {"type": "string"}},
}
_BO_ID_REQ: list[str] = ["business_object", "id"]
_FIELDS_PROPS: dict[str, object] = {"fields": {"type": "array", "items": {"type": "string"}}}


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _bundled_defs() -> dict[str, object]:
    return _bundled_spec().get("$defs", {})  # type: ignore[return-value]


def _spec_path(tmp_path, extra: dict[str, dict[str, object]], preferred_fields: object | None = None) -> str:
    spec = deepcopy(_bundled_spec())
    spec["tools"].update(extra)  # type: ignore[union-attr]
    if preferred_fields is not None:
        spec["preferred_fields"] = preferred_fields
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return str(path)


def _bcr(
    *,
    mode: str = "single",
    render: str = "record_prose",
    props: dict[str, object] | None = None,
    required: list[str] | None = None,
    bind: dict[str, object] | None = None,
    fallback: str | None = None,
    handler: str | None = None,
    access: str = "read",
) -> dict[str, object]:
    entry: dict[str, object] = {
        "description": "fixture bc_read tool",
        "kind": "bc_read",
        "access": access,
        "mode": mode,
        "render": render,
        "input_schema": {"type": "object", "properties": props or {}},
    }
    if required:
        entry["input_schema"]["required"] = required  # type: ignore[index]
    if bind is not None:
        entry["bind"] = bind
    if fallback is not None:
        entry["fallback"] = fallback
    if handler is not None:
        entry["handler"] = handler
    return entry


def _reg(
    tmp_path,
    extra: dict[str, dict[str, object]],
    preferred_fields: object | None = None,
    **settings_overrides,
) -> ToolRegistry:
    settings = AppSettings(**settings_overrides)
    settings.tools_spec_path = _spec_path(tmp_path, extra, preferred_fields)
    return ToolRegistry(settings)


def _session(client: AsyncMock, *, resolve: str | None = "Account") -> SessionContext:
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=resolve)
    return SessionContext(session_id="s1", username="t", client=client, cache=cache)


def _descriptor(*names: str, bc: str = "Contact") -> SiebelBCDescriptor:
    return SiebelBCDescriptor(bc, bc, [SiebelFieldDescriptor(name=n, type="DTYPE_TEXT") for n in names])


def _query_result(record: dict[str, str]) -> SiebelQueryResult:
    return SiebelQueryResult(items=[record], total_count=1, has_more=False, next_start_row=None, offset=0, page_size=1)


# ---------------------------------------------------------------------------
# Load-time validation
# ---------------------------------------------------------------------------


def test_bc_read_forbids_handler(tmp_path) -> None:
    with pytest.raises(_SpecError, match="handler"):
        _reg(tmp_path, {"x": _bcr(handler="mcp_siebel.handlers.records:_aggregate_data")})


def test_bc_read_access_must_be_read(tmp_path) -> None:
    with pytest.raises(_SpecError, match="permits access"):
        _reg(tmp_path, {"x": _bcr(access="write")})


def test_bc_read_missing_mode_rejected(tmp_path) -> None:
    entry = _bcr()
    del entry["mode"]
    with pytest.raises(_SpecError, match="missing required option"):
        _reg(tmp_path, {"x": entry})


def test_bc_read_invalid_mode_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="invalid mode"):
        _reg(tmp_path, {"x": _bcr(mode="bogus")})


def test_bc_read_unknown_render_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="unknown render"):
        _reg(tmp_path, {"x": _bcr(render="nope")})


def test_bc_read_render_wrong_family_rejected(tmp_path) -> None:
    """record_prose belongs to the 'record' family; a 'query'-family render is
    the wrong shape for bc_read."""
    with pytest.raises(_SpecError, match="family 'query'"):
        _reg(
            tmp_path,
            {"x": _bcr(render="query_prose", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
        )


def test_bc_read_fallback_invalid_value_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="invalid fallback"):
        _reg(
            tmp_path,
            {"x": _bcr(fallback="bogus", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
        )


def test_bc_read_fallback_only_valid_with_mode_single(tmp_path) -> None:
    with pytest.raises(_SpecError, match="only valid with mode 'single'"):
        _reg(
            tmp_path,
            {
                "x": _bcr(
                    mode="with_children",
                    render="record_hierarchical_prose",
                    fallback="curated_query",
                    props=_BO_ID_PROPS,
                    required=_BO_ID_REQ,
                )
            },
        )


def test_bc_read_bind_overlap_with_input_schema_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="also in input_schema"):
        _reg(
            tmp_path,
            {"x": _bcr(props=_BO_ID_PROPS, bind={"business_object": "Account"})},
        )


def test_bc_read_with_children_rejects_flat_record_render(tmp_path) -> None:
    """mode 'with_children' must use the hierarchical renderer; the flat
    record_prose would emit the client's __childrenAvailable / __<child>Available
    keys as prose lines (no __-prefix stripping)."""
    with pytest.raises(_SpecError, match="family 'record'"):
        _reg(
            tmp_path,
            {"x": _bcr(mode="with_children", render="record_prose", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
        )


def test_bc_read_with_children_rejects_bound_fields(tmp_path) -> None:
    """Binding 'fields' on a with_children tool is silently ignored (that mode
    never reads fields) — reject it at load time so a spec author hears about it."""
    with pytest.raises(_SpecError, match="not known executor parameters"):
        _reg(
            tmp_path,
            {
                "x": _bcr(
                    mode="with_children",
                    render="record_hierarchical_prose",
                    bind={"business_object": "Account", "id": "1", "fields": ["Name"]},
                    props={},
                )
            },
        )


def test_bc_read_unknown_bind_key_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="not known executor parameters"):
        _reg(tmp_path, {"x": _bcr(bind={"bogus": "x"}, props=_BO_ID_PROPS, required=_BO_ID_REQ)})


def test_bc_read_must_bind_or_require_business_object_and_id(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(tmp_path, {"x": _bcr(props={"business_object": {"type": "string"}}, required=["business_object"])})


def test_bc_read_bind_satisfies_required_coverage(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {"x": _bcr(bind={"business_object": "Account", "id": "1-ABC"}, props=_FIELDS_PROPS)},
    )
    assert "x" in reg._tools


def test_bc_read_bind_fields_wrong_type_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="fields.*list of strings"):
        _reg(
            tmp_path,
            {
                "x": _bcr(
                    bind={"business_object": "Account", "id": "1", "fields": "Name"},
                    props={"searchspec": {"type": "string"}},
                )
            },
        )


def test_bc_read_bind_id_wrong_type_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="id.*string"):
        _reg(
            tmp_path,
            {"x": _bcr(bind={"business_object": "Account", "id": 123}, props=_FIELDS_PROPS)},
        )


# ---------------------------------------------------------------------------
# mode "single" — direct read + curated_query fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bc_read_single_success(tmp_path) -> None:
    client = AsyncMock()
    client.get_record = AsyncMock(return_value={"Name": "Acme", "Id": "1-ABC"})
    reg = _reg(
        tmp_path,
        {"x": _bcr(mode="single", render="record_prose", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
    )
    resp = await reg.call("x", {"business_object": "Account", "id": "1-ABC"}, _session(client))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Account/Account record 1-ABC:\n  Name: Acme\n  Id: 1-ABC"
    assert client.get_record.call_args.args == ("Account", "Account", "1-ABC", None)


@pytest.mark.asyncio
async def test_bc_read_single_success_with_fields(tmp_path) -> None:
    client = AsyncMock()
    client.get_record = AsyncMock(return_value={"Name": "Acme", "Id": "1-ABC"})
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="single",
                render="record_prose",
                props=_BO_ID_FIELDS_PROPS, required=_BO_ID_REQ,
            )
        },
    )
    resp = await reg.call(
        "x", {"business_object": "Account", "id": "1-ABC", "fields": ["Name", "Id"]}, _session(client)
    )
    assert resp.get("isError") is not True
    assert client.get_record.call_args.args == ("Account", "Account", "1-ABC", ["Name", "Id"])


@pytest.mark.asyncio
async def test_bc_read_single_fallback_curated_uses_preferred_fields_from_spec(tmp_path) -> None:
    client = AsyncMock()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock(return_value=_descriptor("Last Name", "Favorite Color", "Id", "Junk"))
    client.query = AsyncMock(return_value=_query_result({"Last Name": "Taylor"}))
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="single",
                render="record_prose",
                fallback="curated_query",
                props=_BO_ID_PROPS, required=_BO_ID_REQ,
            )
        },
        preferred_fields={"Contact": ["Last Name", "Favorite Color"]},
    )
    resp = await reg.call("x", {"business_object": "Contact", "id": "1"}, _session(client, resolve="Contact"))
    assert resp.get("isError") is not True
    assert client.query.call_args.kwargs["fields"] == ["Last Name", "Favorite Color", "Id"]
    assert client.query.call_args.kwargs["search_spec"] == "[Id]='1'"
    assert client.query.call_args.kwargs["page_size"] == 1
    assert "Last Name: Taylor" in resp["content"][0]["text"]


@pytest.mark.asyncio
async def test_bc_read_single_fallback_reuses_caller_supplied_fields(tmp_path) -> None:
    client = AsyncMock()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock()
    client.query = AsyncMock(return_value=_query_result({"Last Name": "Taylor"}))
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="single",
                render="record_prose",
                fallback="curated_query",
                props=_BO_ID_FIELDS_PROPS, required=_BO_ID_REQ,
            )
        },
    )
    await reg.call(
        "x", {"business_object": "Contact", "id": "1", "fields": ["Last Name"]}, _session(client, resolve="Contact")
    )
    client.describe_bc.assert_not_awaited()
    assert client.query.call_args.kwargs["fields"] == ["Last Name"]


@pytest.mark.asyncio
async def test_bc_read_single_no_fallback_siebel_error_is_masked(tmp_path) -> None:
    """Without the curated_query fallback, a Siebel error propagates and the
    registry masks it — no scoped-query retry."""
    client = AsyncMock()
    client.get_record = AsyncMock(side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    reg = _reg(
        tmp_path,
        {"x": _bcr(mode="single", render="record_prose", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
    )
    resp = await reg.call("x", {"business_object": "Account", "id": "1"}, _session(client))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED
    client.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_bc_read_single_fallback_failure_is_masked(tmp_path) -> None:
    """With the fallback, if the scoped query also fails, the error propagates
    and is masked — matching get_record's behavior."""
    client = AsyncMock()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock(side_effect=SiebelError("nope", 500, "{}"))
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="single",
                render="record_prose",
                fallback="curated_query",
                props=_BO_ID_PROPS, required=_BO_ID_REQ,
            )
        },
    )
    resp = await reg.call("x", {"business_object": "Contact", "id": "1"}, _session(client, resolve="Contact"))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_bc_read_single_404_reports_no_record(tmp_path) -> None:
    client = AsyncMock()
    client.get_record = AsyncMock(return_value=None)
    reg = _reg(
        tmp_path,
        {"x": _bcr(mode="single", render="record_prose", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
    )
    resp = await reg.call("x", {"business_object": "Account", "id": "missing"}, _session(client))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "No record found."


@pytest.mark.asyncio
async def test_bc_read_single_bo_not_found(tmp_path) -> None:
    client = AsyncMock()
    reg = _reg(
        tmp_path,
        {"x": _bcr(mode="single", render="record_prose", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
    )
    resp = await reg.call("x", {"business_object": "Nope", "id": "1"}, _session(client, resolve=None))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.get_record.assert_not_awaited()


# ---------------------------------------------------------------------------
# mode "with_children"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bc_read_with_children_success(tmp_path) -> None:
    client = AsyncMock()
    client.get_record_with_children = AsyncMock(
        return_value={"__childrenAvailable": True, "Name": "Acme", "Id": "1-ABC"}
    )
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="with_children",
                render="record_hierarchical_prose",
                props=_BO_ID_PROPS, required=_BO_ID_REQ,
            )
        },
    )
    resp = await reg.call("x", {"business_object": "Account", "id": "1-ABC"}, _session(client))
    assert resp.get("isError") is not True
    # __-prefixed child-availability keys are stripped by the hierarchical formatter.
    assert resp["content"][0]["text"] == "Account/Account record 1-ABC:\n  Name: Acme\n  Id: 1-ABC"
    assert client.get_record_with_children.call_args.args == ("Account", "Account", "1-ABC")


@pytest.mark.asyncio
async def test_bc_read_with_children_siebel_error_is_masked(tmp_path) -> None:
    client = AsyncMock()
    client.get_record_with_children = AsyncMock(side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="with_children",
                render="record_hierarchical_prose",
                props=_BO_ID_PROPS, required=_BO_ID_REQ,
            )
        },
    )
    resp = await reg.call("x", {"business_object": "Account", "id": "1-ABC"}, _session(client))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_bc_read_with_children_404_reports_no_record(tmp_path) -> None:
    client = AsyncMock()
    client.get_record_with_children = AsyncMock(return_value=None)
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="with_children",
                render="record_hierarchical_prose",
                props=_BO_ID_PROPS, required=_BO_ID_REQ,
            )
        },
    )
    resp = await reg.call("x", {"business_object": "Account", "id": "missing"}, _session(client))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "No record found."


@pytest.mark.asyncio
async def test_bc_read_with_children_bo_not_found(tmp_path) -> None:
    client = AsyncMock()
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="with_children",
                render="record_hierarchical_prose",
                props=_BO_ID_PROPS, required=_BO_ID_REQ,
            )
        },
    )
    resp = await reg.call("x", {"business_object": "Nope", "id": "1"}, _session(client, resolve=None))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == 'Business Object "Nope" not found.'
    client.get_record_with_children.assert_not_awaited()


# ---------------------------------------------------------------------------
# bind — fixed values merged with the caller's args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bc_read_bind_supplies_business_object_and_id(tmp_path) -> None:
    client = AsyncMock()
    client.get_record = AsyncMock(return_value={"Name": "Acme"})
    reg = _reg(
        tmp_path,
        {
            "x": _bcr(
                mode="single",
                render="record_prose",
                bind={"business_object": "Account", "id": "1-ABC"},
                props=_FIELDS_PROPS,
            )
        },
    )
    resp = await reg.call("x", {}, _session(client))
    assert resp.get("isError") is not True
    assert client.get_record.call_args.args == ("Account", "Account", "1-ABC", None)


@pytest.mark.asyncio
async def test_bc_read_unauthenticated_returns_error(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {"x": _bcr(mode="single", render="record_prose", props=_BO_ID_PROPS, required=_BO_ID_REQ)},
    )
    resp = await reg.call("x", {"business_object": "Account", "id": "1"}, SessionContext(session_id="s1", username="t"))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."
