"""Tests for spec-change notifications (hot reload, Task C).

MCP clients get ``notifications/tools/list_changed`` (the server advertises
``tools.listChanged`` and each transport broadcasts to its active sessions on a
real spec change). REST clients get ``spec_version`` as an ETag on ``GET
/tools`` (``If-None-Match`` -> 304) and a ``tools_changed`` SSE event with the
new ``spec_version`` on ``GET /tools/events``. A dead session/client is dropped
and never breaks the loop for the others.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from mcp.server import NotificationOptions

from mcp_siebel.notifications import (
    McpSessionBroadcaster,
    SseSubscribers,
    make_spec_changed_hook,
    sse_events_stream,
)
from mcp_siebel.tools import ToolRegistry
from tests.test_tool_specs import _bump_mtime
from tests.test_watcher import _settings, _version_spec


class _FakeSession:
    """Stand-in for an MCP ServerSession: counts list_changed sends, optionally
    raises to simulate a dead/broken session. Supports weakref (plain class)."""

    def __init__(self, *, raise_: bool = False) -> None:
        self.calls = 0
        self._raise = raise_

    async def send_tool_list_changed(self) -> None:
        self.calls += 1
        if self._raise:
            raise RuntimeError("session is dead")


# ---------------------------------------------------------------------------
# Server capability advertisement
# ---------------------------------------------------------------------------


def test_stdio_advertises_tools_list_changed() -> None:
    from mcp_siebel.stdio import _mcp

    opts = _mcp.create_initialization_options(NotificationOptions(tools_changed=True))
    assert opts.capabilities.tools is not None
    assert opts.capabilities.tools.list_changed is True


def test_http_advertises_tools_list_changed() -> None:
    from mcp_siebel.http_server import _mcp

    opts = _mcp.create_initialization_options(NotificationOptions(tools_changed=True))
    assert opts.capabilities.tools is not None
    assert opts.capabilities.tools.list_changed is True


# ---------------------------------------------------------------------------
# Broadcaster — one list_changed per live session; dead ones dropped
# ---------------------------------------------------------------------------


async def test_broadcaster_notify_all_sends_to_each_live_session() -> None:
    b = McpSessionBroadcaster()
    s1, s2 = _FakeSession(), _FakeSession()
    b.add(s1)
    b.add(s2)
    await b.notify_all()
    assert s1.calls == 1
    assert s2.calls == 1


async def test_broadcaster_skips_and_drops_dead_session() -> None:
    b = McpSessionBroadcaster()
    live, dead = _FakeSession(), _FakeSession(raise_=True)
    b.add(live)
    b.add(dead)
    await b.notify_all()  # dead raises -> caught + dropped; live still notified
    assert live.calls == 1
    assert dead.calls == 1
    await b.notify_all()  # dead is gone -> only live is called again
    assert live.calls == 2
    assert dead.calls == 1


# ---------------------------------------------------------------------------
# Registry hook -> broadcaster: exactly one list_changed per real change
# ---------------------------------------------------------------------------


async def test_registry_hook_broadcasts_one_per_real_change(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    b = McpSessionBroadcaster()
    fake = _FakeSession()
    b.add(fake)
    reg.subscribe(make_spec_changed_hook(b))

    _version_spec(spec, 2)
    _bump_mtime(spec)
    reg.reload()
    await asyncio.sleep(0.05)  # let the scheduled notify_all task run
    assert fake.calls == 1

    # Identical content (touched file) -> no hook, no new notification.
    _version_spec(spec, 2)
    _bump_mtime(spec)
    reg.reload()
    await asyncio.sleep(0.05)
    assert fake.calls == 1

    # Another real change -> exactly one more.
    _version_spec(spec, 3)
    _bump_mtime(spec)
    reg.reload()
    await asyncio.sleep(0.05)
    assert fake.calls == 2


# ---------------------------------------------------------------------------
# REST /tools ETag + If-None-Match -> 304
# ---------------------------------------------------------------------------


def test_tools_endpoint_etag_and_304() -> None:
    from mcp_siebel.http_server import app

    client = TestClient(app)
    r = client.get("/tools")
    assert r.status_code == 200
    etag = r.headers["etag"]
    assert etag == '"1"'  # bundled spec -> spec_version 1
    body = r.json()
    assert body["spec_version"] == 1
    assert any(t["name"] == "authenticate" for t in body["tools"])

    # Matching If-None-Match -> 304, empty body.
    r304 = client.get("/tools", headers={"If-None-Match": etag})
    assert r304.status_code == 304
    assert r304.content == b""

    # Non-matching -> 200 with the body.
    r200 = client.get("/tools", headers={"If-None-Match": '"999"'})
    assert r200.status_code == 200
    assert r200.json()["spec_version"] == 1


# ---------------------------------------------------------------------------
# REST /tools/events SSE stream
# ---------------------------------------------------------------------------


async def test_sse_events_stream_emits_initial_change_and_keepalive(tmp_path) -> None:
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    subs = SseSubscribers()
    gen = sse_events_stream(reg, subs, keepalive_s=0.05)
    aiter = gen.__aiter__()
    try:
        first = await aiter.__anext__()
        assert "event: tools_changed" in first
        assert '"spec_version": 1' in first

        subs.broadcast(2)
        second = await aiter.__anext__()
        assert "event: tools_changed" in second
        assert '"spec_version": 2' in second

        third = await aiter.__anext__()  # 0.05s of quiet -> keepalive
        assert third == ": keepalive\n\n"
    finally:
        await gen.aclose()


async def test_sse_dead_client_does_not_affect_others() -> None:
    subs = SseSubscribers()
    q1: asyncio.Queue[int] = asyncio.Queue()
    q2: asyncio.Queue[int] = asyncio.Queue()
    unsub1 = subs.add(q1)
    subs.add(q2)

    subs.broadcast(1)
    assert q1.get_nowait() == 1
    assert q2.get_nowait() == 1

    unsub1()  # client 1 is gone
    subs.broadcast(2)
    assert q2.get_nowait() == 2
    assert q1.empty()  # the dead client's queue receives nothing


async def test_sse_stream_unsubscribes_on_close(tmp_path) -> None:
    """Closing the generator (client disconnect) unsubscribes its queue so the
    SseSubscribers set doesn't keep pushing to a dead client."""
    spec = tmp_path / "spec.json"
    _version_spec(spec, 1)
    reg = ToolRegistry(_settings(spec))
    subs = SseSubscribers()
    gen = sse_events_stream(reg, subs, keepalive_s=10)
    aiter = gen.__aiter__()
    await aiter.__anext__()  # initial event -> queue subscribed
    assert len(subs._queues) == 1
    await gen.aclose()
    assert len(subs._queues) == 0
