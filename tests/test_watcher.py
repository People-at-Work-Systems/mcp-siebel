"""Tests for the tool-spec file watcher (hot reload, Task B).

The watcher (``watchfiles`` ``awatch``) watches the spec file's directory,
filters events to the spec path (so an editor's atomic temp-file + rename
replace is detected), debounces bursts, and calls ``registry.reload()`` on a
quiet change. The lazy mtime check in ``list_tools()`` stays as a fallback.
``SIEBEL_TOOLS_SPEC_WATCH=false`` disables the watcher. A failed reload keeps
the last valid set and fires no change hook.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import ToolRegistry
from mcp_siebel.watcher import ToolSpecWatcher
from tests.test_hot_reload import _bundled_tools, _write_spec
from tests.test_tool_specs import _entry


def _version_spec(path, n: int) -> None:
    """A spec whose probe tool's description + preferred_fields mark version n.
    Each version changes the exposed tool list (description) -> real change."""
    probe = _entry(
        f"probe v{n}", {}, kind="python", handler="mcp_siebel.handlers.probe:probe"
    )
    _write_spec(
        path,
        {"authenticate": _bundled_tools()["authenticate"], "probe": probe},
        {"Probe": [str(n)]},
    )


def _settings(path, *, watch: bool = True) -> AppSettings:
    s = AppSettings()
    s.tools_spec_path = str(path)
    s.tools_spec_watch = watch
    return s


async def _wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


async def _settle() -> None:
    # Give awatch's background notify thread time to arm the watch before the
    # test writes — else the very first event can race the watch registration.
    await asyncio.sleep(0.25)


@pytest.mark.asyncio
async def test_watcher_in_place_write_triggers_one_reload(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    watcher = ToolSpecWatcher(reg, _settings(spec))
    fired: list[int] = []
    reg.subscribe(lambda snap: fired.append(snap.spec_version))
    watcher.start()
    try:
        await _settle()
        _version_spec(spec, 2)
        assert await _wait_for(lambda: reg._snapshot.spec_version == 2), "reload did not run"
        assert fired == [2]
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_atomic_replace_triggers_reload(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    watcher = ToolSpecWatcher(reg, _settings(spec))
    watcher.start()
    try:
        await _settle()
        # Editor-style atomic replace: write a temp file, then os.replace onto
        # the spec path. The directory watcher sees the rename onto spec.json.
        tmp = spec.with_name("spec.json.tmp")
        _version_spec(tmp, 2)
        os.replace(tmp, spec)
        assert await _wait_for(lambda: reg._snapshot.spec_version == 2), "atomic replace not detected"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_burst_writes_collapse_into_one_reload(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    watcher = ToolSpecWatcher(reg, _settings(spec))
    fired: list[int] = []
    reg.subscribe(lambda snap: fired.append(snap.spec_version))
    watcher.start()
    try:
        await _settle()
        # Three writes within the debounce window (20 ms apart << 300 ms) must
        # collapse into a single reload that sees the final content (v4).
        for n in (2, 3, 4):
            _version_spec(spec, n)
            await asyncio.sleep(0.02)
        assert await _wait_for(lambda: reg._snapshot.spec_version == 2), "reload did not run"
        # Only one reload -> one hook fire, and the probe description is v4.
        assert fired == [2]
        assert reg._snapshot.tools["probe"].description == "probe v4"
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_invalid_write_keeps_last_valid_and_no_hook(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    watcher = ToolSpecWatcher(reg, _settings(spec))
    fired: list[int] = []
    reg.subscribe(lambda snap: fired.append(snap.spec_version))
    watcher.start()
    mtime_before = reg._spec_mtime
    try:
        await _settle()
        spec.write_text("{not valid json", encoding="utf-8")
        # The reload is attempted (mtime advances even on a failed reload) but
        # the hook does not fire and the last valid set is kept.
        assert await _wait_for(lambda: reg._spec_mtime != mtime_before), "reload not attempted"
        # Give the hook a moment to (incorrectly) fire.
        await asyncio.sleep(0.1)
        assert fired == []
        assert reg._snapshot.spec_version == 1
        assert "probe" in reg._snapshot.tools  # last valid set kept
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_disabled_no_reload_until_list_tools(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    settings = _settings(spec, watch=False)
    reg = ToolRegistry(settings)
    watcher = ToolSpecWatcher(reg, settings)
    fired: list[int] = []
    reg.subscribe(lambda snap: fired.append(snap.spec_version))
    watcher.start()  # no-op when disabled
    try:
        _version_spec(spec, 2)
        # No watcher-driven reload.
        await asyncio.sleep(0.3)
        assert reg._snapshot.spec_version == 1
        assert fired == []
        # The lazy mtime fallback in list_tools() still heals.
        reg.list_tools()
        assert reg._snapshot.spec_version == 2
        assert fired == [2]
    finally:
        await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_stop_is_clean_no_leaked_task(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    watcher = ToolSpecWatcher(reg, _settings(spec))
    watcher.start()
    assert watcher._task is not None and not watcher._task.done()
    await watcher.stop()
    assert watcher._task is None
    # No lingering mcp-siebel-tool-spec-watcher task on the loop.
    assert not any(getattr(t, "get_name", lambda: "")() == "mcp-siebel-tool-spec-watcher" for t in asyncio.all_tasks())
