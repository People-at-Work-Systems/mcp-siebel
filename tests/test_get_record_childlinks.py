"""get_record must pass childlinks=none so Siebel omits the 100+ child-BC Link
entries it would otherwise return.

_format_record (tools.py) turns those Link entries into a "Children:" line
listing every child Business Component — observed live on the Opportunity
detail for 1-1M6H, where it flooded the chat reply and the record form with a
~120-item comma-separated list. query() already defaulted to childlinks='None';
get_record was the gap. Verified against the demo Siebel: childlinks=none drops
the child Link entries from 124 to 0 (only self + canonical remain).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_siebel.siebel_client import SiebelClient


def _client() -> SiebelClient:
    return SiebelClient(
        base_url="https://siebel.example/siebel/v1.0",
        auth_mode="sso",
        sso_username="SADMIN",
    )


@pytest.mark.asyncio
async def test_get_record_defaults_to_childlinks_none():
    client = _client()
    client._request = AsyncMock(return_value={"Name": "Ellerby - $500 Follow-On"})

    await client.get_record("Opportunity", "Opportunity", "1-1M6H")

    _, kwargs = client._request.call_args
    assert kwargs["query_params"]["childlinks"] == "None"


@pytest.mark.asyncio
async def test_get_record_includes_fields_alongside_childlinks():
    client = _client()
    client._request = AsyncMock(return_value={"Name": "x"})

    await client.get_record("Opportunity", "Opportunity", "1-1M6H", fields=["Name", "Id"])

    _, kwargs = client._request.call_args
    assert kwargs["query_params"]["childlinks"] == "None"
    assert kwargs["query_params"]["fields"] == "Name,Id"


@pytest.mark.asyncio
async def test_get_record_childlinks_override():
    # A caller that genuinely wants child links (e.g. get_record_with_children
    # via a different path) can override the suppress-by-default behaviour.
    client = _client()
    client._request = AsyncMock(return_value={})

    await client.get_record("Opportunity", "Opportunity", "1-1M6H", childlinks="ALL")

    _, kwargs = client._request.call_args
    assert kwargs["query_params"]["childlinks"] == "ALL"
