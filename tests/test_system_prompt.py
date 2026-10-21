"""Tests for the shared system-prompt loader (``system_prompt.load_instructions``).

The loader is LLM-facing instructions delivered to the MCP ``Server(instructions=…)``.
It reads ``SIEBEL_SYSTEM_PROMPT_PATH`` from the process environment, falls back to the
bundled ``prompts/siebel-system.md`` when unset or unreadable, and rewrites the
bundled auth section to the standalone session-id flow (naming the session tool).
"""

from __future__ import annotations

import logging

import pytest

from mcp_siebel.system_prompt import load_instructions

_SESSION_TOOL = "authenticate"
_BUNDLED_AUTH_ANCHOR = (
    "## Authentication\n\nThe user is already authenticated. Never ask for credentials. "
    "If someone types a username/password, ignore it and answer their underlying question."
)


def test_bundled_prompt_loads_and_auth_section_is_replaced() -> None:
    text = load_instructions(_SESSION_TOOL)
    # the bundled auth anchor is gone…
    assert _BUNDLED_AUTH_ANCHOR not in text
    # …replaced by the standalone session-id flow naming the session tool.
    assert f"Call `{_SESSION_TOOL}` with the user's Siebel username" in text
    assert "session_id" in text


def test_override_file_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    custom = tmp_path / "prompt.md"
    custom.write_text("# My custom Siebel prompt.\n", encoding="utf-8")
    monkeypatch.setenv("SIEBEL_SYSTEM_PROMPT_PATH", str(custom))
    text = load_instructions(_SESSION_TOOL)
    assert text.startswith("# My custom Siebel prompt.")


def test_override_with_auth_anchor_has_session_flow(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    custom = tmp_path / "prompt.md"
    custom.write_text(
        "# My prompt.\n\n"
        + _BUNDLED_AUTH_ANCHOR
        + "\n\nMore guidance.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIEBEL_SYSTEM_PROMPT_PATH", str(custom))
    text = load_instructions(_SESSION_TOOL)
    # the anchor was present, so the auth section is rewritten.
    assert _BUNDLED_AUTH_ANCHOR not in text
    assert f"Call `{_SESSION_TOOL}` with the user's Siebel username" in text
    assert "More guidance." in text  # the rest of the override is preserved


def test_override_without_auth_anchor_used_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An override file that doesn't contain the bundled auth anchor is used as-is;
    the `.replace` no-ops, so a deployer's custom prompt keeps its own auth guidance."""
    custom = tmp_path / "prompt.md"
    custom.write_text("# My prompt.\n\n## Authentication\n\nDo it my way.\n", encoding="utf-8")
    monkeypatch.setenv("SIEBEL_SYSTEM_PROMPT_PATH", str(custom))
    text = load_instructions(_SESSION_TOOL)
    assert "Do it my way." in text
    assert f"Call `{_SESSION_TOOL}` with the user's Siebel username" not in text


def test_missing_override_falls_back_to_bundled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-existent override file is non-fatal: log a warning and use the bundled prompt."""
    monkeypatch.setenv("SIEBEL_SYSTEM_PROMPT_PATH", "/does/not/exist/prompt.md")
    with caplog.at_level(logging.WARNING, logger="mcp-siebel"):
        text = load_instructions(_SESSION_TOOL)
    # the bundled prompt was used (auth section replaced with the session-id flow).
    assert _BUNDLED_AUTH_ANCHOR not in text
    assert f"Call `{_SESSION_TOOL}` with the user's Siebel username" in text
    # a warning was logged naming the bad path.
    assert any("/does/not/exist/prompt.md" in r.message for r in caplog.records)


def test_unset_uses_bundled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIEBEL_SYSTEM_PROMPT_PATH", raising=False)
    text = load_instructions(_SESSION_TOOL)
    assert f"Call `{_SESSION_TOOL}` with the user's Siebel username" in text
