"""Read-only by default: write/delete tools are gated by their ``access`` level
in the spec (``"write"`` / ``"delete"``) plus SIEBEL_ALLOW_WRITE /
SIEBEL_ALLOW_DELETE in ToolRegistry. ``disabled_tools`` now returns ONLY the
explicit SIEBEL_DISABLED_TOOLS list — access gating lives in the registry, not
in config. See tool_specs.json (per-entry ``access``) and ToolRegistry.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from importlib.resources import files

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import SessionContext, ToolRegistry

# The write/delete tool sets, as declared by ``access`` in the bundled spec.
WRITE_TOOLS = {
    "create_record",
    "create_record_json",
    "update_record",
    "create_child_record",
    "upload_attachment",
}
DELETE_TOOLS = {"delete_record"}


def _names(reg: ToolRegistry) -> set[str]:
    return {t["name"] for t in reg.list_tools()}


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _registry_with_extra(tmp_path, extra: dict[str, dict[str, object]], **settings_overrides) -> ToolRegistry:
    """Registry backed by the bundled spec plus ``extra`` tool entries — proves a
    tool added via JSON alone is gated by its ``access`` with no code change."""
    spec = deepcopy(_bundled_spec())
    spec["tools"].update(extra)  # type: ignore[union-attr]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    settings = AppSettings(**settings_overrides)
    settings.tools_spec_path = str(path)
    return ToolRegistry(settings)


# --- disabled_tools property: explicit SIEBEL_DISABLED_TOOLS only -----------
#
# Access gating (read/write/delete) moved into ToolRegistry; the property no
# longer auto-appends write/delete tools.


def test_disabled_tools_empty_by_default() -> None:
    assert AppSettings().disabled_tools == []  # nothing auto-gated


def test_disabled_tools_returns_only_explicit_list() -> None:
    s = AppSettings()
    s.disabled_tools_raw = "query_data, get_record"
    assert s.disabled_tools == ["query_data", "get_record"]


def test_disabled_tools_unaffected_by_access_flags() -> None:
    # allow_write/allow_delete gate at the registry (by `access`), not here.
    s = AppSettings(allow_write=True, allow_delete=True)
    s.disabled_tools_raw = "query_data"
    assert s.disabled_tools == ["query_data"]


# --- ToolRegistry integration ---------------------------------------------


def test_list_tools_hides_write_and_delete_by_default() -> None:
    reg = ToolRegistry(AppSettings())
    names = _names(reg)
    assert not (names & WRITE_TOOLS), f"write tools visible by default: {names & WRITE_TOOLS}"
    assert not (names & DELETE_TOOLS), f"delete tools visible by default: {names & DELETE_TOOLS}"
    # read + authenticate always present
    assert "authenticate" in names
    assert "query_data" in names


def test_list_tools_allow_write_shows_write_tools() -> None:
    reg = ToolRegistry(AppSettings(allow_write=True))
    names = _names(reg)
    assert WRITE_TOOLS <= names
    assert not (names & DELETE_TOOLS)  # delete still hidden


def test_list_tools_allow_delete_shows_delete_tool() -> None:
    reg = ToolRegistry(AppSettings(allow_delete=True))
    names = _names(reg)
    assert DELETE_TOOLS <= names
    assert not (names & WRITE_TOOLS)  # write still hidden


@pytest.mark.asyncio
async def test_call_refuses_disabled_write_tool() -> None:
    """A gated tool must be refused at call() before any Siebel I/O."""
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


# --- always_enabled exemption (spec-driven, not hard-coded) ------------------
#
# The authenticate tool is marked ``"always_enabled": true`` in tool_specs.json.
# The registry exempts always_enabled tools from the disabled-tools filter so a
# deployer can't brick the server by listing them in SIEBEL_DISABLED_TOOLS, and
# logs a warning instead of silently ignoring the entry.


def test_always_enabled_tool_stays_listed_and_callable_when_disabled_listed() -> None:
    """An always_enabled tool listed in SIEBEL_DISABLED_TOOLS stays listed, and
    call() does not refuse it as disabled (it runs the handler instead)."""
    s = AppSettings()
    s.disabled_tools_raw = "authenticate, query_data"
    reg = ToolRegistry(s)
    names = _names(reg)
    assert "authenticate" in names  # always_enabled — exempt
    assert "query_data" not in names  # normal tool alongside it — still hidden


@pytest.mark.asyncio
async def test_call_does_not_refuse_always_enabled_tool() -> None:
    """call() runs the always_enabled tool's handler rather than returning the
    'is disabled' error — the handler surfaces its own (credential) error."""
    s = AppSettings()
    s.disabled_tools_raw = "authenticate"
    reg = ToolRegistry(s)
    result = await reg.call("authenticate", {"username": "x"}, SessionContext(session_id="s1"))
    assert result.get("isError")  # password-required, but NOT a disabled refusal
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    assert "disabled" not in text.lower()


def test_warning_logged_when_always_enabled_listed_in_disabled(caplog) -> None:
    """Listing an always_enabled tool in SIEBEL_DISABLED_TOOLS logs a clear
    warning at load time — the entry is ignored, not silently dropped."""
    s = AppSettings()
    s.disabled_tools_raw = "authenticate"
    with caplog.at_level(logging.WARNING, logger="mcp-siebel"):
        reg = ToolRegistry(s)  # noqa: F841 — construction triggers the load-time warning
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("authenticate" in m and "always_enabled" in m for m in warnings), warnings


def test_normal_tool_alongside_always_enabled_is_still_disabled() -> None:
    """The exemption applies only to always_enabled tools — a normal tool listed
    next to one is still disabled, and no warning is logged for it."""
    s = AppSettings(allow_write=True, allow_delete=True)
    s.disabled_tools_raw = "authenticate, query_data"
    reg = ToolRegistry(s)
    names = _names(reg)
    assert "query_data" not in names
    assert "authenticate" in names


# --- spec-driven access gating (access level lives in the spec) -------------
#
# A tool's ``access`` ("read"|"write"|"delete") is the single source of truth
# for gating. A write tool added via JSON alone is hidden/refused when
# allow_write is off — no framework code change is needed.


_BULK_CREATE: dict[str, object] = {
    "description": "A NEW write tool added via the spec only.",
    "kind": "bc_upsert",
    "access": "write",
    "operation": "create",
    "scope": "root",
    "render": "create_prose",
    "input_schema": {
        "type": "object",
        "properties": {"business_object": {"type": "string"}},
        "required": ["business_object"],
    },
}


def test_new_write_tool_is_gated_by_default(tmp_path) -> None:
    """A write tool added via JSON is hidden when allow_write is off — no code
    change needed beyond the spec entry."""
    reg = _registry_with_extra(tmp_path, {"bulk_create": _BULK_CREATE})
    assert "bulk_create" not in _names(reg)


def test_new_write_tool_enabled_by_allow_write(tmp_path) -> None:
    reg = _registry_with_extra(tmp_path, {"bulk_create": _BULK_CREATE}, allow_write=True)
    assert "bulk_create" in _names(reg)


@pytest.mark.asyncio
async def test_new_write_tool_call_refused_when_gated(tmp_path) -> None:
    reg = _registry_with_extra(tmp_path, {"bulk_create": _BULK_CREATE})
    result = await reg.call("bulk_create", {"business_object": "Account"}, None)
    assert result.get("isError")
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    assert "disabled" in text.lower()


# --- always_enabled is only allowed with access "read" ----------------------


def test_always_enabled_with_write_access_rejected_at_load(tmp_path) -> None:
    """An entry that is both always_enabled and access "write" is rejected at
    load time — always_enabled must never bypass the access gate."""
    from mcp_siebel.tools import _SpecError

    bad = {**_BULK_CREATE, "always_enabled": True}
    with pytest.raises(_SpecError, match="always_enabled is only allowed with access 'read'"):
        _registry_with_extra(tmp_path, {"always_write": bad})
