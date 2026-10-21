"""Finding 6: ``ToolRegistry.call`` validates ``arguments`` against the tool's
resolved ``input_schema`` (a ``Draft202012Validator`` built per tool at load
time) before dispatch, returning a specific param/expected/received error instead
of the internal mask or forwarding bad args to the executor.

Also covers the undeclared-bound-key path of finding 3 (additionalProperties:false
rejects a caller-supplied bound key on a pinned tool).
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelQueryResult
from mcp_siebel.tools import SessionContext, ToolRegistry


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _reg(tmp_path, extra: dict[str, dict[str, object]] | None = None) -> ToolRegistry:
    spec = deepcopy(_bundled_spec())
    if extra:
        spec["tools"].update(extra)  # type: ignore[union-attr]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(path)
    return ToolRegistry(settings)


def _session(result: SiebelQueryResult | None = None) -> tuple[SessionContext, AsyncMock]:
    client = AsyncMock()
    if result is not None:
        client.query = AsyncMock(return_value=result)
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    return SessionContext(session_id="s1", username="t", client=client, cache=cache), client


def _text(resp: dict[str, object]) -> str:
    return resp["content"][0]["text"]  # type: ignore[index]


# --------------------------------------------------------------------------- #
# query_data against the bundled schema (additionalProperties:false, bounds)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_missing_required_names_the_param(tmp_path) -> None:
    reg = _reg(tmp_path)
    resp = await reg.call("query_data", {}, None)
    assert resp.get("isError") is True
    assert "business_object" in _text(resp)
    assert "required" in _text(resp)


@pytest.mark.asyncio
async def test_wrong_type_page_size(tmp_path) -> None:
    reg = _reg(tmp_path)
    resp = await reg.call("query_data", {"business_object": "Account", "page_size": "abc"}, None)
    assert resp.get("isError") is True
    text = _text(resp)
    assert "page_size" in text
    assert "integer" in text


@pytest.mark.asyncio
async def test_page_size_above_max(tmp_path) -> None:
    reg = _reg(tmp_path)
    resp = await reg.call("query_data", {"business_object": "Account", "page_size": 999}, None)
    assert resp.get("isError") is True
    text = _text(resp)
    assert "page_size" in text
    assert "100" in text  # the maximum


@pytest.mark.asyncio
async def test_page_size_below_min(tmp_path) -> None:
    reg = _reg(tmp_path)
    resp = await reg.call("query_data", {"business_object": "Account", "page_size": 0}, None)
    assert resp.get("isError") is True
    assert "page_size" in _text(resp)
    assert "minimum" in _text(resp)


@pytest.mark.asyncio
async def test_offset_below_min(tmp_path) -> None:
    reg = _reg(tmp_path)
    resp = await reg.call("query_data", {"business_object": "Account", "offset": -1}, None)
    assert resp.get("isError") is True
    assert "offset" in _text(resp)
    assert "minimum" in _text(resp)


@pytest.mark.asyncio
async def test_unknown_property_rejected(tmp_path) -> None:
    """An undeclared top-level property is rejected by additionalProperties:false."""
    reg = _reg(tmp_path)
    resp = await reg.call(
        "query_data", {"business_object": "Account", "not_a_real_param": "x"}, None
    )
    assert resp.get("isError") is True
    assert "not_a_real_param" in _text(resp)


# --------------------------------------------------------------------------- #
# Valid args pass through to the handler
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_valid_args_reach_handler(tmp_path) -> None:
    reg = _reg(tmp_path)
    session, client = _session(
        SiebelQueryResult(items=[{"Id": "1", "Name": "Acme"}], total_count=1, has_more=False, offset=0, page_size=20)
    )
    resp = await reg.call(
        "query_data",
        {"business_object": "Account", "page_size": 20, "offset": 0},
        session,
    )
    assert resp.get("isError") is not True, resp
    client.query.assert_called_once()


# --------------------------------------------------------------------------- #
# A caller-supplied bound key on a pinned tool is an unknown property (finding 3+6)
# --------------------------------------------------------------------------- #


def _pinned_query_entry() -> dict[str, object]:
    return {
        "description": "pinned query tool",
        "kind": "bc_query",
        "scope": "root",
        "result": "records",
        "render": "query_prose",
        "enabled": True,
        "bind": {"business_component": "Account"},
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"business_object": {"type": "string"}},
            "required": ["business_object"],
        },
    }


@pytest.mark.asyncio
async def test_caller_bound_key_on_pinned_tool_rejected(tmp_path) -> None:
    reg = _reg(tmp_path, {"query_data": _pinned_query_entry()})
    session, client = _session(
        SiebelQueryResult(items=[{"Id": "1"}], total_count=1, has_more=False, offset=0, page_size=20)
    )
    resp = await reg.call(
        "query_data", {"business_object": "Account", "business_component": "Evil"}, session
    )
    assert resp.get("isError") is True
    assert "business_component" in _text(resp)
    client.query.assert_not_called()
