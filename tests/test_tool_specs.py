"""Tests for the externalized, hot-reloadable tool-spec JSON.

The spec file (``mcp_siebel/specs/tool_specs.json``) is the single source of
truth for each tool's description and input_schema at runtime; the registry knows
only the handlers (``_build_handlers()``). Shared parameter definitions live
under a top-level ``$defs`` block and are referenced via ``$ref``; the loader
inlines every ``$ref`` before exposing schemas, validates, and runs a
handler↔spec coverage check on every (re)load — keeping the last valid set on
failure and degrading only if the first load fails.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import ToolRegistry, _build_handlers


def _degraded_description(name: str) -> str:
    return f"Tool '{name}' — spec unavailable (tool_specs.json missing or invalid)."


def _entry(
    description: str, props: dict[str, object] | None = None, required: list[str] | None = None
) -> dict[str, object]:
    schema: dict[str, object] = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return {"description": description, "input_schema": schema}


def _write_spec(path, tools: dict[str, object], defs: dict[str, object] | None = None) -> None:
    path.write_text(json.dumps({"$defs": defs or {}, "tools": tools}), encoding="utf-8")


def _registry_with_spec(tmp_path, tools: dict[str, object], defs: dict[str, object] | None = None) -> ToolRegistry:
    spec = tmp_path / "spec.json"
    _write_spec(spec, tools, defs)
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    return ToolRegistry(settings)


# A spec that covers every handler with a minimal valid entry — the baseline for
# reload/coverage tests so the coverage gate passes.
def _full_minimal_spec() -> dict[str, object]:
    return {name: _entry(f"{name} tool", {"business_object": {"type": "string"}}) for name in _build_handlers()}


# ---------------------------------------------------------------------------
# Bundled spec integrity
# ---------------------------------------------------------------------------


def test_spec_json_covers_all_handlers() -> None:
    """The bundled spec's tool set exactly matches the handler set — so adding a
    handler without a spec entry (or a stale spec entry) is caught at load time.
    Value equality is NOT asserted, so editing descriptions in the JSON is fine."""
    spec = json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))
    handler_names = set(_build_handlers())
    spec_names = set(spec["tools"])
    assert spec_names == handler_names, {
        "handlers without a spec entry": handler_names - spec_names,
        "spec entries without a handler": spec_names - handler_names,
    }


def test_spec_refs_are_inlined() -> None:
    """The bundled spec uses $defs/$ref, but the registry serves fully-resolved
    schemas with no $ref left (MCP clients get a plain inputSchema)."""
    reg = ToolRegistry(AppSettings())
    props = reg._tools["query_data"].input_schema["properties"]
    assert "$ref" not in props["business_object"]
    assert "$ref" not in props["page_size"]
    # the shared definition's type and rich description come through
    assert props["page_size"]["type"] == "integer"
    assert props["fields"]["type"] == "array"
    assert props["fields"].get("items") == {"type": "string"}
    assert "NEVER pass placeholder" in props["fields"]["description"]


def test_spec_required_preserved() -> None:
    reg = ToolRegistry(AppSettings())
    assert reg._tools["query_data"].input_schema["required"] == ["business_object"]
    assert reg._tools["query_child_records"].input_schema["required"] == [
        "business_object",
        "parent_id",
        "child_business_component",
    ]


def test_every_served_property_has_concrete_type() -> None:
    """Every property of every served tool carries a concrete JSON Schema ``type``
    and no ``$ref`` — the Claude-Desktop requirement, enforced post-deref."""
    reg = ToolRegistry(AppSettings())
    for tool in reg._tools.values():
        for prop in tool.input_schema.get("properties", {}).values():
            assert "type" in prop, f"{tool.name}: property without a type: {prop}"
            assert "$ref" not in prop, f"{tool.name}: unresolved $ref: {prop}"


# ---------------------------------------------------------------------------
# $defs / $ref resolution
# ---------------------------------------------------------------------------


def test_spec_defs_resolved_from_override(tmp_path) -> None:
    """An override spec's $defs/$ref are inlined into the served schema."""
    defs = {"business_object": {"type": "string", "description": "override BO"}}
    reg = _registry_with_spec(
        tmp_path,
        _full_minimal_spec()
        | {"query_data": _entry("q", {"business_object": {"$ref": "#/$defs/business_object"}}, ["business_object"])},
        defs,
    )
    prop = reg._tools["query_data"].input_schema["properties"]["business_object"]
    assert prop == {"type": "string", "description": "override BO"}


def test_spec_unsupported_ref_rejected(tmp_path) -> None:
    """An external or non-#$defs $ref fails the load (degraded, since first load)."""
    reg = _registry_with_spec(
        tmp_path,
        _full_minimal_spec()
        | {"query_data": _entry("q", {"business_object": {"$ref": "https://example.com/bo"}}, ["business_object"])},
        {},
    )
    assert reg._tools["query_data"].description == _degraded_description("query_data")


def test_spec_unknown_ref_rejected(tmp_path) -> None:
    reg = _registry_with_spec(
        tmp_path,
        _full_minimal_spec()
        | {"query_data": _entry("q", {"business_object": {"$ref": "#/$defs/nope"}}, ["business_object"])},
        {"business_object": {"type": "string"}},
    )
    assert reg._tools["query_data"].description == _degraded_description("query_data")


