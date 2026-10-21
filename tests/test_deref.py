"""Findings 11 & 12: ``_deref`` rejects ``$ref``-with-sibling-keys and caps
``$ref`` chain depth at 100. A deep non-cyclic chain surfaces as a clean
``_SpecError`` (not an uncaught ``RecursionError``), and a reload that feeds a
deep chain keeps the last valid tool set serving (no crash).
"""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import ToolRegistry, _deref, _SpecError
from tests.test_tool_specs import _bump_mtime


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Finding 11 — $ref with sibling keys is rejected, not silently dropped
# --------------------------------------------------------------------------- #


def test_deref_rejects_ref_with_sibling_keys() -> None:
    with pytest.raises(_SpecError, match="sibling"):
        _deref({"$ref": "#/$defs/id", "description": "x"}, {"id": {"type": "string"}})


def test_deref_allows_bare_ref() -> None:
    # A bare $ref (no siblings) resolves normally.
    out = _deref({"$ref": "#/$defs/id"}, {"id": {"type": "string"}})
    assert out == {"type": "string"}


# --------------------------------------------------------------------------- #
# Finding 12 — depth cap turns a deep chain into _SpecError, not RecursionError
# --------------------------------------------------------------------------- #


def test_deref_deep_chain_raises_spec_error_not_recursion_error() -> None:
    # A 2000-deep non-cyclic chain: d0 -> d1 -> ... -> d1999 (terminal).
    defs: dict[str, object] = {f"d{i}": {"$ref": f"#/$defs/d{i + 1}"} for i in range(2000)}
    defs["d1999"] = {"type": "string"}
    with pytest.raises(_SpecError, match="too deep"):
        _deref({"$ref": "#/$defs/d0"}, defs)


def test_reload_with_deep_chain_keeps_last_valid(tmp_path) -> None:
    """A reload whose spec contains a deep $ref chain must not crash the registry;
    the last valid tool set keeps serving (keep-last-valid contract)."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_bundled_spec(), indent=2), encoding="utf-8")
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    n_valid = len(reg.list_tools())
    assert n_valid > 0

    # Rewrite one tool's schema to point at a deep $ref chain, bump mtime.
    bad = json.loads(json.dumps(_bundled_spec()))
    for i in range(2000):
        bad["$defs"][f"d{i}"] = {"$ref": f"#/$defs/d{i + 1}"}  # type: ignore[index]
    bad["$defs"]["d1999"] = {"type": "string"}  # type: ignore[index]
    bad["tools"]["query_data"]["input_schema"]["properties"]["business_object"] = {"$ref": "#/$defs/d0"}  # type: ignore[index]
    spec.write_text(json.dumps(bad, indent=2), encoding="utf-8")
    _bump_mtime(spec)

    # The reload fails on the deep chain; the last valid set is still served.
    tools = reg.list_tools()
    assert len(tools) == n_valid
