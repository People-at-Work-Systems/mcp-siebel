"""Tests for the externalized, hot-reloadable tool-spec JSON.

The spec file (``mcp_siebel/specs/tool_specs.json``) is the single source of
truth for each tool's description and input_schema at runtime; the registry knows
only the handlers (``_build_handlers()``). These pin: the bundled file covers
every handler, the registry actually loads it (typed schemas reach the client),
mtime hot-reload works, and a missing / corrupt / invalid / orphaned spec yields
a degraded placeholder (tool still callable) rather than crashing.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import ToolRegistry, _build_handlers


def _registry_with_spec(tmp_path, spec_obj) -> ToolRegistry:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(spec_obj), encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    return ToolRegistry(settings)


def _degraded_description(name: str) -> str:
    return f"Tool '{name}' — spec unavailable (tool_specs.json missing or invalid)."


# ---------------------------------------------------------------------------
# Bundled spec integrity
# ---------------------------------------------------------------------------


def test_spec_json_covers_all_handlers() -> None:
    """Every handler in _build_handlers() has an entry in the bundled spec — so
    adding a Python tool without regenerating the spec is caught. Value equality
    is NOT asserted, so editing descriptions in the JSON is allowed."""
    spec = json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))
    missing = [name for name in _build_handlers() if name not in spec]
    assert not missing, f"handlers without a spec entry: {missing}"


def test_spec_loaded_from_json() -> None:
    """The registry serves the typed schema from the spec file, not a degraded
    placeholder (page_size/fields are typed in the spec)."""
    reg = ToolRegistry(AppSettings())
    props = reg._tools["query_data"].input_schema["properties"]
    assert props["page_size"]["type"] == "integer"
    assert props["fields"]["type"] == "array"
    assert props["fields"].get("items") == {"type": "string"}
    # the merged rich description from FIELD_DESCRIPTIONS is preserved
    assert "NEVER pass placeholder" in props["fields"]["description"]


def test_spec_required_preserved() -> None:
    reg = ToolRegistry(AppSettings())
    assert reg._tools["query_data"].input_schema["required"] == ["business_object"]
    assert reg._tools["query_child_records"].input_schema["required"] == [
        "business_object",
        "parent_id",
        "child_business_component",
    ]


# ---------------------------------------------------------------------------
# Hot reload by mtime
# ---------------------------------------------------------------------------


def _bump_mtime(path, delta_ns: int = 1_000_000_000) -> None:
    prev = os.stat(path).st_mtime_ns
    os.utime(path, ns=(prev + delta_ns, prev + delta_ns))


def test_spec_hot_reload(tmp_path) -> None:
    schema = {"type": "object", "properties": {"business_object": {"type": "string"}}, "required": ["business_object"]}
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"query_data": {"description": "v1", "input_schema": schema}}), encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg._tools["query_data"].description == "v1"

    spec.write_text(json.dumps({"query_data": {"description": "v2", "input_schema": schema}}), encoding="utf-8")
    _bump_mtime(spec)

    tools = {t["name"]: t for t in reg.list_tools()}
    assert tools["query_data"]["description"] == "v2"


def test_spec_no_reload_when_mtime_unchanged(tmp_path) -> None:
    """A list call must not re-read the file when it hasn't changed."""
    schema = {"type": "object", "properties": {"business_object": {"type": "string"}}, "required": ["business_object"]}
    reg = _registry_with_spec(tmp_path, {"query_data": {"description": "stable", "input_schema": schema}})
    mtime_before = reg._spec_mtime
    reg.list_tools()
    assert reg._spec_mtime == mtime_before


# ---------------------------------------------------------------------------
# Fallbacks — the server never crashes over a bad spec; it degrades.
# ---------------------------------------------------------------------------


def test_spec_fallback_on_missing_file(tmp_path) -> None:
    settings = AppSettings()
    settings.tools_spec_path = str(tmp_path / "does-not-exist.json")
    reg = ToolRegistry(settings)
    names = {t["name"] for t in reg.list_tools()}
    assert "query_data" in names
    # degraded placeholder: empty properties, marker description
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
    assert reg._tools["query_data"].input_schema["properties"] == {}
    assert reg._tools["query_data"].description == _degraded_description("query_data")


def test_spec_invalid_entry_degraded(tmp_path) -> None:
    """An entry that fails validation (non-string description) is ignored for
    that tool and a degraded placeholder is used. A valid entry beside it still
    serves its JSON metadata, so one bad entry doesn't poison the rest."""
    spec = tmp_path / "badentry.json"
    spec.write_text(
        json.dumps(
            {
                "query_data": {"description": 123, "input_schema": {"type": "object", "properties": {}}},
                "count_records": {
                    "description": "count tool",
                    "input_schema": {"type": "object", "properties": {"business_object": {"type": "string"}}},
                },
            }
        ),
        encoding="utf-8",
    )
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg._tools["query_data"].description == _degraded_description("query_data")
    assert reg._tools["query_data"].input_schema["properties"] == {}
    # the valid sibling entry is served from JSON, not degraded
    assert reg._tools["count_records"].description == "count tool"


def test_spec_entry_without_handler_skipped(tmp_path) -> None:
    """A spec entry naming a tool with no matching handler is skipped (warned),
    not exposed; real tools still load."""
    schema = {"type": "object", "properties": {"business_object": {"type": "string"}}, "required": ["business_object"]}
    reg = _registry_with_spec(
        tmp_path,
        {
            "query_data": {"description": "ok", "input_schema": schema},
            "no_such_handler": {"description": "x", "input_schema": {"type": "object", "properties": {}}},
        },
    )
    assert "no_such_handler" not in reg._tools
    assert "query_data" in reg._tools
