#!/usr/bin/env python3
"""Generate the bundled tool-spec JSON from the handlers in ``tools.py``.

The spec file (``src/mcp_siebel/specs/tool_specs.json``) is the single source of
truth for each tool's description and input_schema at runtime. Handlers stay in
Python; this script bakes the *final, typed* schema for each tool — combining the
per-parameter type map below with the rich ``FIELD_DESCRIPTIONS`` text — so the
generated file is what the MCP server serves.

Re-run this whenever a handler's default metadata changes (a tool added, a param
renamed, a description tweaked in ``FIELD_DESCRIPTIONS``). It is a dev tool, not
imported at runtime.

    python scripts/dump_tool_specs.py
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_siebel.tools import _build_tool_metadata

# Per-parameter JSON Schema types that are not plain strings. This map lived in
# stdio.py as the runtime enrichment layer; with schemas now authored in the spec
# file it is only needed here, at generation time, to bake the typed schema.
_PROP_TYPES: dict[str, dict[str, object]] = {
    "page_size": {"type": "integer", "description": "Number of records per page (default 20)"},
    "offset": {"type": "integer", "description": "Zero-based record offset for pagination"},
    "count": {"type": "integer", "description": "Number of results to return"},
    "data": {"type": "object", "description": "Field/value pairs for create or update"},
    "fields": {"type": "array", "items": {"type": "string"}, "description": "Field names to include"},
    "childlinks": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Child business component names to include",
    },
    "lov_types": {"type": "array", "items": {"type": "string"}, "description": "LOV type codes"},
}


def _merge_property(name: str, base: dict[str, object]) -> dict[str, object]:
    """Build the final property schema for ``name``: type from ``_PROP_TYPES`` if
    the name is known (else the base string type), description from the base
    schema (already authored via ``FIELD_DESCRIPTIONS`` in ``_schema()``) if
    present, else the ``_PROP_TYPES`` description."""
    pt = _PROP_TYPES.get(name)
    if pt is None:
        return dict(base)
    out = dict(pt)
    desc = base.get("description") or pt.get("description")
    if desc:
        out["description"] = desc
    return out


def _build_final_schema(base_schema: dict[str, object]) -> dict[str, object]:
    props = {
        name: _merge_property(name, prop)
        for name, prop in base_schema.get("properties", {}).items()
    }
    schema: dict[str, object] = {"type": "object", "properties": props}
    if "required" in base_schema:
        schema["required"] = base_schema["required"]
    return schema


def main() -> None:
    tools = _build_tool_metadata()
    spec: dict[str, dict[str, object]] = {}
    for name, tool in tools.items():
        spec[name] = {
            "description": tool.description,
            "input_schema": _build_final_schema(tool.input_schema),
        }

    out = Path(__file__).resolve().parent.parent / "src" / "mcp_siebel" / "specs" / "tool_specs.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(spec)} tool specs to {out}")


if __name__ == "__main__":
    main()
