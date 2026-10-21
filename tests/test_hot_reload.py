"""Tests for the atomic-snapshot reload (hot reload, Task A).

The registry serves an immutable ``ToolSnapshot`` holding the tool map,
``preferred_fields``, the session-tool name, a monotonic ``spec_version``, and a
content hash of the exposed tool list. A reload builds a fresh snapshot and
swaps the single reference under the lock, so ``list_tools()`` and ``call()``
read it exactly once and never mix data from two spec versions. A change hook
fires only after a successful reload whose *exposed* tool list actually changed
(identical content or a failed reload fire nothing and bump nothing).
``/health`` exposes ``spec_version`` and the content hash. The
"not authenticated" message is built from the registry's session-tool name, so
no framework module hard-codes a tool name.
"""

from __future__ import annotations

import json
import threading
import time

from mcp_siebel.config import AppSettings
from mcp_siebel.tools import ToolContext, ToolRegistry, ToolSnapshot
from tests.test_tool_specs import (
    _bump_mtime,
    _bundled_defs,
    _bundled_tools,
    _entry,
)


def _write_spec(path, tools: dict[str, object], preferred_fields: dict[str, list[str]] | None = None) -> None:
    """Write a spec including the bundled $defs (so the bundled authenticate
    entry resolves) and an optional top-level preferred_fields section."""
    payload: dict[str, object] = {"$defs": _bundled_defs(), "tools": tools}
    if preferred_fields is not None:
        payload["preferred_fields"] = preferred_fields
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def _probe_entry() -> dict[str, object]:
    return _entry("probe tool", {}, kind="python", handler="mcp_siebel.handlers.probe:probe")


def _v1_spec(path) -> None:
    _write_spec(
        path,
        {"authenticate": _bundled_tools()["authenticate"], "probe": _probe_entry()},
        {"Probe": ["a"]},
    )


def _v2_spec(path) -> None:
    # v2 drops "probe" and changes preferred_fields — a call to "probe" must
    # then be "Unknown tool", never the v2 marker.
    _write_spec(path, {"authenticate": _bundled_tools()["authenticate"]}, {"Probe": ["b"]})


def _registry(path) -> ToolRegistry:
    settings = AppSettings()
    settings.tools_spec_path = str(path)
    return ToolRegistry(settings)


# ---------------------------------------------------------------------------
# Snapshot object + spec_version / content hash
# ---------------------------------------------------------------------------


