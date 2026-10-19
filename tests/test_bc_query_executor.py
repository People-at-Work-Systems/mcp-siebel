"""Unit tests for the ``bc_query`` kind: load-time option validation, bind, and
searchspec_template — exercised with test-only fixture specs (none of the
bundled tools use bind or a template yet; customer-specific tools with a fixed
BO/BC/fields will depend on both).
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError, SiebelQueryResult
from mcp_siebel.tools import SessionContext, ToolRegistry, _SpecError


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _spec_path(tmp_path, extra: dict[str, dict[str, object]]) -> str:
    """Bundled spec + extra/overriding tool entries, written to a temp file."""
    spec = deepcopy(_bundled_spec())
    spec["tools"].update(extra)  # type: ignore[union-attr]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return str(path)


def _bcq(
    *,
    scope: str = "root",
    result: str = "records",
    render: str = "query_prose",
    props: dict[str, object] | None = None,
    required: list[str] | None = None,
    bind: dict[str, object] | None = None,
    template: str | None = None,
    handler: str | None = None,
    access: str = "read",
) -> dict[str, object]:
    entry: dict[str, object] = {
        "description": "fixture bc_query tool",
        "kind": "bc_query",
        "access": access,
        "scope": scope,
        "result": result,
        "render": render,
        "input_schema": {"type": "object", "properties": props or {}},
    }
    if required:
        entry["input_schema"]["required"] = required  # type: ignore[index]
    if bind is not None:
        entry["bind"] = bind
    if template is not None:
        entry["searchspec_template"] = template
    if handler is not None:
        entry["handler"] = handler
    return entry


def _reg(tmp_path, extra: dict[str, dict[str, object]], **settings_overrides) -> ToolRegistry:
    settings = AppSettings(**settings_overrides)
    settings.tools_spec_path = _spec_path(tmp_path, extra)
    return ToolRegistry(settings)


def _session(query_result: SiebelQueryResult | None = None) -> tuple[SessionContext, AsyncMock]:
    client = AsyncMock()
    if query_result is not None:
        client.query = AsyncMock(return_value=query_result)
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    return SessionContext(session_id="s1", username="t", client=client, cache=cache), client


# ---------------------------------------------------------------------------
# Load-time validation
# ---------------------------------------------------------------------------


def test_bc_query_forbids_handler(tmp_path) -> None:
    with pytest.raises(_SpecError, match="handler"):
        _reg(tmp_path, {"x": _bcq(handler="mcp_siebel.handlers.records:_aggregate_data")})


def test_bc_query_access_must_be_read(tmp_path) -> None:
    with pytest.raises(_SpecError, match="permits access"):
        _reg(tmp_path, {"x": _bcq(access="write")})


def test_bc_query_unknown_render_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="unknown render"):
        _reg(tmp_path, {"x": _bcq(render="nope")})


def test_bc_query_missing_required_option_rejected(tmp_path) -> None:
    entry = _bcq()
    del entry["scope"]
    with pytest.raises(_SpecError, match="missing required option"):
        _reg(tmp_path, {"x": entry})


def test_bc_query_invalid_scope_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="invalid scope"):
        _reg(tmp_path, {"x": _bcq(scope="bogus")})


def test_bc_query_bind_overlap_with_input_schema_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="also in input_schema"):
        _reg(
            tmp_path,
            {"x": _bcq(props={"business_object": {"type": "string"}}, bind={"business_object": "Account"})},
        )


def test_bc_query_template_placeholder_must_exist_in_input_schema(tmp_path) -> None:
    with pytest.raises(_SpecError, match="not in input_schema"):
        _reg(
            tmp_path,
            {"x": _bcq(props={"name": {"type": "string"}}, template="[Name] LIKE '{foo}'")},
        )


def test_bc_query_template_and_searchspec_arg_mutually_exclusive(tmp_path) -> None:
    with pytest.raises(_SpecError, match="mutually exclusive"):
        _reg(
            tmp_path,
            {"x": _bcq(props={"id": {"type": "string"}, "searchspec": {"type": "string"}}, template="[Id]='{id}'")},
        )


def test_bc_query_template_and_searchspec_bind_mutually_exclusive(tmp_path) -> None:
    """A searchspec_template and a bound ``searchspec`` are mutually exclusive too
    — a fixed searchspec would override the template's whole purpose."""
    with pytest.raises(_SpecError, match="mutually exclusive"):
        _reg(
            tmp_path,
            {
                "x": _bcq(
                    bind={"business_object": "Account", "searchspec": "[Name] LIKE 'A*'"},
                    props={"name": {"type": "string"}},
                    required=["name"],
                    template="[Name] = '{name}'",
                )
            },
        )


