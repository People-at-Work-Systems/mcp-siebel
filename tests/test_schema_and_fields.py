"""Tests for the mcp-siebel schema/fields hardening.

Background: every Siebel tool argument was declared as a bare `{"type":"string"}`
with no description and no `required` (`_schema()` in tools.py), so the LLM got
no guidance on what `fields` / `child_business_component` should contain and
emitted placeholder tokens like '<?>'. That produced:

  - SBL-EAI-50258 "Invalid fields: '<?>'" from query_data / count_records,
  - a "retry without fields" that fell back to Siebel's *default* Integration
    Object field set — which for Contact references 'Primary Address Integration
    Id' (not on the Contact BC) → SBL-DAT-00398, and
  - SBL-EAI-50257 "BC 'Contact' not mapped in IO 'Account Integration Object'"
    from describe_business_object(Account, Contact) — Contact lives in its own
    BO, not Account's.

These tests pin the fixes: rich schemas (descriptions + `required`, now authored
in ``tool_specs.json`` and served via the registry), placeholder-field rejection
before any Siebel call, the safe ['Id'] retry, and the describe_business_object
BO fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.schema_cache import SchemaCache
from mcp_siebel.siebel_client import (
    SiebelBCDescriptor,
    SiebelError,
    SiebelFieldDescriptor,
    SiebelQueryResult,
)
from mcp_siebel.tools import (
    SessionContext,
    ToolContext,
    _describe_business_object,
    _query_data,
    _validate_fields_arg,
)

# ---------------------------------------------------------------------------
# Registry-served schema (authored in tool_specs.json, $ref-resolved by the loader)
# ---------------------------------------------------------------------------


def test_query_data_schema_marks_business_object_required():
    # Imported lazily so the test reflects the registry actually built at import.
    from mcp_siebel.tools import ToolRegistry

    tools = ToolRegistry(AppSettings())._tools
    assert tools["query_data"].input_schema["required"] == ["business_object"]
    assert tools["query_child_records"].input_schema["required"] == [
        "business_object",
        "parent_id",
        "child_business_component",
    ]
    assert "description" in tools["query_child_records"].input_schema["properties"]["child_business_component"]


# ---------------------------------------------------------------------------
# _validate_fields_arg
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", [], ["Name", "Status"], "Name,Status"])
def test_validate_fields_accepts_real_or_absent(raw):
    assert _validate_fields_arg(raw) is None


@pytest.mark.parametrize("raw", ["<?>", ["<?>"], "Name,<?>", "<?>,Status", "?", "unknown", "<field>"])
def test_validate_fields_rejects_placeholders(raw):
    msg = _validate_fields_arg(raw)
    assert msg is not None
    assert "placeholder" in msg.lower()
    assert "describe_business_object" in msg


# ---------------------------------------------------------------------------
# _query — placeholder rejection (no Siebel round-trip)
# ---------------------------------------------------------------------------


def _query_ctx() -> tuple[ToolContext, AsyncMock]:
    query_mock = AsyncMock()
    client = AsyncMock()
    client.query = query_mock
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)
    return ToolContext(settings=AppSettings(), session=session), query_mock


@pytest.mark.asyncio
async def test_query_data_rejects_placeholder_fields_without_calling_siebel():
    ctx, query_mock = _query_ctx()
    response = await _query_data(
        {"business_object": "Account", "business_component": "Account", "fields": "<?>"}, ctx
    )
    assert response.get("isError") is True
    assert "placeholder" in response["content"][0]["text"].lower()
    query_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _query — retry uses safe ['Id'] instead of None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_data_field_error_retries_with_id_not_none():
    """A genuinely invalid field name (not a placeholder — it passes
    validation) must trigger the 50258 retry with fields=['Id'], never with
    None, so Siebel's broken default Contact field set is never used."""
    result = SiebelQueryResult(items=[{"Id": "1-5GZE"}], total_count=1, has_more=False, offset=0, page_size=20)
    ctx, query_mock = _query_ctx()
    field_error = SiebelError("bad field", 500, '{"ERROR":"...SBL-EAI-50258..."}')
    query_mock.side_effect = [field_error, result]

    response = await _query_data(
        {"business_object": "Account", "business_component": "Account", "fields": "NonExistentField"}, ctx
    )
    assert response.get("isError") is not True  # retry succeeded
    # Second (retry) call must pass fields=["Id"], not None.
    second_kwargs = query_mock.call_args_list[1].kwargs
    assert second_kwargs["fields"] == ["Id"]


# ---------------------------------------------------------------------------
# _describe_business_object — BO fallback on SBL-EAI-50257
# ---------------------------------------------------------------------------


def _describe_ctx(get_descriptor_side_effect, resolve_map: dict[str, str]) -> ToolContext:
    cache = AsyncMock()

    async def resolve(name: str) -> str | None:
        return resolve_map.get(name.lower())

    cache.resolve_business_object = AsyncMock(side_effect=resolve)
    cache.get_descriptor = AsyncMock(side_effect=get_descriptor_side_effect)
    # field_summary is a sync @staticmethod on the real SchemaCache; the
    # AsyncMock would otherwise return a coroutine, so bind the real one.
    cache.field_summary = SchemaCache.field_summary

    client = AsyncMock()
    session = SessionContext(session_id="s1", username="t", client=client, cache=cache)
    return ToolContext(settings=AppSettings(), session=session)


@pytest.mark.asyncio
async def test_describe_falls_back_to_child_own_bo_on_50257():
    """describe_business_object(Account, Contact): Contact isn't in the Account
    IO (50257). Contact resolves to its own BO, so describe should retry under
    (Contact, Contact) and return that field list instead of erroring."""

    async def get_descriptor(bo: str, bc: str) -> SiebelBCDescriptor:
        if (bo, bc) == ("Account", "Contact"):
            raise SiebelError("not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}')
        if (bo, bc) == ("Contact", "Contact"):
            return SiebelBCDescriptor(
                "Contact", "Contact", [SiebelFieldDescriptor(name="Last Name", type="DTYPE_TEXT")]
            )
        raise AssertionError(f"unexpected describe call: {bo}/{bc}")

    ctx = _describe_ctx(get_descriptor, {"account": "Account", "contact": "Contact"})
    response = await _describe_business_object(
        {"business_object": "Account", "business_component": "Contact"}, ctx
    )
    text = response["content"][0]["text"]
    assert "Contact / Contact" in text  # described under Contact's own BO, not Account
    assert "Last Name" in text
    assert response.get("isError") is not True


@pytest.mark.asyncio
async def test_describe_returns_error_when_bc_is_the_bo_itself():
    """No fallback when the BC equals the BO (nothing else to try)."""

    async def get_descriptor(bo: str, bc: str) -> SiebelBCDescriptor:
        raise SiebelError("not mapped", 500, '{"ERROR":"...SBL-EAI-50257..."}')

    ctx = _describe_ctx(get_descriptor, {"account": "Account"})
    response = await _describe_business_object({"business_object": "Account"}, ctx)
    assert response.get("isError") is True
