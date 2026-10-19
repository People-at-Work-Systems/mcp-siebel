"""Tests for the top-level ``preferred_fields`` section of ``tool_specs.json``.

The section is optional: a spec without it loads with an empty mapping. When
present it must be an object of Business Component name -> non-empty list of
unique strings; any violation is a ``_SpecError`` (fatal on first load,
keep-last-valid on reload). It is hot-reloaded together with the tools, and the
loaded mapping reaches the record handlers through ``ToolContext`` (no
module-level global a reload would miss).
"""

from __future__ import annotations

import json
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelBCDescriptor, SiebelError, SiebelFieldDescriptor, SiebelQueryResult
from mcp_siebel.tools import SessionContext, ToolRegistry, _SpecError, _validate_preferred_fields

# The verbatim content of the former handlers.records._PREFERRED_FIELDS, now
# authored in the spec. Order matters (reading order: who, reach, where, detail).
_EXPECTED_BUNDLED: dict[str, list[str]] = {
    "Contact": [
        "Last Name",
        "First Name",
        "Middle Name",
        "Job Title",
        "Primary Account Name",
        "Work Phone #",
        "Cellular Phone #",
        "Home Phone #",
        "Fax Phone #",
        "Email Address",
        "Primary Street Address",
        "Primary City",
        "Primary State",
        "Primary Postal Code",
        "Primary Country",
        "Status",
        "Comment",
    ],
    "Account": [
        "Name",
        "Account Status",
        "Type",
        "Main Phone Number",
        "Main Fax Number",
        "Home Page",
        "Primary Account Street Address",
        "Primary Account City",
        "Primary Account State",
        "Primary Account Postal Code",
        "Primary Account Country",
        "Primary Organization",
        "Annual Revenue",
        "Employees",
    ],
    "Opportunity": [
        "Name",
        "Account",
        "Sales Stage",
        "Primary Revenue Amount",
        "Primary Revenue Close Date",
        "Win Probability",
        "Primary Organization",
        "Sales Method",
        "Description",
    ],
    "Service Request": [
        "SR Number",
        "Abstract",
        "Status",
        "Substatus",
        "Priority",
        "Severity",
        "Account",
        "Contact Last Name",
        "Contact First Name",
        "Owner",
        "Description",
    ],
}


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _bundled_defs() -> dict[str, object]:
    return _bundled_spec().get("$defs", {})  # type: ignore[return-value]


def _bundled_tools() -> dict[str, dict[str, object]]:
    return _bundled_spec()["tools"]  # type: ignore[return-value]


def _write_spec(path, tools: dict[str, object], preferred_fields: object | None = None) -> None:
    payload: dict[str, object] = {"$defs": _bundled_defs(), "tools": tools}
    if preferred_fields is not None:
        payload["preferred_fields"] = preferred_fields
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def _registry(tmp_path, tools: dict[str, object], preferred_fields: object | None = None) -> ToolRegistry:
    spec = tmp_path / "spec.json"
    _write_spec(spec, tools, preferred_fields)
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    return ToolRegistry(settings)


# --- bundled spec integrity --------------------------------------------------


def test_bundled_spec_has_preferred_fields_verbatim() -> None:
    assert _bundled_spec().get("preferred_fields") == _EXPECTED_BUNDLED


def test_registry_loads_bundled_preferred_fields() -> None:
    reg = ToolRegistry(AppSettings())
    assert reg.preferred_fields == _EXPECTED_BUNDLED


# --- optionality + validation ------------------------------------------------


def test_spec_without_preferred_fields_loads_empty(tmp_path) -> None:
    reg = _registry(tmp_path, _bundled_tools())
    assert reg.preferred_fields == {}


def test_validate_preferred_fields_not_an_object() -> None:
    with pytest.raises(_SpecError, match="must be an object"):
        _validate_preferred_fields(["Contact", "Last Name"])


