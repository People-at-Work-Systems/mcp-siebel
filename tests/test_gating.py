"""Read-only by default via the per-tool ``enabled`` flag in the spec.

The bundled ``tool_specs.json`` sets ``"enabled": false`` on its 7
write/delete/external tools; every other tool omits ``enabled`` (default
true). The registry hides disabled tools from ``list_tools()`` and refuses
them in ``call()``. Enabling a tool is done by flipping ``"enabled": true``
on its entry in an override spec (``SIEBEL_TOOLS_SPEC_PATH``) — no env var.
See ``tool_specs.json`` and ``ToolRegistry``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import SessionContext, ToolRegistry, _SpecError

# The 7 tools the bundled spec disables (read-only by default). ``external``
# covers tools that send free text to an outside service (web_search) — gated
# off by default so customer data can't leak into a query.
DISABLED_BY_DEFAULT = {
    "create_record",
    "create_record_json",
    "update_record",
    "create_child_record",
    "upload_attachment",
    "delete_record",
    "web_search",
}


def _names(reg: ToolRegistry) -> set[str]:
    return {t["name"] for t in reg.list_tools()}


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _bundled_tools() -> dict[str, dict[str, object]]:
    return _bundled_spec()["tools"]  # type: ignore[return-value]


def _registry_with_spec(tmp_path, tools: dict[str, object]) -> ToolRegistry:
    """Registry backed by a spec whose ``tools`` map is ``tools`` (the bundled
    $defs are merged in so $refs resolve)."""
    spec = deepcopy(_bundled_spec())
    spec["tools"] = tools  # type: ignore[assignment]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(path)
    return ToolRegistry(settings)


def _override_one(tmp_path, name: str, **changes) -> ToolRegistry:
    """Registry backed by the bundled spec with one entry overridden."""
    tools = deepcopy(_bundled_tools())
    tools[name] = {**tools[name], **changes}  # type: ignore[index]
    return _registry_with_spec(tmp_path, tools)


# --- bundled spec is read-only by default -----------------------------------


def test_list_tools_hides_disabled_tools_by_default() -> None:
    reg = ToolRegistry(AppSettings())
    names = _names(reg)
    assert not (names & DISABLED_BY_DEFAULT), f"disabled tools visible: {names & DISABLED_BY_DEFAULT}"
    # the 19 enabled tools are present, including the session tool.
    assert "authenticate" in names
    assert "query_data" in names
    assert len(names) == 19


def test_bundled_spec_marks_the_seven_disabled() -> None:
    """The bundled spec itself sets enabled: false on exactly the 7 tools."""
    tools = _bundled_tools()
    disabled = {n for n, e in tools.items() if e.get("enabled") is False}
    assert disabled == DISABLED_BY_DEFAULT


# --- flipping enabled: true in an override spec exposes a tool ---------------


def test_override_enabled_true_exposes_tool(tmp_path) -> None:
    """Flipping create_record to enabled: true in an override spec exposes it."""
    reg = _override_one(tmp_path, "create_record", enabled=True)
    assert "create_record" in _names(reg)
    # the other 6 stay disabled.
    assert not (_names(reg) & (DISABLED_BY_DEFAULT - {"create_record"}))


def test_override_enabled_true_exposes_external_tool(tmp_path) -> None:
    reg = _override_one(tmp_path, "web_search", enabled=True)
    assert "web_search" in _names(reg)


def test_omitting_enabled_defaults_true(tmp_path) -> None:
    """An entry that omits enabled is enabled by default — so deleting the
    enabled: false key from a disabled tool exposes it."""
    tools = deepcopy(_bundled_tools())
    tools["delete_record"] = {k: v for k, v in tools["delete_record"].items() if k != "enabled"}  # type: ignore[union-attr]
    reg = _registry_with_spec(tmp_path, tools)
    assert "delete_record" in _names(reg)


# --- disabled tools are refused at call() ------------------------------------


@pytest.mark.asyncio
async def test_call_refuses_disabled_write_tool() -> None:
    """A disabled tool must be refused at call() before any Siebel I/O."""
    reg = ToolRegistry(AppSettings())
    result = await reg.call("create_record", {"data": {"Last Name": "x"}}, None)
    assert result.get("isError"), f"expected disabled error, got: {result}"
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    assert "disabled" in text.lower()


@pytest.mark.asyncio
async def test_call_refuses_disabled_delete_tool() -> None:
    reg = ToolRegistry(AppSettings())
    result = await reg.call("delete_record", {}, None)
    assert result.get("isError")


@pytest.mark.asyncio
async def test_call_refuses_disabled_external_tool() -> None:
    reg = ToolRegistry(AppSettings())
    result = await reg.call("web_search", {"query": "x"}, None)
    assert result.get("isError"), f"expected disabled error, got: {result}"
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    assert "disabled" in text.lower()


@pytest.mark.asyncio
async def test_call_runs_tool_once_enabled(tmp_path) -> None:
    """With enabled: true in an override spec, call() runs the handler (which
    surfaces its own error rather than a disabled refusal)."""
    reg = _override_one(tmp_path, "web_search", enabled=True)
    result = await reg.call("web_search", {"query": "x"}, SessionContext(session_id="s1"))
    assert result.get("isError")
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    assert "disabled" not in text.lower()


# --- a new tool added via JSON is gated by its enabled flag ------------------


_BULK_CREATE: dict[str, object] = {
    "description": "A NEW write tool added via the spec only.",
    "kind": "bc_upsert",
    "operation": "create",
    "scope": "root",
    "render": "create_prose",
    "input_schema": {
        "type": "object",
        "properties": {"business_object": {"type": "string"}},
        "required": ["business_object"],
    },
}


def test_new_tool_enabled_by_default(tmp_path) -> None:
    """A new tool with no enabled key is enabled by default — no env var
    needed."""
    tools = deepcopy(_bundled_tools())
    tools["bulk_create"] = _BULK_CREATE  # type: ignore[index]
    reg = _registry_with_spec(tmp_path, tools)
    assert "bulk_create" in _names(reg)


def test_new_tool_hidden_when_enabled_false(tmp_path) -> None:
    tools = deepcopy(_bundled_tools())
    tools["bulk_create"] = {**_BULK_CREATE, "enabled": False}  # type: ignore[index]
    reg = _registry_with_spec(tmp_path, tools)
    assert "bulk_create" not in _names(reg)


@pytest.mark.asyncio
async def test_new_tool_call_refused_when_disabled(tmp_path) -> None:
    tools = deepcopy(_bundled_tools())
    tools["bulk_create"] = {**_BULK_CREATE, "enabled": False}  # type: ignore[index]
    reg = _registry_with_spec(tmp_path, tools)
    result = await reg.call("bulk_create", {"business_object": "Account"}, None)
    assert result.get("isError")
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    assert "disabled" in text.lower()


# --- the session tool must be enabled ---------------------------------------


def test_session_tool_must_be_enabled(tmp_path) -> None:
    """Disabling the session tool is rejected at load — it would brick the
    server (no way to establish a session)."""
    with pytest.raises(_SpecError, match="must be enabled"):
        _override_one(tmp_path, "authenticate", enabled=False)
