"""File-system watcher that pushes tool-spec changes to the registry.

A ``watchfiles`` ``awatch`` runs as an asyncio task started by each transport
alongside its MCP server. It watches the spec file's *directory* (so an
editor's atomic temp-file + rename replace is detected) and filters events to
the spec path, debounces bursts (~300 ms), and calls ``registry.reload()`` on a
quiet change. The registry's lazy mtime check in ``list_tools()`` stays as a
fallback, so a missed event still heals on the next tools/list — the watcher is
the prompt path, not the only path.

``SIEBEL_TOOLS_SPEC_WATCH=false`` disables the watcher (the lazy fallback still
applies). Python handler modules are NOT watched — a code change still needs a
restart; only the JSON spec is hot-reloaded.

This module is framework code: it imports only ``config``/``tools`` and
``watchfiles`` — never ``mcp_siebel.handlers``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from watchfiles import Change, awatch

if TYPE_CHECKING:
    from .config import AppSettings
    from .tools import ToolRegistry

log = logging.getLogger("mcp-siebel")

# Debounce window for coalescing a burst of editor writes (temp file + rename,
# repeated saves) into a single reload. About 300 ms per the spec.
_DEFAULT_DEBOUNCE_MS = 300


class ToolSpecWatcher:
    """Starts an asyncio task that reloads the registry when the spec file
    changes. Owned by a transport; ``start()`` spawns the task on the running
    loop and ``stop()`` cancels it cleanly (no leaked task, no warning)."""

    def __init__(
        self,
        registry: ToolRegistry,
        settings: AppSettings,
        *,
        debounce_ms: int = _DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._spec_path = os.path.abspath(registry.spec_path)
        self._spec_dir = os.path.dirname(self._spec_path) or "."
        self._debounce_ms = debounce_ms
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    def _is_spec(self, path: str) -> bool:
        # Compare by absolute path so a delete event (file already gone, samefile
        # would raise) still matches; the reload handles a missing file by
        # keeping the last valid set.
        try:
            return os.path.abspath(path) == self._spec_path
        except (OSError, ValueError):
            return False

    def _watch_filter(self, change: Change, path: str) -> bool:
        return self._is_spec(path)

    async def _run(self) -> None:
        assert self._stop_event is not None
        try:
            async for changes in awatch(
                self._spec_dir,
                stop_event=self._stop_event,
                debounce=self._debounce_ms,
                recursive=False,
                watch_filter=self._watch_filter,
            ):
                if not changes:
                    continue
                log.info("Tool spec change detected (%d event(s)), reloading", len(changes))
                try:
                    self._registry.reload()
                except Exception:  # noqa: BLE001 — a reload fault must not kill the watcher
                    log.exception("Watcher-triggered reload failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — log and exit; the lazy fallback still heals
            log.exception("Tool spec watcher stopped unexpectedly")

    def start(self) -> None:
        """Spawn the watcher task on the running loop. No-op if disabled or
        already running. Must be called inside a running event loop."""
        if not self._settings.tools_spec_watch:
            log.info("Tool spec watcher disabled (SIEBEL_TOOLS_SPEC_WATCH=false)")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="mcp-siebel-tool-spec-watcher")
        log.info("Tool spec watcher started on %s", self._spec_path)

    async def stop(self) -> None:
        """Signal the watcher to stop and await its exit. Safe to call when not
        started or already stopped. Cancels the task if it doesn't exit within
        a short grace period (watchfiles' stop_event poll)."""
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    log.exception("Tool spec watcher task raised on cancel")
            except asyncio.CancelledError:
                pass
        self._task = None
        self._stop_event = None
