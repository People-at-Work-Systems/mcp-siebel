"""Read-only by default: write/delete tools are gated by SIEBEL_ALLOW_WRITE /
SIEBEL_ALLOW_DELETE, both default off. See config.AppSettings.WRITE_TOOLS /
DELETE_TOOLS and the disabled_tools property.
"""

from __future__ import annotations

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import ToolRegistry

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
