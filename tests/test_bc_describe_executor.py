"""Unit tests for the ``bc_describe`` kind: load-time option validation, the
three sources (business_objects / bc_descriptor / repository), and the
SBL-EAI-50257 child-BC fallback on the bc_descriptor path — exercised with
test-only fixture specs and SchemaCache / SiebelClient mocks.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelBCDescriptor, SiebelError, SiebelFieldDescriptor
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


def _describe(
    *,
    source: str = "bc_descriptor",
    render: str = "describe_bc_prose",
    props: dict[str, object] | None = None,
    required: list[str] | None = None,
    bind: dict[str, object] | None = None,
    enabled: bool = True,
    bo_schema: bool = False,
) -> dict[str, object]:
    if bo_schema:
        props = props or {"business_object": {"type": "string"}, "business_component": {"type": "string"}}
        required = required or ["business_object"]
    entry: dict[str, object] = {
        "description": "fixture bc_describe tool",
        "kind": "bc_describe",
        "source": source,
        "render": render,
        "input_schema": {"type": "object", "properties": props or {}},
    }
    if required:
        entry["input_schema"]["required"] = required  # type: ignore[index]
    if bind is not None:
        entry["bind"] = bind
    entry["enabled"] = enabled
    return entry


def _reg(tmp_path, extra: dict[str, dict[str, object]]) -> ToolRegistry:
    settings = AppSettings()
    settings.tools_spec_path = _spec_path(tmp_path, extra)
    return ToolRegistry(settings)


def _cache(
    *,
    business_objects: list[str] | None = None,
    resolve: str | None | object = "Account",
    descriptor: SiebelBCDescriptor | None = None,
    descriptor_side_effect: object | None = None,
) -> AsyncMock:
    cache = AsyncMock()
    bos = business_objects if business_objects is not None else ["Account", "Contact"]
    cache.get_business_objects = AsyncMock(return_value=bos)
    if isinstance(resolve, dict):
        cache.resolve_business_object = AsyncMock(side_effect=lambda name: resolve.get(name))
    else:
        cache.resolve_business_object = AsyncMock(return_value=resolve)
    if descriptor_side_effect is not None:
        cache.get_descriptor = AsyncMock(side_effect=descriptor_side_effect)
    else:
        cache.get_descriptor = AsyncMock(return_value=descriptor)
    return cache


def _client(*, repo_fields: list | None = None, repo_side_effect: object | None = None) -> AsyncMock:
    client = AsyncMock()
    if repo_side_effect is not None:
        client.describe_repository_bc = AsyncMock(side_effect=repo_side_effect)
    else:
        client.describe_repository_bc = AsyncMock(return_value=repo_fields or [])
    return client


def _auth(cache: AsyncMock, client: AsyncMock | None = None) -> SessionContext:
    return SessionContext(session_id="s1", username="t", client=client or _client(), cache=cache)


def _no_auth() -> SessionContext:
    return SessionContext(session_id="s1", username="t")


# ---------------------------------------------------------------------------
# Load-time validation
# ---------------------------------------------------------------------------


def test_describe_invalid_source_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="invalid source"):
        _reg(tmp_path, {"x": _describe(source="bogus")})


def test_describe_render_wrong_family_for_source_rejected(tmp_path) -> None:
    # business_objects source must use a 'list'-family render, not 'describe'.
    with pytest.raises(_SpecError, match="family 'describe'"):
        _reg(tmp_path, {"x": _describe(source="business_objects", render="describe_bc_prose")})


def test_describe_bc_descriptor_rejects_list_render(tmp_path) -> None:
    with pytest.raises(_SpecError, match="family 'list'"):
        _reg(tmp_path, {"x": _describe(source="bc_descriptor", render="list_prose")})


def test_describe_bc_descriptor_requires_business_object(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be bound or listed in input_schema.required"):
        _reg(tmp_path, {"x": _describe(source="bc_descriptor")})


def test_describe_business_objects_needs_no_business_object(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(source="business_objects", render="list_prose")})
    assert "x" in reg._tools


def test_describe_unknown_bind_key_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="not known executor parameters"):
        _reg(tmp_path, {"x": _describe(bind={"bogus": "x"})})


def test_describe_bind_business_object_wrong_type_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="business_object.*string"):
        _reg(tmp_path, {"x": _describe(bind={"business_object": 123})})


# ---------------------------------------------------------------------------
# business_objects source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_business_objects_prose(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(source="business_objects", render="list_prose")})
    resp = await reg.call("x", {}, _auth(_cache(business_objects=["Account", "Contact"])))
    assert resp["content"][0]["text"] == "Available Business Objects (2):\n  - Account\n  - Contact"


@pytest.mark.asyncio
async def test_describe_business_objects_json(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(source="business_objects", render="list_json")})
    resp = await reg.call("x", {}, _auth(_cache(business_objects=["Account", "Contact"])))
    assert json.loads(resp["content"][0]["text"]) == {"business_objects": ["Account", "Contact"]}


# ---------------------------------------------------------------------------
# bc_descriptor source + the SBL-EAI-50257 fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_bc_descriptor_prose(tmp_path) -> None:
    d = SiebelBCDescriptor("Account", "Account", [SiebelFieldDescriptor("Name", "Text")])
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_prose", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account"}, _auth(_cache(descriptor=d)))
    assert resp["content"][0]["text"] == "Account / Account\n\n  - Name: Text\n\nTotal fields: 1"


@pytest.mark.asyncio
async def test_describe_bc_descriptor_json(tmp_path) -> None:
    d = SiebelBCDescriptor("Account", "Account", [SiebelFieldDescriptor("Name", "Text", max_length=30)])
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_json", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account"}, _auth(_cache(descriptor=d)))
    assert json.loads(resp["content"][0]["text"]) == {
        "business_object": "Account",
        "business_component": "Account",
        "fields": [{"name": "Name", "type": "Text", "required": False, "read_only": False, "max_length": 30}],
    }


@pytest.mark.asyncio
async def test_describe_bc_descriptor_50257_fallback_prose(tmp_path) -> None:
    own = SiebelBCDescriptor("Contact", "Contact", [SiebelFieldDescriptor("Last Name", "Text")])
    cache = _cache(
        resolve={"Account": "Account", "Contact": "Contact"},
        descriptor_side_effect=[SiebelError("not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}'), own],
    )
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_prose", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account", "business_component": "Contact"}, _auth(cache))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "Contact / Contact\n\n  - Last Name: Text\n\nTotal fields: 1"


@pytest.mark.asyncio
async def test_describe_bc_descriptor_50257_fallback_json(tmp_path) -> None:
    own = SiebelBCDescriptor("Contact", "Contact", [SiebelFieldDescriptor("Last Name", "Text", max_length=50)])
    cache = _cache(
        resolve={"Account": "Account", "Contact": "Contact"},
        descriptor_side_effect=[SiebelError("not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}'), own],
    )
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_json", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account", "business_component": "Contact"}, _auth(cache))
    assert resp.get("isError") is not True
    assert json.loads(resp["content"][0]["text"]) == {
        "business_object": "Contact",
        "business_component": "Contact",
        "fields": [{"name": "Last Name", "type": "Text", "required": False, "read_only": False, "max_length": 50}],
    }


@pytest.mark.asyncio
async def test_describe_bc_descriptor_50257_no_own_bo_returns_error(tmp_path) -> None:
    """If the BC's own BO can't be resolved (or equals the parent), the original
    50257 is surfaced as a masked error — no fallback."""
    cache = _cache(
        resolve={"Account": "Account", "Contact": None},
        descriptor_side_effect=[SiebelError("not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}')],
    )
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_prose", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account", "business_component": "Contact"}, _auth(cache))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_describe_bc_descriptor_non_50257_error_is_masked(tmp_path) -> None:
    cache = _cache(descriptor_side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_prose", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account"}, _auth(cache))
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


@pytest.mark.asyncio
async def test_describe_bc_descriptor_bo_not_found_prose_is_text(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_prose", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Nope"}, _auth(_cache(resolve=None)))
    assert resp.get("isError") is not True  # prose: _text
    assert resp["content"][0]["text"] == (
        'Business Object "Nope" not found. Use list_business_objects to see available options.'
    )


@pytest.mark.asyncio
async def test_describe_bc_descriptor_bo_not_found_json_is_error(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(render="describe_bc_json", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Nope"}, _auth(_cache(resolve=None)))
    assert resp.get("isError") is True  # json: _error
    assert resp["content"][0]["text"] == (
        'Business Object "Nope" not found. Use list_business_objects to see available options.'
    )


# ---------------------------------------------------------------------------
# repository source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_repository_prose(tmp_path) -> None:
    repo = [
        {"Name": "Status", "Type": "Text"},
        {"Name": "Name", "Type": "Text", "Length": 30, "Required": "Y"},
    ]
    reg = _reg(tmp_path, {"x": _describe(source="repository", render="repository_prose", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account"}, _auth(_cache(), _client(repo_fields=repo)))
    assert resp["content"][0]["text"] == (
        "Repository metadata: Account / Account\nTotal fields: 2\n\n  Name (Text)  [len=30, Required]\n  Status (Text)"
    )


@pytest.mark.asyncio
async def test_describe_repository_empty_fields_prose(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(source="repository", render="repository_prose", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account"}, _auth(_cache(), _client(repo_fields=[])))
    assert resp["content"][0]["text"] == (
        "No repository metadata found for Account/Account. "
        "The workspace metadata API may not be accessible. "
        "Use describe_business_object for the REST field list instead."
    )


@pytest.mark.asyncio
async def test_describe_repository_json(tmp_path) -> None:
    repo = [{"Name": "Name", "Type": "Text", "Length": 30, "Required": "Y"}]
    reg = _reg(tmp_path, {"x": _describe(source="repository", render="repository_json", bo_schema=True)})
    resp = await reg.call("x", {"business_object": "Account"}, _auth(_cache(), _client(repo_fields=repo)))
    assert json.loads(resp["content"][0]["text"]) == {
        "business_object": "Account",
        "business_component": "Account",
        "fields": [
            {
                "name": "Name", "type": "Text", "lov_type": "", "required": True,
                "read_only": False, "calculated": False, "max_length": 30,
            }
        ],
    }


@pytest.mark.asyncio
async def test_describe_repository_siebel_error_is_masked(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(source="repository", render="repository_prose", bo_schema=True)})
    resp = await reg.call(
        "x",
        {"business_object": "Account"},
        _auth(_cache(), _client(repo_side_effect=SiebelError("boom", 500, '{"ERROR":"x"}'))),
    )
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == _MASKED


# ---------------------------------------------------------------------------
# unauthenticated session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_unauthenticated_returns_error(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _describe(source="business_objects", render="list_prose")})
    resp = await reg.call("x", {}, _no_auth())
    assert resp.get("isError") is True
    assert resp["content"][0]["text"] == "Not authenticated. Call the authenticate tool first."
