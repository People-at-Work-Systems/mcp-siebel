"""Tests for the externalized, hot-reloadable tool-spec JSON.

The spec file (``mcp_siebel/specs/tool_specs.json``) is the single source of
truth for each tool's description, input_schema, ``kind``, and ``handler``
reference. The registry resolves handlers from the spec by ``importlib``
(permitting only ``mcp_siebel.handlers`` modules) and knows no tool names
itself. Shared parameter definitions live under a top-level ``$defs`` block and
are referenced via ``$ref``; the loader inlines every ``$ref`` before exposing
schemas, validates each entry, and checks that every entry has a known ``kind``
and a resolvable ``handler`` on every (re)load — keeping the last valid set on
failure and serving nothing if the first load fails.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from importlib.resources import files

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import _KINDS, ToolRegistry, _resolve_handler, _SpecError


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _bundled_tools() -> dict[str, dict[str, object]]:
    return _bundled_spec()["tools"]  # type: ignore[return-value]


def _bundled_defs() -> dict[str, object]:
    return _bundled_spec().get("$defs", {})  # type: ignore[return-value]


def _entry(
    description: str,
    props: dict[str, object] | None = None,
    required: list[str] | None = None,
    *,
    kind: str = "python",
    handler: str = "mcp_siebel.handlers.records:_aggregate_data",
    enabled: bool = True,
) -> dict[str, object]:
    """A minimal valid spec entry. The default handler points at a real,
    resolvable function so an entry is valid out of the box; tests that need a
    bad kind/handler or a disabled tool override those kwargs. ``enabled`` is
    omitted when None (default true); pass False to hide the tool."""
    schema: dict[str, object] = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    entry: dict[str, object] = {
        "description": description,
        "kind": kind,
        "handler": handler,
        "input_schema": schema,
    }
    entry["enabled"] = enabled
    return entry


def _write_spec(path, tools: dict[str, object], defs: dict[str, object] | None = None) -> None:
    # Always include the bundled $defs (test defs override/extend) so the
    # non-overridden bundled tools — which reference shared $defs — resolve.
    # A spec that should fail must fail because of its intentionally-bad entry,
    # not because every other tool's $ref lost its definition.
    merged = {**_bundled_defs(), **(defs or {})}
    path.write_text(json.dumps({"$defs": merged, "tools": tools}, sort_keys=True, indent=2), encoding="utf-8")


def _registry_with_spec(tmp_path, tools: dict[str, object], defs: dict[str, object] | None = None) -> ToolRegistry:
    spec = tmp_path / "spec.json"
    _write_spec(spec, tools, defs)
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    return ToolRegistry(settings)


def _full_valid_spec() -> dict[str, dict[str, object]]:
    """A deep copy of the bundled spec's tools — a baseline that resolves real
    handlers, so reload/override tests can mutate one tool and still load."""
    return deepcopy(_bundled_tools())


# ---------------------------------------------------------------------------
# Bundled spec integrity
# ---------------------------------------------------------------------------


def test_spec_entries_have_known_kind_and_valid_options() -> None:
    """Every bundled entry declares a known kind whose allowed options accept
    it, and the registry loads them all. The 7 write/delete/external tools are
    ``enabled: false`` in the bundled spec (read-only by default); every other
    tool omits ``enabled`` (default true)."""
    disabled_expected = {
        "create_child_record",
        "create_record",
        "create_record_json",
        "update_record",
        "upload_attachment",
        "delete_record",
        "web_search",
    }
    tools = _bundled_tools()
    for name, entry in tools.items():
        kind = entry["kind"]
        assert kind in _KINDS, f"{name}: unknown kind {kind!r}"
        # python entries resolve a real handler; bc_query entries have none.
        if kind == "python":
            assert entry["handler"].startswith("mcp_siebel.handlers."), f"{name}: handler outside handlers pkg"
            assert callable(_resolve_handler(entry["handler"])), f"{name}: handler not callable"
        else:
            assert "handler" not in entry, f"{name}: non-python kind must not declare a handler"
        if name in disabled_expected:
            assert entry.get("enabled") is False, f"{name}: expected enabled: false"
        else:
            assert entry.get("enabled") is True, f"{name}: expected enabled: true"
    reg = ToolRegistry(AppSettings())
    assert set(reg._tools) == set(tools), "registry didn't load every bundled tool"


def test_spec_refs_are_inlined() -> None:
    """The bundled spec uses $defs/$ref, but the registry serves fully-resolved
    schemas with no $ref left (MCP clients get a plain inputSchema)."""
    reg = ToolRegistry(AppSettings())
    props = reg._tools["query_data"].input_schema["properties"]
    assert "$ref" not in props["business_object"]
    assert "$ref" not in props["page_size"]
    # the shared definition's type and rich description come through
    assert props["page_size"]["type"] == "integer"
    assert props["page_size"]["minimum"] == 1
    assert props["page_size"]["maximum"] == 100
    assert props["offset"]["type"] == "integer"
    assert props["offset"]["minimum"] == 0
    # fields accepts either a list or a comma-separated string (the tool's
    # documented contract); items only constrains the array form.
    assert props["fields"]["type"] == ["array", "string"]
    assert props["fields"].get("items") == {"type": "string"}
    assert "Never pass placeholders" in props["fields"]["description"]


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
        _full_valid_spec()
        | {"query_data": _entry("q", {"business_object": {"$ref": "#/$defs/business_object"}}, ["business_object"])},
        defs,
    )
    prop = reg._tools["query_data"].input_schema["properties"]["business_object"]
    assert prop == {"type": "string", "description": "override BO"}


def test_spec_unsupported_ref_rejected(tmp_path) -> None:
    """An external or non-#$defs $ref aborts the first load — construction raises."""
    with pytest.raises(_SpecError, match="unsupported \\$ref"):
        _registry_with_spec(
            tmp_path,
            _full_valid_spec()
            | {"query_data": _entry("q", {"business_object": {"$ref": "https://example.com/bo"}}, ["business_object"])},
            {},
        )