def test_bc_query_template_placeholder_outside_quotes_rejected(tmp_path) -> None:
    """A template placeholder must sit inside a single-quoted literal — Siebel
    treats a bare token as a field reference / operator, not a value."""
    with pytest.raises(_SpecError, match="inside a single-quoted"):
        _reg(
            tmp_path,
            {
                "x": _bcq(
                    bind={"business_object": "Account", "business_component": "Account"},
                    props={"name": {"type": "string"}},
                    required=["name"],
                    template="{name} LIKE 'x'",
                )
            },
        )


def test_bc_query_unknown_bind_key_rejected(tmp_path) -> None:
    """A bind key must name a parameter the executor actually reads."""
    with pytest.raises(_SpecError, match="not known executor parameters"):
        _reg(tmp_path, {"x": _bcq(bind={"bogus": "x"}, props={"name": {"type": "string"}}, required=["name"])})


def test_bc_query_root_must_bind_or_require_business_object(tmp_path) -> None:
    """Root scope: business_object must be bound or in input_schema.required."""
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(tmp_path, {"x": _bcq(props={"name": {"type": "string"}}, required=["name"])})


def test_bc_query_child_must_bind_or_require_parent_and_child_bc(tmp_path) -> None:
    """Child scope: parent_id and child_business_component must each be bound or
    required (business_object is required here, the other two are not)."""
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(
            tmp_path,
            {"x": _bcq(scope="child", props={"business_object": {"type": "string"}}, required=["business_object"])},
        )


def test_bc_query_bind_satisfies_required_coverage(tmp_path) -> None:
    """A bound parameter satisfies required-coverage without being in required."""
    reg = _reg(
        tmp_path,
        {"x": _bcq(bind={"business_object": "Account"}, props={"searchspec": {"type": "string"}})},
    )
    assert "x" in reg._tools


# --- renderer families + bind type checks (step 4 Task A) -------------------


def test_bc_query_records_result_rejects_count_family_render(tmp_path) -> None:
    """result=records must use a 'query'-family render, not a 'count' one."""
    with pytest.raises(_SpecError, match="family 'count'"):
        _reg(
            tmp_path,
            {
                "x": _bcq(
                    result="records",
                    render="count_prose",
                    bind={"business_object": "Account"},
                    props={"searchspec": {"type": "string"}},
                )
            },
        )


def test_bc_query_count_result_rejects_query_family_render(tmp_path) -> None:
    """result=count must use a 'count'-family render, not a 'query' one."""
    with pytest.raises(_SpecError, match="family 'query'"):
        _reg(
            tmp_path,
            {
                "x": _bcq(
                    result="count",
                    render="query_prose",
                    bind={"business_object": "Account"},
                    props={"searchspec": {"type": "string"}},
                )
            },
        )


def test_bc_query_count_json_render_loads(tmp_path) -> None:
    """count_json is a 'count'-family render usable with result=count."""
    reg = _reg(
        tmp_path,
        {
            "x": _bcq(
                result="count",
                render="count_json",
                bind={"business_object": "Account", "business_component": "Account"},
                props={"searchspec": {"type": "string"}},
            )
        },
    )
    assert "x" in reg._tools


def test_bc_query_bind_page_size_wrong_type_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="page_size.*non-negative integer"):
        _reg(
            tmp_path,
            {
                "x": _bcq(
                    bind={"business_object": "Account", "page_size": "20"}, props={"searchspec": {"type": "string"}}
                )
            },
        )


def test_bc_query_bind_fields_wrong_type_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="fields.*list of strings"):
        _reg(
            tmp_path,
            {
                "x": _bcq(
                    bind={"business_object": "Account", "fields": "Name"}, props={"searchspec": {"type": "string"}}
                )
            },
        )


def test_bc_query_bind_business_object_wrong_type_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="business_object.*string"):
        _reg(tmp_path, {"x": _bcq(bind={"business_object": 123}, props={"searchspec": {"type": "string"}})})


@pytest.mark.asyncio
async def test_bc_query_bind_page_size_int_is_accepted(tmp_path) -> None:
    """A correctly-typed int bind for page_size flows through to the client."""
    result = SiebelQueryResult(items=[{"Id": "1"}], total_count=1, has_more=False, offset=0, page_size=5)
    reg = _reg(
        tmp_path,
        {
            "x": _bcq(
                bind={"business_object": "Account", "business_component": "Account", "page_size": 5},
                props={"searchspec": {"type": "string"}},
            )
        },
    )
    session, client = _session(result)
    await reg.call("x", {"searchspec": "[Name] LIKE 'A*'"}, session)
    assert client.query.call_args.kwargs["page_size"] == 5


