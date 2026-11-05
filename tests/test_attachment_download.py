"""Behavior tests for the ``download_attachment`` tool (kind ``python``,
disabled by default).

The handler returns the file contents base64-encoded when the decoded size is
under ``SIEBEL_ATTACHMENT_MAX_BYTES``, and metadata only (name, extension, size,
attachment id) with a notice when over the cap. Driven through ``ToolRegistry.call``
with an override spec that flips the tool to ``enabled: true`` (the bundled spec
ships it fail-closed).
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.siebel_client import SiebelError
from mcp_siebel.tools import SessionContext, ToolRegistry


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _registry_and_session(tmp_path, client: AsyncMock, **settings_overrides) -> tuple[ToolRegistry, SessionContext]:
    spec = deepcopy(_bundled_spec())
    tools = spec["tools"]  # type: ignore[index]
    tools["download_attachment"] = {**tools["download_attachment"], "enabled": True}  # type: ignore[index]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    settings = AppSettings(**settings_overrides)
    settings.tools_spec_path = str(path)
    cache = AsyncMock()
    cache.resolve_business_object = AsyncMock(return_value="Account")
    session = SessionContext(session_id="s1", username="tester", client=client, cache=cache)
    return ToolRegistry(settings), session


def _client(record: dict | Exception) -> AsyncMock:
    client = AsyncMock()
    if isinstance(record, Exception):
        client.download_attachment = AsyncMock(side_effect=record)
    else:
        client.download_attachment = AsyncMock(return_value=record)
    return client


@pytest.mark.asyncio
async def test_download_under_cap_returns_base64_body(tmp_path) -> None:
    record = {
        "Attachment Id": "AAAA",
        "FileName": "report",
        "FileExt": "pdf",
        "File Size": "3",
    }
    reg, session = _registry_and_session(tmp_path, _client(record))
    response = await reg.call(
        "download_attachment",
        {"business_object": "Account", "parent_id": "1-A", "attachment_bc": "Account Attachment", "id": "1-B"},
        session,
    )
    assert response.get("isError") is not True
    text = response["content"][0]["text"]
    assert "Attachment: report.pdf" in text
    assert "AAAA" in text  # base64 body returned


@pytest.mark.asyncio
async def test_download_over_cap_returns_metadata_only(tmp_path) -> None:
    record = {"Attachment Id": "AAAA", "FileName": "report", "FileExt": "pdf", "File Size": "3"}
    # cap=1 byte; "AAAA" decodes to ~3 bytes -> over cap.
    reg, session = _registry_and_session(tmp_path, _client(record), attachment_max_bytes=1)
    response = await reg.call(
        "download_attachment",
        {"business_object": "Account", "parent_id": "1-A", "attachment_bc": "Account Attachment", "id": "1-B"},
        session,
    )
    assert response.get("isError") is not True
    text = response["content"][0]["text"]
    assert "Attachment: report.pdf" in text
    assert "exceeds the SIEBEL_ATTACHMENT_MAX_BYTES cap" in text
    assert "AAAA" not in text  # body withheld


@pytest.mark.asyncio
async def test_download_with_no_payload_returns_metadata_only(tmp_path) -> None:
    record = {"FileName": "report", "FileExt": "pdf"}  # no Attachment Id
    reg, session = _registry_and_session(tmp_path, _client(record))
    response = await reg.call(
        "download_attachment",
        {"business_object": "Account", "parent_id": "1-A", "attachment_bc": "Account Attachment", "id": "1-B"},
        session,
    )
    text = response["content"][0]["text"]
    assert "No file payload was returned" in text
    assert "Attachment: report.pdf" in text


@pytest.mark.asyncio
async def test_download_siebel_error_is_error_result(tmp_path) -> None:
    client = _client(SiebelError("Siebel API returned 404", 404, "not found"))
    reg, session = _registry_and_session(tmp_path, client)
    response = await reg.call(
        "download_attachment",
        {"business_object": "Account", "parent_id": "1-A", "attachment_bc": "Account Attachment", "id": "1-B"},
        session,
    )
    assert response.get("isError") is True


def test_download_attachment_disabled_by_default() -> None:
    reg = ToolRegistry(AppSettings())
    names = {t["name"] for t in reg.list_tools()}
    assert "download_attachment" not in names