def test_snapshot_holds_consistent_view_and_versions(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _v1_spec(spec)
    reg = _registry(spec)

    snap = reg._snapshot
    assert isinstance(snap, ToolSnapshot)
    assert "probe" in snap.tools
    assert snap.preferred_fields == {"Probe": ["a"]}
    assert snap.session_tool_name == "authenticate"
    assert snap.spec_version == 1
    assert isinstance(snap.content_hash, str) and snap.content_hash

    # Real change -> version 2, hash changes, probe gone.
    v1_hash = snap.content_hash
    _v2_spec(spec)
    _bump_mtime(spec)
    reg.reload()
    snap2 = reg._snapshot
    assert snap2.spec_version == 2
    assert snap2.content_hash != v1_hash
    assert "probe" not in snap2.tools
    assert snap2.preferred_fields == {"Probe": ["b"]}

    # Identical content (touched file) -> no version bump, no hash change.
    _v2_spec(spec)
    _bump_mtime(spec)
    reg.reload()
    snap3 = reg._snapshot
    assert snap3.spec_version == 2
    assert snap3.content_hash == snap2.content_hash


def test_health_status_exposes_spec_version_and_hash(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _v1_spec(spec)
    reg = _registry(spec)
    health = reg.health_status()
    assert health["spec_version"] == 1
    assert isinstance(health["content_hash"], str) and health["content_hash"]
    # Still reports the loaded tool count and spec path.
    assert health["tools"] == 2
    assert health["spec_path"] == str(spec)

    _v2_spec(spec)
    _bump_mtime(spec)
    reg.reload()
    health2 = reg.health_status()
    assert health2["spec_version"] == 2
    assert health2["content_hash"] != health["content_hash"]


# ---------------------------------------------------------------------------
# Change hook — fires only on a real exposed-list change
# ---------------------------------------------------------------------------


def test_change_hook_fires_only_on_real_change(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _v1_spec(spec)
    reg = _registry(spec)

    fired: list[int] = []
    unsub = reg.subscribe(lambda snap: fired.append(snap.spec_version))

    # Real change -> fires once with the new version.
    _v2_spec(spec)
    _bump_mtime(spec)
    reg.reload()
    assert fired == [2]

    # Identical content (touched file) -> no fire.
    _v2_spec(spec)
    _bump_mtime(spec)
    reg.reload()
    assert fired == [2]

    # Failed reload (corrupt) -> no fire, last valid set kept.
    spec.write_text("{not valid json", encoding="utf-8")
    _bump_mtime(spec)
    reg.reload()
    assert fired == [2]
    assert reg._snapshot.spec_version == 2
    assert "authenticate" in reg._snapshot.tools

    # Another real change -> fires again.
    _v1_spec(spec)
    _bump_mtime(spec)
    reg.reload()
    assert fired == [2, 3]

    # Unsubscribe -> no further fires.
    unsub()
    _v2_spec(spec)
    _bump_mtime(spec)
    reg.reload()
    assert fired == [2, 3]


# ---------------------------------------------------------------------------
# Atomicity — concurrent call() during reload never mixes versions
# ---------------------------------------------------------------------------


def test_concurrent_call_never_mixes_tools_and_preferred_fields(tmp_path) -> None:
    """A call to ``probe`` must return either the v1 marker (probe present, v1
    preferred_fields) or "Unknown tool" (probe absent in v2) — never the v2
    marker, which would mean probe was found from v1's tool map but the v2
    preferred_fields were used (a torn read across two spec versions)."""
    spec = tmp_path / "spec.json"
    _v1_spec(spec)
    reg = _registry(spec)

    stop = threading.Event()
    mismatches: list[str] = []

    def reloader() -> None:
        toggle = False
        while not stop.is_set():
            (_v2_spec if toggle else _v1_spec)(spec)
            _bump_mtime(spec)
            try:
                reg.reload()
            except Exception:  # noqa: BLE001 — keep reloading regardless
                pass
            toggle = not toggle

    def caller() -> None:
        while not stop.is_set():
            result = _run_call(reg, "probe", {})
            text = _result_text(result)
            # "b" is the torn-read signature: probe found (v1 tools) + v2 pf.
            if text == "b":
                mismatches.append(text)

    t1 = threading.Thread(target=reloader)
    t2 = threading.Thread(target=caller)
    t1.start()
    t2.start()
    time.sleep(0.5)
    stop.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not mismatches, f"torn read observed: probe returned v2 marker {mismatches!r}"


def _run_call(reg: ToolRegistry, name: str, args: dict[str, object]) -> dict[str, object]:
    import asyncio

    return asyncio.run(reg.call(name, args, None))


def _result_text(result: dict[str, object]) -> str:
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        return str(content[0].get("text", ""))
    return str(result)


# ---------------------------------------------------------------------------
# Auth message built from session_tool_name (no hard-coded tool name)
# ---------------------------------------------------------------------------


def test_auth_message_uses_session_tool_name(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _v1_spec(spec)
    reg = _registry(spec)

    # With the bundled session tool named "authenticate", the message is
    # byte-identical to the historical hard-coded one.
    ctx = ToolContext(settings=reg.settings, session=None, session_tool_name="authenticate")
    from mcp_siebel.common import _require_auth

    assert _require_auth(ctx) == {
        "content": [{"type": "text", "text": "Not authenticated. Call the authenticate tool first."}],
        "isError": True,
    }

    # Renaming the session tool changes the message — framework code holds no
    # tool-name literal.
    ctx2 = ToolContext(settings=reg.settings, session=None, session_tool_name="login")
    assert "Call the login tool first." in _result_text(_require_auth(ctx2))


def test_call_injects_session_tool_name_into_context() -> None:
    """registry.call builds the ToolContext with the snapshot's session tool
    name, so a not-authenticated call surfaces it without hard-coding. Uses the
    bundled spec (session tool "authenticate") -> byte-identical message."""
    reg = ToolRegistry(AppSettings())
    # query_data's executor calls _require_auth(ctx) before touching Siebel, so a
    # call with valid args and no session returns the not-authenticated message
    # built from the session-tool name injected by registry.call. (Args must be
    # schema-valid so registry-level argument validation lets the call reach the
    # handler's auth gate.)
    result = _run_call(reg, "query_data", {"business_object": "Account"})
    assert _result_text(result) == "Not authenticated. Call the authenticate tool first."
