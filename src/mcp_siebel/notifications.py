"""Spec-change fan-out: MCP ``tools/list_changed`` + REST SSE.

Both transports own a ``McpSessionBroadcaster`` that tracks the active MCP
``ServerSession`` objects (captured from the ``tools/list`` / ``tools/call``
handlers, so it works for every protocol version) and, for the HTTP transport,
a ``SseSubscribers`` set of per-client ``asyncio`` queues backing the REST
``/tools/events`` stream.

The registry's change hook (Task A) fires synchronously in the loop thread.
``make_spec_changed_hook`` bridges that to the async world: it schedules a
``notify_all`` task that calls ``session.send_tool_list_changed()`` on every
live MCP session, and pushes the new ``spec_version`` to every REST SSE queue.
A dead session (send raises) or a gone SSE client (its generator's ``finally``
unsubscribes) is dropped so one failure never breaks the loop.

This is framework code: it imports only the MCP SDK, ``asyncio``/``weakref``,
and (for annotations) the registry — never ``mcp_siebel.handlers``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.session import ServerSession

    from .tools import ToolRegistry, ToolSnapshot

log = logging.getLogger("mcp-siebel")


class McpSessionBroadcaster:
    """Tracks live MCP sessions and broadcasts ``tools/list_changed`` to them.

    Sessions are held weakly: a session whose connection closes is GC'd and
    drops out of the set automatically. A session that is still referenced but
    whose write stream is broken raises on send; ``notify_all`` catches that,
    drops the session, and continues with the rest.
    """

    def __init__(self) -> None:
        self._sessions: weakref.WeakSet[ServerSession] = weakref.WeakSet()

    def add(self, session: ServerSession | None) -> None:
        # Idempotent — handlers may add the same session on every list/call.
        # None (a handler invoked with a minimal context, e.g. in tests) is a
        # no-op: nothing to track, nothing to notify.
        if session is None:
            return
        self._sessions.add(session)

    async def notify_all(self) -> None:
        for session in list(self._sessions):
            try:
                await session.send_tool_list_changed()
            except Exception:  # noqa: BLE001 — a dead session must not break the loop
                log.debug("dropping dead MCP session from broadcaster", exc_info=True)
                try:
                    self._sessions.discard(session)
                except Exception:  # noqa: BLE001
                    pass


class SseSubscribers:
    """Set of per-REST-SSE-client ``asyncio`` queues for ``/tools/events``.

    ``broadcast`` pushes the new ``spec_version`` to every queue (``put_nowait``
    is safe — the hook fires in the loop thread, as does the stream consumer).
    A stream's ``finally`` unsubscribes its queue when the client goes away, so
    a dead client is dropped and never affects the others.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[int]] = set()

    def add(self, queue: asyncio.Queue[int]) -> Callable[[], None]:
        self._queues.add(queue)

        def _remove() -> None:
            self._queues.discard(queue)

        return _remove

    def broadcast(self, version: int) -> None:
        for queue in list(self._queues):
            try:
                queue.put_nowait(version)
            except asyncio.QueueFull:
                log.warning("SSE subscriber queue full; dropping tools_changed event")


def _sse_event(event: str, data: dict[str, Any]) -> str:
    """One Server-Sent-Events message: an ``event:`` line + a JSON ``data:`` line."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def sse_events_stream(
    registry: ToolRegistry,
    sse_subs: SseSubscribers,
    *,
    keepalive_s: float = 15.0,
) -> AsyncIterator[str]:
    """Async generator yielding SSE messages for ``/tools/events``.

    Emits an initial ``tools_changed`` with the current ``spec_version`` (so a
    client learns the starting state on connect), then a ``tools_changed`` per
    real spec change and a ``: keepalive`` comment every ``keepalive_s`` of
    quiet. Unsubscribes its queue on close (client disconnect) so dead clients
    are reclaimed."""
    queue: asyncio.Queue[int] = asyncio.Queue()
    unsubscribe = sse_subs.add(queue)
    try:
        yield _sse_event("tools_changed", {"spec_version": registry.snapshot().spec_version})
        while True:
            try:
                version = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield _sse_event("tools_changed", {"spec_version": version})
    finally:
        unsubscribe()


def make_spec_changed_hook(
    broadcaster: McpSessionBroadcaster,
    sse_subs: SseSubscribers | None = None,
):
    """Build a sync callback for ``registry.subscribe`` that fans a spec change
    out to every MCP session (``tools/list_changed``) and every REST SSE
    subscriber. Must fire in the loop thread (the watcher and the lazy-reload
    path both do); if there is no running loop it does nothing."""

    def _on_spec_changed(snap: ToolSnapshot) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(broadcaster.notify_all())
        if sse_subs is not None:
            sse_subs.broadcast(snap.spec_version)

    return _on_spec_changed