def test_validate_preferred_fields_value_not_a_list(tmp_path) -> None:
    with pytest.raises(_SpecError, match="non-empty list"):
        _registry(tmp_path, _bundled_tools(), preferred_fields={"Contact": "Last Name"})


def test_validate_preferred_fields_empty_list(tmp_path) -> None:
    with pytest.raises(_SpecError, match="non-empty list"):
        _registry(tmp_path, _bundled_tools(), preferred_fields={"Contact": []})


def test_validate_preferred_fields_non_string_element(tmp_path) -> None:
    with pytest.raises(_SpecError, match="only strings"):
        _registry(tmp_path, _bundled_tools(), preferred_fields={"Contact": ["Last Name", 42]})


def test_validate_preferred_fields_duplicate_field(tmp_path) -> None:
    with pytest.raises(_SpecError, match="duplicate field names"):
        _registry(tmp_path, _bundled_tools(), preferred_fields={"Contact": ["Last Name", "Last Name"]})


# --- hot reload --------------------------------------------------------------


def _bump_mtime(path) -> None:
    import os

    prev = os.stat(path).st_mtime_ns
    os.utime(path, ns=(prev + 1_000_000_000, prev + 1_000_000_000))


def test_preferred_fields_hot_reloaded(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _write_spec(spec, _bundled_tools(), preferred_fields={"Contact": ["Last Name"]})
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg.preferred_fields == {"Contact": ["Last Name"]}

    _write_spec(spec, _bundled_tools(), preferred_fields={"Account": ["Name"]})
    _bump_mtime(spec)
    reg.list_tools()  # triggers reload
    assert reg.preferred_fields == {"Account": ["Name"]}


def test_preferred_fields_reload_failure_keeps_last_valid(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _write_spec(spec, _bundled_tools(), preferred_fields={"Contact": ["Last Name"]})
    settings = AppSettings()
    settings.tools_spec_path = str(spec)
    reg = ToolRegistry(settings)
    assert reg.preferred_fields == {"Contact": ["Last Name"]}

    # Corrupt preferred_fields and bump mtime; the reload fails so the last
    # valid mapping keeps serving alongside the last valid tool set.
    _write_spec(spec, _bundled_tools(), preferred_fields={"Contact": []})
    _bump_mtime(spec)
    reg.list_tools()
    assert reg.preferred_fields == {"Contact": ["Last Name"]}


# --- end-to-end: the fallback reads preferred_fields from the spec -----------


def _descriptor(*names: str) -> SiebelBCDescriptor:
    return SiebelBCDescriptor(
        "Contact", "Contact", [SiebelFieldDescriptor(name=n, type="DTYPE_TEXT") for n in names]
    )


def _query_result(record: dict[str, str]) -> SiebelQueryResult:
    return SiebelQueryResult(items=[record], total_count=1, has_more=False, next_start_row=None, offset=0, page_size=1)


_BROKEN_FIELD_ERROR = SiebelError("Siebel API returned 500", 500, '{"ERROR":"SBL-DAT-00398"}')


@pytest.mark.asyncio
async def test_get_record_fallback_uses_preferred_fields_from_the_spec(tmp_path) -> None:
    """Override the spec's Contact preferred fields with a custom list and prove
    the scoped-query fallback asks for exactly those (curated ∩ available + Id)
    — so the field list is sourced from the spec, not a Python constant."""
    tools = _bundled_tools()
    reg = _registry(tmp_path, tools, preferred_fields={"Contact": ["Last Name", "Favorite Color"]})

    client = AsyncMock()
    client.get_record = AsyncMock(side_effect=_BROKEN_FIELD_ERROR)
    client.describe_bc = AsyncMock(return_value=_descriptor("Last Name", "Favorite Color", "Id", "Junk"))
    client.query = AsyncMock(return_value=_query_result({"Last Name": "Taylor"}))
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Contact")
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)

    await reg.call("get_record", {"business_object": "Contact", "id": "1"}, session)
    assert client.query.call_args.kwargs["fields"] == ["Last Name", "Favorite Color", "Id"]
