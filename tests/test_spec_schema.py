"""Keep the spec JSON Schema (``tool_specs.schema.json``) consistent with the
kind registry in code, and validate the bundled spec against it.

The schema is an authoring aid for editors (autocomplete + validation); the
loader's runtime validation remains the source of truth. This test guards the
hand-maintained schema against drift from the code:

* every kind in ``_KINDS`` appears in the schema's ``kind`` enum;
* every access level the loader accepts (``_ACCESS_LEVELS``) appears in the
  schema's ``access`` enum;
* for each kind, every allowed option appears as a property in that kind's
  ``if/then`` block, and every required option is in its ``required`` array;
* every renderer in ``RENDERERS`` appears in some per-kind render enum;
* the bundled ``tool_specs.json`` validates against the schema.
"""

from __future__ import annotations

import json
from importlib.resources import files

import jsonschema
import pytest

from mcp_siebel.renderers import RENDERERS
from mcp_siebel.tools import _ACCESS_LEVELS, _KINDS


def _load_schema() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.schema.json").read_text(encoding="utf-8"))


def _load_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _per_kind_thens(schema: dict[str, object]) -> dict[str, dict[str, object]]:
    """Map kind name -> the ``then`` block of that kind's if/then in toolEntry."""
    tool_entry = schema["$defs"]["toolEntry"]  # type: ignore[index]
    out: dict[str, dict[str, object]] = {}
    for block in tool_entry.get("allOf", []):  # type: ignore[union-attr]
        cond = block.get("if", {})  # type: ignore[union-attr]
        kind_prop = cond.get("properties", {}).get("kind", {})  # type: ignore[union-attr]
        if "const" in kind_prop:  # type: ignore[union-attr]
            out[kind_prop["const"]] = block["then"]  # type: ignore[index]
    return out


def _all_render_enums(schema: dict[str, object]) -> set[str]:
    """Every render name the schema mentions in a per-kind render enum."""
    renders: set[str] = set()
    for then in _per_kind_thens(schema).values():
        prop = then.get("properties", {}).get("render", {})  # type: ignore[union-attr]
        if isinstance(prop, dict) and "enum" in prop:
            renders.update(prop["enum"])  # type: ignore[union-attr]
    return renders


# ---------------------------------------------------------------------------
# Schema/spec consistency with the kind registry
# ---------------------------------------------------------------------------


def test_schema_kind_enum_matches_registry() -> None:
    schema = _load_schema()
    kind_enum = set(schema["$defs"]["kindEnum"]["enum"])  # type: ignore[index]
    assert kind_enum == set(_KINDS), f"schema kinds {sorted(kind_enum)} != registry {sorted(_KINDS)}"


def test_schema_access_enum_matches_loader() -> None:
    schema = _load_schema()
    access_enum = set(schema["$defs"]["accessEnum"]["enum"])  # type: ignore[index]
    assert access_enum == _ACCESS_LEVELS, f"schema access {sorted(access_enum)} != loader {sorted(_ACCESS_LEVELS)}"


@pytest.mark.parametrize("kind_name", sorted(_KINDS))
def test_schema_per_kind_options_match_registry(kind_name: str) -> None:
    schema = _load_schema()
    thens = _per_kind_thens(schema)
    assert kind_name in thens, f"no if/then block for kind {kind_name!r}"
    then = thens[kind_name]
    kind_spec = _KINDS[kind_name]

    props = set(then.get("properties", {}).keys())  # type: ignore[union-attr]
    assert kind_spec.allowed_options <= props, (
        f"kind {kind_name!r}: allowed options {sorted(kind_spec.allowed_options)} "
        f"not all in schema then.properties {sorted(props)}"
    )

    required = set(then.get("required", []))  # type: ignore[union-attr]
    assert kind_spec.required_options <= required, (
        f"kind {kind_name!r}: required options {sorted(kind_spec.required_options)} "
        f"not all in schema then.required {sorted(required)}"
    )


def test_schema_render_enums_cover_every_renderer() -> None:
    schema = _load_schema()
    schema_renders = _all_render_enums(schema)
    assert schema_renders == set(RENDERERS), (
        f"schema render names {sorted(schema_renders)} != registry {sorted(RENDERERS)}"
    )


# ---------------------------------------------------------------------------
# The bundled spec validates against the schema
# ---------------------------------------------------------------------------


def test_bundled_spec_validates_against_schema() -> None:
    schema = _load_schema()
    spec = _load_spec()
    jsonschema.Draft202012Validator(schema).validate(spec)