def test_spec_unknown_ref_rejected(tmp_path) -> None:
    with pytest.raises(_SpecError, match="unknown \\$ref target"):
        _registry_with_spec(
            tmp_path,
            _full_valid_spec()
            | {"query_data": _entry("q", {"business_object": {"$ref": "#/$defs/nope"}}, ["business_object"])},
            {"business_object": {"type": "string"}},
        )


def test_spec_circular_ref_rejected(tmp_path) -> None:
    defs = {
        "a": {"type": "object", "properties": {"b": {"$ref": "#/$defs/b"}}},
        "b": {"type": "object", "properties": {"a": {"$ref": "#/$defs/a"}}},
    }
    with pytest.raises(_SpecError, match="circular \\$ref"):
        _registry_with_spec(
            tmp_path,
            _full_valid_spec()
            | {"query_data": _entry("q", {"business_object": {"$ref": "#/$defs/a"}}, ["business_object"])},
            defs,
        )


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
        _full_valid_spec() | {"query_data": _entry("v1", {"business_object": {"type": "string"}}, ["business_object"])},
    )
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg._tools["query_data"].description == "v1"

    _write_spec(
        spec,
        _full_valid_spec() | {"query_data": _entry("v2", {"business_object": {"type": "string"}}, ["business_object"])},
    )
    _bump_mtime(spec)

    tools = {t["name"]: t for t in reg.list_tools()}
    assert tools["query_data"]["description"] == "v2"


def test_spec_no_reload_when_mtime_unchanged(tmp_path) -> None:
    reg = _registry_with_spec(tmp_path, _full_valid_spec())
    mtime_before = reg._spec_mtime
    reg.list_tools()
    assert reg._spec_mtime == mtime_before


