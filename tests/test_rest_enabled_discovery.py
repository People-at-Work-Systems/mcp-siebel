"""Behavior tests for the ``list_rest_enabled_components`` discovery tool
(kind ``python``, enabled by default).

Composes ``SchemaCache.get_business_objects`` with ``get_business_components``
(a per-BO REST describe). With a ``business_object`` arg, lists BCs under one
BO; without it, enumerates BOs (capped by ``discovery_max_business_objects``)
and lists BCs per BO. A BO whose describe fails shows ``(none)`` rather than
aborting the sweep.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError
from mcp_siebel.tools import SessionContext, ToolRegistry


def _reg_and_session(cache: AsyncMock, **overrides) -> tuple[ToolRegistry, SessionContext]:
    client = AsyncMock()
    session = SessionContext(session_id="s1", username="tester", client=client, cache=cache)
    return ToolRegistry(AppSettings(**overrides)), session


@pytest.mark.asyncio
async def test_single_business_object_lists_components() -> None:
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    cache.get_business_components = AsyncMock(return_value=["Account", "Account Attachment"])
    reg, session = _reg_and_session(cache)
    response = await reg.call("list_rest_enabled_components", {"business_object": "account"}, session)
    assert response.get("isError") is not True
    text = response["content"][0]["text"]
    assert "Account" in text
    assert "Account Attachment" in text
    assert "REST-enabled Business Components" in text


@pytest.mark.asyncio
async def test_single_business_object_with_no_components() -> None:
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    cache.get_business_components = AsyncMock(return_value=[])
    reg, session = _reg_and_session(cache)
    response = await reg.call("list_rest_enabled_components", {"business_object": "account"}, session)
    text = response["content"][0]["text"]
    assert "exposes no Business Components" in text


@pytest.mark.asyncio
async def test_all_business_objects_under_cap_returns_full_map() -> None:
    async def gbc(bo: str) -> list[str]:
        return {"Account": ["Account", "Account Attachment"], "Contact": ["Contact"]}[bo]

    cache = AsyncMock()
    cache.get_business_objects = AsyncMock(return_value=["Account", "Contact"])
    cache.get_business_components = AsyncMock(side_effect=gbc)
    reg, session = _reg_and_session(cache)  # default cap 100
    response = await reg.call("list_rest_enabled_components", {}, session)
    text = response["content"][0]["text"]
    assert "(2/2)" in text
    assert "truncated" not in text
    assert "Account: Account, Account Attachment" in text
    assert "Contact: Contact" in text


@pytest.mark.asyncio
async def test_all_business_objects_over_cap_truncates() -> None:
    async def gbc(bo: str) -> list[str]:
        return {"Account": ["Account"], "Contact": ["Contact"]}[bo]

    cache = AsyncMock()
    cache.get_business_objects = AsyncMock(return_value=["Account", "Contact"])
    cache.get_business_components = AsyncMock(side_effect=gbc)
    reg, session = _reg_and_session(cache, discovery_max_business_objects=1)
    response = await reg.call("list_rest_enabled_components", {}, session)
    text = response["content"][0]["text"]
    assert "(1/2, truncated)" in text
    assert "Account: Account" in text
    assert "Contact" not in text  # only the first `cap` BOs are enumerated


@pytest.mark.asyncio
async def test_unknown_business_object_returns_not_found() -> None:
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value=None)
    reg, session = _reg_and_session(cache)
    response = await reg.call("list_rest_enabled_components", {"business_object": "Nope"}, session)
    text = response["content"][0]["text"]
    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_one_bo_describe_failure_shows_none_and_does_not_abort() -> None:
    async def gbc(bo: str) -> list[str]:
        if bo == "Contact":
            raise SiebelError("Siebel API returned 500", 500, "")
        return {"Account": ["Account"]}[bo]

    cache = AsyncMock()
    cache.get_business_objects = AsyncMock(return_value=["Account", "Contact"])
    cache.get_business_components = AsyncMock(side_effect=gbc)
    reg, session = _reg_and_session(cache)
    response = await reg.call("list_rest_enabled_components", {}, session)
    text = response["content"][0]["text"]
    assert response.get("isError") is not True
    assert "Account: Account" in text
    assert "Contact: (none)" in text


def test_list_rest_enabled_components_enabled_by_default() -> None:
    reg = ToolRegistry(AppSettings())
    names = {t["name"] for t in reg.list_tools()}
    assert "list_rest_enabled_components" in names
