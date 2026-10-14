"""Read-only by default: write/delete tools are gated by SIEBEL_ALLOW_WRITE /
SIEBEL_ALLOW_DELETE, both default off. See config.AppSettings.WRITE_TOOLS /
DELETE_TOOLS and the disabled_tools property.
"""

from __future__ import annotations

import logging

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import SessionContext, ToolRegistry

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


def _disabled_lower(settings: AppSettings) -> set[str]:
    return {d.lower() for d in settings.disabled_tools}


# --- disabled_tools property (the choke point) -----------------------------


def test_property_gates_write_and_delete_by_default() -> None:
    s = AppSettings()
    disabled = _disabled_lower(s)
    assert {w.lower() for w in WRITE_TOOLS} <= disabled
    assert DELETE_TOOLS <= disabled
    # read tools are NOT auto-gated
    assert "query_data" not in disabled
    assert "list_business_objects" not in disabled


def test_property_allow_write_ungates_write_only() -> None:
    s = AppSettings(allow_write=True)
    disabled = _disabled_lower(s)
    assert not ({w.lower() for w in WRITE_TOOLS} & disabled)
    assert DELETE_TOOLS <= disabled  # delete still gated


def test_property_allow_delete_ungates_delete_only() -> None:
    s = AppSettings(allow_delete=True)
    disabled = _disabled_lower(s)
    assert not (DELETE_TOOLS & disabled)
    assert {w.lower() for w in WRITE_TOOLS} <= disabled  # write still gated


def test_property_both_flags_ungate_everything() -> None:
    s = AppSettings(allow_write=True, allow_delete=True)
    disabled = _disabled_lower(s)
    assert not ({w.lower() for w in WRITE_TOOLS} | DELETE_TOOLS) & disabled


def test_property_explicit_disabled_tools_unioned_with_gates() -> None:
    s = AppSettings()
    s.disabled_tools_raw = "query_data, get_record"
    disabled = _disabled_lower(s)
    # gated write/delete still present
    assert "create_record" in disabled and "delete_record" in disabled
    # explicit additions present too
    assert "query_data" in disabled and "get_record" in disabled
    # a read tool not explicitly disabled is not gated
    assert "list_business_objects" not in disabled


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