# ---------------------------------------------------------------------------
# bind — fixed values merged with the caller's args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_supplies_business_object_and_component(tmp_path) -> None:
    result = SiebelQueryResult(
        items=[{"Id": "1", "Name": "Acme"}], total_count=1, has_more=False, offset=0, page_size=20
    )
    reg = _reg(
        tmp_path,
        {
            "acct_query": _bcq(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"searchspec": {"type": "string"}, "fields": {"type": "array", "items": {"type": "string"}}},
            )
        },
    )
    session, client = _session(result)
    resp = await reg.call("acct_query", {"searchspec": "[Name] LIKE 'A*'"}, session)
    assert resp.get("isError") is not True
    args, kwargs = client.query.call_args
    assert args[0] == "Account"  # business_object (positional)
    assert args[1] == "Account"  # business_component (positional)
    assert kwargs["search_spec"] == "[Name] LIKE 'A*'"


# ---------------------------------------------------------------------------
# searchspec_template — placeholder substitution + escaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_searchspec_template_substitutes_placeholders(tmp_path) -> None:
    result = SiebelQueryResult(items=[{"Id": "1"}], total_count=1, has_more=False, offset=0, page_size=20)
    reg = _reg(
        tmp_path,
        {
            "name_query": _bcq(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"name_prefix": {"type": "string"}},
                required=["name_prefix"],
                template="[Name] LIKE '{name_prefix}*'",
            )
        },
    )
    session, client = _session(result)
    await reg.call("name_query", {"name_prefix": "A"}, session)
    assert client.query.call_args.kwargs["search_spec"] == "[Name] LIKE 'A*'"


@pytest.mark.asyncio
async def test_searchspec_template_doubles_single_quotes(tmp_path) -> None:
    result = SiebelQueryResult(items=[{"Id": "1"}], total_count=1, has_more=False, offset=0, page_size=20)
    reg = _reg(
        tmp_path,
        {
            "name_query": _bcq(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"name": {"type": "string"}},
                required=["name"],
                template="[Name] = '{name}'",
            )
        },
    )
    session, client = _session(result)
    await reg.call("name_query", {"name": "O'Brien"}, session)
    assert client.query.call_args.kwargs["search_spec"] == "[Name] = 'O''Brien'"


@pytest.mark.asyncio
async def test_searchspec_template_rejects_forbidden_chars(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "name_query": _bcq(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"name": {"type": "string"}},
                required=["name"],
                template="[Name] = '{name}'",
            )
        },
    )
    session, client = _session()
    resp = await reg.call("name_query", {"name": "A[B"}, session)
    assert resp.get("isError") is True
    assert "not allowed" in resp["content"][0]["text"]
    client.query.assert_not_called()


@pytest.mark.asyncio
async def test_searchspec_template_missing_placeholder_value_is_a_tool_error(tmp_path) -> None:
    reg = _reg(
        tmp_path,
        {
            "name_query": _bcq(
                bind={"business_object": "Account", "business_component": "Account"},
                props={"name": {"type": "string"}},
                template="[Name] = '{name}'",
            )
        },
    )
    session, client = _session()
    resp = await reg.call("name_query", {}, session)
    assert resp.get("isError") is True
    assert "was not provided" in resp["content"][0]["text"]
    client.query.assert_not_called()


# ---------------------------------------------------------------------------
# count via bc_query forces page_size=1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bc_query_count_forces_page_size_one(tmp_path) -> None:
    result = SiebelQueryResult(items=[{"Id": "1"}], total_count=7, has_more=False, offset=0, page_size=1)
    reg = _reg(
        tmp_path,
        {
            "acct_count": _bcq(
                result="count",
                render="count_prose",
                bind={"business_object": "Account", "business_component": "Account"},
                props={"searchspec": {"type": "string"}},
            )
        },
    )
    session, client = _session(result)
    resp = await reg.call("acct_count", {"searchspec": "[Name] LIKE 'A*'"}, session)
    assert resp["content"][0]["text"] == "Total records: **7**"
    assert client.query.call_args.kwargs["page_size"] == 1


@pytest.mark.asyncio
async def test_bc_query_count_404_empty_reflects_forced_page_size(tmp_path) -> None:
    """A count tool's 404 -> empty result must carry the forced page_size=1
    (and offset=0), matching the old _count_records which set page_size=1 before
    the empty fallback — not the default 20 _empty_result would otherwise use."""
    reg = _reg(
        tmp_path,
        {
            "acct_count_json": _bcq(
                result="count",
                render="count_json",
                bind={"business_object": "Account", "business_component": "Account"},
                props={"searchspec": {"type": "string"}},
            )
        },
    )
    client = AsyncMock()
    client.query = AsyncMock(side_effect=SiebelError("not found", 404, '{"ERROR":"404"}'))
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)

    resp = await reg.call("acct_count_json", {"searchspec": "[Name] LIKE 'A*'"}, session)
    assert resp.get("isError") is not True
    payload = json.loads(resp["content"][0]["text"])
    assert payload["items"] == []
    assert payload["totalCount"] == 0
    assert payload["pageSize"] == 1  # the count path's forced page_size, not 20
    assert payload["offset"] == 0