def test_spec_reload_failure_keeps_last_valid(tmp_path) -> None:
    """A good load followed by a broken reload keeps the last valid tools — the
    server never swaps in a half-edited spec."""
    spec = tmp_path / "spec.json"
    _write_spec(
        spec,
        _full_valid_spec()
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
# Coverage gate — every entry needs a known kind + a resolvable handler
# ---------------------------------------------------------------------------


def test_spec_subset_is_accepted_not_mirrored(tmp_path) -> None:
    """There is no handler map to mirror any more: a spec with a subset of the
    bundled tools (including the session tool) simply serves that subset."""
    tools = {
        "query_data": _bundled_tools()["query_data"],
        "authenticate": _bundled_tools()["authenticate"],
    }
    reg = _registry_with_spec(tmp_path, tools)
    assert set(reg._tools) == {"query_data", "authenticate"}
    assert reg._tools["query_data"].description == _bundled_tools()["query_data"]["description"]


def test_spec_unknown_kind_rejected(tmp_path) -> None:
    spec = _full_valid_spec()
    spec["query_data"] = {**spec["query_data"], "kind": "bogus"}
    with pytest.raises(_SpecError, match="unknown kind"):
        _registry_with_spec(tmp_path, spec)


def test_spec_missing_kind_rejected(tmp_path) -> None:
    spec = _full_valid_spec()
    spec["query_data"] = {k: v for k, v in spec["query_data"].items() if k != "kind"}
    with pytest.raises(_SpecError, match="invalid entry for 'query_data'"):
        _registry_with_spec(tmp_path, spec)


def test_spec_missing_handler_rejected(tmp_path) -> None:
    spec = _full_valid_spec()
    spec["aggregate_data"] = {k: v for k, v in spec["aggregate_data"].items() if k != "handler"}
    with pytest.raises(_SpecError, match="requires a 'handler'"):
        _registry_with_spec(tmp_path, spec)


def test_spec_unresolvable_handler_rejected(tmp_path) -> None:
    """A handler pointing at a missing attribute fails the load."""
    spec = _full_valid_spec()
    spec["aggregate_data"] = {**spec["aggregate_data"], "handler": "mcp_siebel.handlers.records:no_such_func"}
    with pytest.raises(_SpecError, match="no attribute 'no_such_func'"):
        _registry_with_spec(tmp_path, spec)


def test_spec_handler_outside_package_rejected(tmp_path) -> None:
    """A real module+attr outside mcp_siebel.handlers is refused — the resolver
    only permits handler-package modules."""
    spec = _full_valid_spec()
    spec["aggregate_data"] = {**spec["aggregate_data"], "handler": "mcp_siebel.siebel_client:SiebelClient"}
    with pytest.raises(_SpecError, match="is not under"):
        _registry_with_spec(tmp_path, spec)


def test_spec_invalid_entry_rejected(tmp_path) -> None:
    """One invalid entry rejects the whole spec (not just that tool)."""
    spec = _full_valid_spec()
    spec["query_data"] = {"description": 123, "input_schema": {"type": "object", "properties": {}}}
    with pytest.raises(_SpecError, match="invalid entry for 'query_data'"):
        _registry_with_spec(tmp_path, spec)


def test_spec_duplicate_tool_name_rejected(tmp_path) -> None:
    """Duplicate tool names under ``tools`` are rejected (name uniqueness)."""
    spec = tmp_path / "spec.json"
    entry = json.dumps(_bundled_tools()["query_data"])
    spec.write_text(
        f'{{"$defs": {{}}, "tools": {{"query_data": {entry}, "query_data": {entry}}}}}',
        encoding="utf-8",
    )
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    with pytest.raises(_SpecError, match="duplicate key 'query_data'"):
        ToolRegistry(settings)


# ---------------------------------------------------------------------------
# Session tool — exactly one entry marks establishes_session; it must be
# enabled. (Zero/two -> load error.)
# ---------------------------------------------------------------------------


def test_spec_zero_session_tools_rejected(tmp_path) -> None:
    """A spec with no establishes_session tool is rejected — exactly one is
    required so the transports have a session-establishment tool to call."""
    with pytest.raises(_SpecError, match="exactly one tool must mark establishes_session"):
        _registry_with_spec(tmp_path, {"query_data": _bundled_tools()["query_data"]})


def test_spec_two_session_tools_rejected(tmp_path) -> None:
    spec = _full_valid_spec()
    spec["query_data"] = {**spec["query_data"], "establishes_session": True}
    with pytest.raises(_SpecError, match="found 2"):
        _registry_with_spec(tmp_path, spec)


def test_spec_session_tool_must_be_enabled(tmp_path) -> None:
    """The session tool must be enabled — disabling it would brick the server
    (no way to establish a session), so the loader rejects it at load time."""
    spec = _full_valid_spec()
    spec["authenticate"] = {**spec["authenticate"], "enabled": False}
    with pytest.raises(_SpecError, match="must be enabled"):
        _registry_with_spec(tmp_path, spec)


def test_spec_session_tool_name_exposed(tmp_path) -> None:
    """The registry exposes the session tool's name so transports need not
    hard-code it."""
    reg = _registry_with_spec(tmp_path, _full_valid_spec())
    assert reg.session_tool_name == "authenticate"


# ---------------------------------------------------------------------------
# First-load failure is fatal — a missing/corrupt/invalid spec aborts startup
# ---------------------------------------------------------------------------


def test_spec_first_load_missing_file_raises(tmp_path) -> None:
    """A missing spec on the very first load aborts construction (non-zero
    startup) instead of silently serving no tools."""
    settings = AppSettings()
    settings.tools_spec_path = str(tmp_path / "does-not-exist.json")
    with pytest.raises(_SpecError, match="tool spec file unavailable"):
        ToolRegistry(settings)


def test_spec_first_load_corrupt_json_raises(tmp_path) -> None:
    spec = tmp_path / "bad.json"
    spec.write_text("{not valid json", encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    with pytest.raises(_SpecError, match="invalid JSON"):
        ToolRegistry(settings)


def test_spec_first_load_error_names_the_entry(tmp_path) -> None:
    """The fatal first-load error names the offending entry and rule."""
    spec = _full_valid_spec()
    spec["query_data"] = {**spec["query_data"], "kind": "bogus"}
    with pytest.raises(_SpecError, match="query_data") as exc_info:
        _registry_with_spec(tmp_path, spec)
    assert "unknown kind" in str(exc_info.value)
