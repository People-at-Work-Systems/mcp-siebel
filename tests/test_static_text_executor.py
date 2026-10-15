"""Unit tests for the ``static_text`` kind: the ``text`` option (string or list
of strings joined with ``\\n``), load-time validation, and that the tool returns
the text verbatim with no auth required.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import SessionContext, ToolRegistry, _SpecError


def _bundled_spec() -> dict[str, object]:
    return json.loads(files("mcp_siebel").joinpath("specs", "tool_specs.json").read_text(encoding="utf-8"))


def _spec_path(tmp_path, extra: dict[str, dict[str, object]]) -> str:
    spec = deepcopy(_bundled_spec())
    spec["tools"].update(extra)  # type: ignore[union-attr]
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return str(path)


def _static(text: object, *, access: str = "read", props: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "description": "fixture static_text tool",
        "kind": "static_text",
        "access": access,
        "text": text,
        "input_schema": {"type": "object", "properties": props or {}},
    }


def _reg(tmp_path, extra: dict[str, dict[str, object]]) -> ToolRegistry:
    settings = AppSettings()
    settings.tools_spec_path = _spec_path(tmp_path, extra)
    return ToolRegistry(settings)


# --- load-time validation ---


def test_static_text_access_must_be_read(tmp_path) -> None:
    with pytest.raises(_SpecError, match="permits access"):
        _reg(tmp_path, {"x": _static("hi", access="write")})


def test_static_text_text_required(tmp_path) -> None:
    entry = _static("hi")
    del entry["text"]
    with pytest.raises(_SpecError, match="missing required option"):
        _reg(tmp_path, {"x": entry})


def test_static_text_text_must_be_str_or_list(tmp_path) -> None:
    with pytest.raises(_SpecError, match="must be a string or a list of strings"):
        _reg(tmp_path, {"x": _static(123)})


def test_static_text_list_must_contain_only_strings(tmp_path) -> None:
    with pytest.raises(_SpecError, match="list must contain only strings"):
        _reg(tmp_path, {"x": _static(["a", 2])})


def test_static_text_unknown_option_rejected(tmp_path) -> None:
    entry = _static("hi")
    entry["render"] = "query_prose"
    with pytest.raises(_SpecError, match="unknown option"):
        _reg(tmp_path, {"x": entry})


# --- runtime ---


@pytest.mark.asyncio
async def test_static_text_string_returned_verbatim(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _static("hello world")})
    resp = await reg.call("x", {}, SessionContext(session_id="s1", username="t"))
    assert resp.get("isError") is not True
    assert resp["content"][0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_static_text_list_joined_with_newline(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _static(["one", "two", "three"])})
    resp = await reg.call("x", {}, SessionContext(session_id="s1", username="t"))
    assert resp["content"][0]["text"] == "one\ntwo\nthree"


@pytest.mark.asyncio
async def test_static_text_needs_no_session(tmp_path) -> None:
    """static_text works without a client/cache — like get_query_syntax did."""
    reg = _reg(tmp_path, {"x": _static("no auth needed")})
    resp = await reg.call("x", {}, None)
    assert resp["content"][0]["text"] == "no auth needed"


@pytest.mark.asyncio
async def test_static_text_ignores_caller_args(tmp_path) -> None:
    reg = _reg(tmp_path, {"x": _static("fixed", props={"q": {"type": "string"}})})
    resp = await reg.call("x", {"q": "anything"}, AsyncMock())
    assert resp["content"][0]["text"] == "fixed"
