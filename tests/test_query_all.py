"""Behavior tests for the ``query_all_records`` "scroll all" tool (kind
``python``, enabled by default).

The handler loops ``SiebelClient.query`` advancing ``offset = next_start_row``
until ``has_more`` is false or the per-call row ceiling (``query_max_records``,
clamped by ``SIEBEL_QUERY_MAX_RECORDS``) is hit. Driven through
``ToolRegistry.call`` with an ``AsyncMock`` client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError, SiebelQueryResult
from mcp_siebel.tools import SessionContext, ToolRegistry


def _reg_and_session(
    query_mock: AsyncMock, *, resolve: str | None = "Account", **overrides
) -> tuple[ToolRegistry, SessionContext]:
    client = AsyncMock()
    client.query = query_mock
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=resolve)
    session = SessionContext(session_id="s1", username="tester", client=client, cache=cache)
    return ToolRegistry(AppSettings(**overrides)), session


def _page(items, *, total, has_more, offset, next_start_row) -> SiebelQueryResult:
    return SiebelQueryResult(
        items=items, total_count=total, has_more=has_more,
        offset=offset, page_size=100, next_start_row=next_start_row,
    )


@pytest.mark.asyncio
async def test_multi_page_reaches_end_without_truncation() -> None:
    pages = [
        _page([{"Id": "1", "Name": "Acme"}, {"Id": "2", "Name": "Beta"}, {"Id": "3", "Name": "Gamma"}],
              total=5, has_more=True, offset=0, next_start_row=3),
        _page([{"Id": "4", "Name": "Delta"}, {"Id": "5", "Name": "Eps"}],
              total=5, has_more=False, offset=3, next_start_row=None),
    ]
    reg, session = _reg_and_session(AsyncMock(side_effect=pages))
    response = await reg.call(
        "query_all_records", {"business_object": "Account", "business_component": "Account"}, session
    )
    assert response.get("isError") is not True
    text = response["content"][0]["text"]
    assert "Acme" in text and "Eps" in text
    assert "Records: 5 of 5" in text
    assert "truncated" not in text


@pytest.mark.asyncio
async def test_ceiling_hit_marks_truncated() -> None:
    page = _page([{"Id": str(i), "Name": f"N{i}"} for i in range(1, 6)],
                 total=10, has_more=True, offset=0, next_start_row=5)
    reg, session = _reg_and_session(AsyncMock(side_effect=[page]), query_max_records=4)
    response = await reg.call("query_all_records", {"business_object": "Account"}, session)
    text = response["content"][0]["text"]
    assert "truncated at ceiling 4" in text
    assert "Records: 4 of 10" in text


@pytest.mark.asyncio
async def test_max_records_arg_below_setting_is_respected() -> None:
    page = _page([{"Id": str(i), "Name": f"N{i}"} for i in range(1, 6)],
                 total=10, has_more=True, offset=0, next_start_row=5)
    reg, session = _reg_and_session(AsyncMock(side_effect=[page]))  # default setting 10000
    response = await reg.call("query_all_records", {"business_object": "Account", "max_records": 2}, session)
    text = response["content"][0]["text"]
    assert "truncated at ceiling 2" in text
    assert "Records: 2 of 10" in text


@pytest.mark.asyncio
async def test_max_records_arg_above_setting_is_clamped() -> None:
    page = _page([{"Id": str(i), "Name": f"N{i}"} for i in range(1, 6)],
                 total=10, has_more=True, offset=0, next_start_row=5)
    reg, session = _reg_and_session(AsyncMock(side_effect=[page]), query_max_records=3)
    response = await reg.call("query_all_records", {"business_object": "Account", "max_records": 100000}, session)
    text = response["content"][0]["text"]
    assert "truncated at ceiling 3" in text


@pytest.mark.asyncio
async def test_placeholder_fields_returns_error() -> None:
    reg, session = _reg_and_session(AsyncMock(side_effect=[]))
    response = await reg.call("query_all_records", {"business_object": "Account", "fields": "<?>"}, session)
    assert response.get("isError") is True
    assert "placeholder" in response["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_unknown_business_object_returns_not_found() -> None:
    reg, session = _reg_and_session(AsyncMock(side_effect=[]), resolve=None)
    response = await reg.call("query_all_records", {"business_object": "Nope"}, session)
    text = response["content"][0]["text"]
    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_404_treated_as_empty_result() -> None:
    page = _page([{"Id": "1", "Name": "Acme"}], total=1, has_more=True, offset=0, next_start_row=1)
    query_mock = AsyncMock(side_effect=[page, SiebelError("404", 404, "nope")])
    reg, session = _reg_and_session(query_mock)
    response = await reg.call("query_all_records", {"business_object": "Account"}, session)
    text = response["content"][0]["text"]
    # First page returned 1 row, second page 404 -> loop breaks, no error.
    assert "Acme" in text
    assert response.get("isError") is not True


def test_query_all_records_enabled_by_default() -> None:
    reg = ToolRegistry(AppSettings())
    names = {t["name"] for t in reg.list_tools()}
    assert "query_all_records" in names