def test_spec_circular_ref_rejected(tmp_path) -> None:
    defs = {
        "a": {"type": "object", "properties": {"b": {"$ref": "#/$defs/b"}}},
        "b": {"type": "object", "properties": {"a": {"$ref": "#/$defs/a"}}},
    }
    reg = _registry_with_spec(
        tmp_path,
        _full_minimal_spec()
        | {"query_data": _entry("q", {"business_object": {"$ref": "#/$defs/a"}}, ["business_object"])},
        defs,
    )
    assert reg._tools["query_data"].description == _degraded_description("query_data")


# ---------------------------------------------------------------------------
# Hot reload by mtime
# ---------------------------------------------------------------------------


def _bump_mtime(path, delta_ns: int = 1_000_000_000) -> None:
    prev = os.stat(path).st_mtime_ns
    os.utime(path, ns=(prev + delta_ns, prev + delta_ns))


def test_spec_hot_reload(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        _full_minimal_spec()
        | {"query_data": _entry("v1", {"business_object": {"type": "string"}}, ["business_object"])},
    )
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg._tools["query_data"].description == "v1"

    _write_spec(
        spec,
        _full_minimal_spec()
        | {"query_data": _entry("v2", {"business_object": {"type": "string"}}, ["business_object"])},
    )
    _bump_mtime(spec)

    tools = {t["name"]: t for t in reg.list_tools()}
    assert tools["query_data"]["description"] == "v2"


def test_spec_no_reload_when_mtime_unchanged(tmp_path) -> None:
    reg = _registry_with_spec(tmp_path, _full_minimal_spec())
    mtime_before = reg._spec_mtime
    reg.list_tools()
    assert reg._spec_mtime == mtime_before


def test_spec_reload_failure_keeps_last_valid(tmp_path) -> None:
    """A good load followed by a broken reload keeps the last valid tools — the
    server never swaps in a half-edited spec."""
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        _full_minimal_spec()
        | {"query_data": _entry("good", {"business_object": {"type": "string"}}, ["business_object"])},
    )
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg._tools["query_data"].description == "good"

    # Corrupt the file and bump mtime so the reload is attempted.
    spec.write_text("{not valid json", encoding="utf-8")
    _bump_mtime(spec)
    reg.list_tools()
    assert reg._tools["query_data"].description == "good"  # last valid set kept


# ---------------------------------------------------------------------------
# Coverage gate — a spec that doesn't exactly match the handler set is rejected
# ---------------------------------------------------------------------------


def test_spec_missing_handler_entry_rejected(tmp_path) -> None:
    """A spec missing an entry for a handler fails the coverage check (degraded,
    since first load)."""
    spec_obj = _full_minimal_spec()
    del spec_obj["query_data"]
    reg = _registry_with_spec(tmp_path, spec_obj)
    assert reg._tools["query_data"].description == _degraded_description("query_data")


def test_spec_orphan_entry_rejected(tmp_path) -> None:
    """A spec entry with no matching handler fails the coverage check."""
    reg = _registry_with_spec(tmp_path, _full_minimal_spec() | {"no_such_handler": _entry("x", {})})
    assert "no_such_handler" not in reg._tools
    # first load failed → every handler is degraded
    assert reg._tools["query_data"].description == _degraded_description("query_data")


def test_spec_invalid_entry_rejected(tmp_path) -> None:
    """One invalid entry rejects the whole spec (not just that tool)."""
    spec_obj = _full_minimal_spec()
    spec_obj["query_data"] = {"description": 123, "input_schema": {"type": "object", "properties": {}}}
    reg = _registry_with_spec(tmp_path, spec_obj)
    assert reg._tools["query_data"].description == _degraded_description("query_data")


def test_spec_duplicate_tool_name_rejected(tmp_path) -> None:
    """Duplicate tool names under ``tools`` are rejected (name uniqueness)."""
    spec = tmp_path / "spec.json"
    # Raw JSON with a duplicate key — can't be expressed via a Python dict.
    entry = json.dumps(_entry("a", {"business_object": {"type": "string"}}))
    spec.write_text(
        f'{{"$defs": {{}}, "tools": {{"query_data": {entry}, "query_data": {entry}}}}}',
        encoding="utf-8",
    )
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg._tools["query_data"].description == _degraded_description("query_data")


# ---------------------------------------------------------------------------
# First-load fallback — a missing/corrupt spec degrades but still starts
# ---------------------------------------------------------------------------


def test_spec_fallback_on_missing_file(tmp_path) -> None:
    settings = AppSettings()
    settings.tools_spec_path = str(tmp_path / "does-not-exist.json")
    reg = ToolRegistry(settings)
    names = {t["name"] for t in reg.list_tools()}
    assert "query_data" in names
    assert reg._tools["query_data"].input_schema["properties"] == {}
    assert reg._tools["query_data"].description == _degraded_description("query_data")


def test_spec_fallback_on_corrupt_json(tmp_path) -> None:
    spec = tmp_path / "bad.json"
    spec.write_text("{not valid json", encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    names = {t["name"] for t in reg.list_tools()}
    assert "query_data" in names
    assert reg._tools["query_data"].description == _degraded_description("query_data")
