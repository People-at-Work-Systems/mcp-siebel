"""Finding 3: a spec-pinned ``bind`` key wins over a caller-supplied value.

The executors previously merged ``{**bind, **args}`` so a caller could override
a pinned bind key. The merge is now ``{**args, **bind}`` so the spec wins. Bound
keys are also disjoint from ``input_schema.properties`` (enforced at load), so
finding 6's ``additionalProperties: false`` rejects a caller-supplied bound key
as an unknown property — the pinned value is the only one that can reach Siebel.
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


def _pinned_query_entry() -> dict[str, object]:
    """A bc_query tool pinned to ``business_component: Account`` via bind. The
    bound key must be disjoint from input_schema.properties, so business_component
    is NOT a declared property — a caller supplying it is rejected by
    additionalProperties:false, and even via a direct merge the spec bind wins."""
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


def _reg(tmp_path, extra: dict[str, dict[str, object]]) -> ToolRegistry:
    spec = deepcopy(_bundled_spec())
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


@pytest.mark.asyncio
async def test_caller_supplied_bound_key_rejected_as_unknown(tmp_path) -> None:
    """A caller passing the pinned key (business_component) is rejected by
    additionalProperties:false — the key isn't a declared property."""
    reg = _reg(tmp_path, {"query_data": _pinned_query_entry()})
    session, client = _session(
        SiebelQueryResult(items=[{"Id": "1"}], total_count=1, has_more=False, offset=0, page_size=20)
    )
    resp = await reg.call(
        "query_data", {"business_object": "Account", "business_component": "SecretBC"}, session
    )
    assert resp.get("isError") is True
    assert "business_component" in resp["content"][0]["text"]
    client.query.assert_not_called()


@pytest.mark.asyncio
async def test_pinned_bind_value_reaches_siebel(tmp_path) -> None:
    """With no caller override of the bound key, the pinned value (Account)
    reaches the Siebel client — not a caller value and not the BC default."""
    reg = _reg(tmp_path, {"query_data": _pinned_query_entry()})
    session, client = _session(
        SiebelQueryResult(items=[{"Id": "1", "Name": "Acme"}], total_count=1, has_more=False, offset=0, page_size=20)
    )
    resp = await reg.call("query_data", {"business_object": "Account"}, session)
    assert resp.get("isError") is not True, resp
    # client.query(resolved_bo, bc, ...) — bc is the 2nd positional argument.
    bc = client.query.call_args.args[1]
    assert bc == "Account"
