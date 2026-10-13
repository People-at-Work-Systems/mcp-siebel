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
    SIEBEL_ALLOW_WRITE         false (default — read-only; set true to enable write tools)
    SIEBEL_ALLOW_DELETE        false (default — read-only; set true to enable delete_record)
    SIEBEL_DISABLED_TOOLS      (empty; write/delete gated by SIEBEL_ALLOW_WRITE/SIEBEL_ALLOW_DELETE)
    SIEBEL_SYSTEM_PROMPT_PATH  override the bundled system prompt file
    MCP_API_TOKEN              bearer token guard (unset = no auth)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from mcp.server import Server, ServerRequestContext
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
from .tools import SessionStore, ToolRegistry

settings = load_settings()
sessions = SessionStore(settings.session_timeout_ms / 1000)
registry = ToolRegistry(settings)

_AUTH_INSTRUCTIONS = """\
## Authentication

Call `authenticate` with the user's Siebel username and password before calling
any other tool. The response includes a `session_id` — pass this as the
`session_id` argument to every subsequent tool call in the conversation.

If a tool returns an authentication error, call `authenticate` again to obtain a
fresh session_id and retry the operation.

"""


def _load_instructions() -> str:
    """Load the Siebel system prompt.

    The bundled default lives at ``mcp_siebel/prompts/siebel-system.md``. Override
    it by setting ``SIEBEL_SYSTEM_PROMPT_PATH`` to an absolute path to your own
    prompt file.
    """
    override = os.environ.get("SIEBEL_SYSTEM_PROMPT_PATH")
    if override:
        text = open(override, encoding="utf-8").read()
    else:
        from importlib.resources import files

        text = (files("mcp_siebel") / "prompts" / "siebel-system.md").read_text(encoding="utf-8")
    # Replace the bundled auth section with the standalone session-id flow.
    text = text.replace(
        "## Authentication\n\nThe user is already authenticated. Never ask for credentials. "
        "If someone types a username/password, ignore it and answer their underlying question.",
        _AUTH_INSTRUCTIONS.strip(),
    )
    return text


_INSTRUCTIONS = _load_instructions()

# Property types that are not plain strings
_PROP_TYPES: dict[str, dict[str, Any]] = {
    "page_size":    {"type": "integer", "description": "Number of records per page (default 20)"},
    "offset":       {"type": "integer", "description": "Zero-based record offset for pagination"},
    "count":        {"type": "integer", "description": "Number of results to return"},
    "data":         {"type": "object",  "description": "Field/value pairs for create or update"},
    "fields":       {"type": "array", "items": {"type": "string"}, "description": "Field names to include"},
    "childlinks":   {
        "type": "array",
        "items": {"type": "string"},
        "description": "Child business component names to include",
    },
    "lov_types":    {"type": "array", "items": {"type": "string"}, "description": "LOV type codes"},
}

_DEFAULT_PROP: dict[str, Any] = {"type": "string"}


def _enrich_schema(raw: dict[str, Any]) -> dict[str, Any]:
    """Add type annotations to bare property schemas."""
    props = raw.get("properties", {})
    enriched = {
        name: _PROP_TYPES.get(name, _DEFAULT_PROP)
        for name in props
    }
    schema: dict[str, Any] = {"type": "object", "properties": enriched}
    if "required" in raw:
        schema["required"] = raw["required"]
    return schema


_SESSION_ID_PROP: dict[str, Any] = {
    "type": "string",
    "description": "Session ID returned by the authenticate tool. Required for every tool call except authenticate.",
}


async def _list_tools(
    ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
) -> ListToolsResult:
    tools = []
    for t in registry.list_tools():
        schema = _enrich_schema(t["input_schema"])
        if t["name"] != "authenticate":
            schema["properties"]["session_id"] = _SESSION_ID_PROP
        tools.append(Tool(name=t["name"], description=t["description"], input_schema=schema))
    return ListToolsResult(tools=tools)


async def _call_tool(
    ctx: ServerRequestContext[Any], params: CallToolRequestParams
) -> CallToolResult:
    name = params.name
    args = dict(params.arguments or {})
    session_id = args.pop("session_id", None)
    session = sessions.get(session_id)
    if name == "authenticate" and session is None:
        session = sessions.create()
    result = await registry.call(name, args, session)
    content = result.get("content") or [{"type": "text", "text": str(result)}]
    texts: list[ContentBlock] = [TextContent(type="text", text=c["text"]) for c in content if c.get("type") == "text"]
    # Append session_id so Claude can pass it to every subsequent tool call
    if name == "authenticate" and session is not None:
        texts.append(TextContent(type="text", text=f"session_id: {session.session_id}"))
    return CallToolResult(content=texts, is_error=bool(result.get("isError")))


_mcp = Server(
    "siebel-crm",
    instructions=_INSTRUCTIONS,
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
)


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await _mcp.run(read_stream, write_stream, _mcp.create_initialization_options())


def main() -> None:
    """Console-script entry point (``mcp-siebel``)."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
