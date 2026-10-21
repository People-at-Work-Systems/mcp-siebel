#!/usr/bin/env python3
"""Stdio MCP launcher for Claude Desktop.

Claude Desktop (or any stdio MCP client) runs this via the ``mcp-siebel``
console script and communicates over stdio. Environment variables are read
from the process environment (set in ``claude_desktop_config.json`` or
inherited from the shell).

Required env vars (same as the HTTP server):
    SIEBEL_BASE_URL   e.g. https://siebel.example.com/siebel/v1.0
    SIEBEL_SCHEME     https (default: http)

Optional:
    SIEBEL_MCP_MODE            standalone (default)
    SIEBEL_SSO_HEADER          SBL_USER (default)
    SIEBEL_DEFAULT_PAGE_SIZE   20 (default)
    SIEBEL_SYSTEM_PROMPT_PATH  override the bundled system prompt file
    MCP_API_TOKEN              bearer token guard (unset = no auth)
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server import NotificationOptions, Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ContentBlock,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from .config import load_settings
from .notifications import McpSessionBroadcaster, make_spec_changed_hook
from .system_prompt import load_instructions
from .tools import SessionStore, ToolRegistry
from .watcher import ToolSpecWatcher

settings = load_settings()
sessions = SessionStore(settings.session_timeout_ms / 1000)
registry = ToolRegistry(settings)
watcher = ToolSpecWatcher(registry, settings)
# Fan-out for tools/list_changed: every connected MCP session is captured from
# the list/call handlers and notified on a spec change (see make_spec_changed_hook).
broadcaster = McpSessionBroadcaster()
registry.subscribe(make_spec_changed_hook(broadcaster))

_session_tool = registry.session_tool_name
# LLM-facing server instructions, delivered to the MCP client in the Initialize
# handshake. Loaded once at import (not hot-reloaded — a change needs a
# restart, unlike SIEBEL_TOOLS_SPEC_PATH). See system_prompt.py for the override
# semantics (SIEBEL_SYSTEM_PROMPT_PATH) and the missing-file fallback.
_INSTRUCTIONS = load_instructions(_session_tool)

# Injected onto every non-session tool's schema at list time. The spec JSON is
# the source of truth for each tool's own schema (types included); session_id is
# transport-specific to stdio (the client passes it as a tool argument), so it
# stays here rather than in the spec file. The session tool's name comes from the
# spec (registry.session_tool_name), not a hard-coded literal.
_SESSION_ID_PROP: dict[str, Any] = {
    "type": "string",
    "description": (
        f"Session ID returned by the {_session_tool} tool. "
        f"Required for every tool call except {_session_tool}."
    ),
}


async def _list_tools(
    ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
) -> ListToolsResult:
    broadcaster.add(getattr(ctx, "session", None))  # capture for tools/list_changed
    session_tool = registry.session_tool_name
    tools = []
    for t in registry.list_tools():
        # The registry already serves the final, typed input_schema from the
        # spec JSON (hot-reloaded by mtime). Copy it so the session_id injection
        # below never mutates the cached schema dict in the registry.
        schema = {k: (v.copy() if isinstance(v, dict) else v) for k, v in t["input_schema"].items()}
        props = dict(schema.get("properties", {}))
        if t["name"] != session_tool:
            props["session_id"] = _SESSION_ID_PROP
        schema["properties"] = props
        tools.append(Tool(name=t["name"], description=t["description"], input_schema=schema))
    return ListToolsResult(tools=tools)


async def _call_tool(
    ctx: ServerRequestContext[Any], params: CallToolRequestParams
) -> CallToolResult:
    broadcaster.add(getattr(ctx, "session", None))  # capture for tools/list_changed
    name = params.name
    args = dict(params.arguments or {})
    session_id = args.pop("session_id", None)
    session = sessions.get(session_id)
    if name == registry.session_tool_name:
        session = sessions.create()
    result = await registry.call(name, args, session)
    content = result.get("content") or [{"type": "text", "text": str(result)}]
    texts: list[ContentBlock] = [TextContent(type="text", text=c["text"]) for c in content if c.get("type") == "text"]
    # Append session_id so Claude can pass it to every subsequent tool call
    if name == registry.session_tool_name and session is not None:
        texts.append(TextContent(type="text", text=f"session_id: {session.session_id}"))
    return CallToolResult(content=texts, is_error=bool(result.get("isError")))


_mcp = Server(
    "siebel-crm",
    instructions=_INSTRUCTIONS,
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
)


async def _run() -> None:
    # Start the spec watcher on the server's loop; it reloads the registry when
    # the spec file changes and is cancelled cleanly on shutdown (no task leak).
    watcher.start()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await _mcp.run(
                read_stream,
                write_stream,
                _mcp.create_initialization_options(NotificationOptions(tools_changed=True)),
            )
    finally:
        await watcher.stop()


def main() -> None:
    """Console-script entry point (``mcp-siebel``)."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
