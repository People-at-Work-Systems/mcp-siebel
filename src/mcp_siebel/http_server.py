"""mcp-siebel HTTP/SSE server.

Exposes the Siebel tool registry/call surface over FastAPI, with per-session
Siebel authentication held server-side. This module is imported only when the
``http`` extra is installed (``pip install mcp-siebel[http]``); the stdio entry
point (``mcp-siebel``) does not import it.

MCP SSE transport (for Claude Desktop via mcp-remote)
------------------------------------------------------
A proper MCP SSE transport is mounted at:

    GET  /sse        — open SSE connection
    POST /messages/  — client → server messages

Point Claude Desktop at it via mcp-remote:

    {
      "mcpServers": {
        "siebel": {
            "command": "npx",
            "args": ["-y", "mcp-remote", "http://localhost:8201/sse"]
        }
      }
    }

The first tool to call is the session-establishment tool (the spec entry marked
``establishes_session`` — see ``registry.session_tool_name``) with
username/password to obtain a ``session_id`` required by all subsequent Siebel
tools.

REST surface: /tools, /sessions, /tools/{name}, /mcp/tools, /mcp/call/{name},
/health, /metrics.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mcp.server import NotificationOptions, Server, ServerRequestContext
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ContentBlock,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field

from . import __version__
from .config import load_settings
from .context import CorrelationMiddleware, session_id_var, turn_id_var
from .notifications import McpSessionBroadcaster, SseSubscribers, make_spec_changed_hook, sse_events_stream
from .tools import SessionStore, ToolRegistry
from .watcher import ToolSpecWatcher


class _AccessLogFilter(logging.Filter):
    """Silence uvicorn access-log noise from health/metrics probes."""

    _SILENCED_PATHS = frozenset({"/health", "/metrics"})

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args or len(record.args) < 3:
            return True
        # record.args is a tuple at runtime (uvicorn access-log positional args);
        # the logging stub types it as Mapping, so index with int needs ignoring.
        path = str(record.args[2]).split("?", 1)[0]  # type: ignore[index]
        return path not in self._SILENCED_PATHS


logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())


class _JsonFormatter(logging.Formatter):
    """Render every log record as a single JSON line for Loki/Grafana."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if session_id_var.get():
            payload["session_id"] = session_id_var.get()
        if turn_id_var.get():
            payload["turn_id"] = turn_id_var.get()
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _use_json_logging(service: str) -> None:
    formatter = _JsonFormatter(service)
    for logger_name in (None, "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(logger_name).handlers:
            handler.setFormatter(formatter)


_use_json_logging("mcp-siebel")

settings = load_settings()
sessions = SessionStore(settings.session_timeout_ms / 1000)
registry = ToolRegistry(settings)
# Spec watcher: started on app startup, stopped on shutdown (see _lifespan).
watcher = ToolSpecWatcher(registry, settings)
# Spec-change fan-out: MCP sessions get tools/list_changed; REST /tools/events
# SSE clients get a tools_changed event with the new spec_version.
broadcaster = McpSessionBroadcaster()
sse_subs = SseSubscribers()
registry.subscribe(make_spec_changed_hook(broadcaster, sse_subs))
started_at = time.monotonic()


# ---------------------------------------------------------------------------
# MCP SSE transport — wired to the shared registry/sessions above
# ---------------------------------------------------------------------------

_sse = SseServerTransport("/messages/")


async def _list_tools(
    ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
) -> ListToolsResult:
    broadcaster.add(getattr(ctx, "session", None))  # capture for tools/list_changed
    return ListToolsResult(
        tools=[
            Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"])
            for t in registry.list_tools()
        ]
    )


async def _call_tool(
    ctx: ServerRequestContext[Any], params: CallToolRequestParams
) -> CallToolResult:
    broadcaster.add(getattr(ctx, "session", None))  # capture for tools/list_changed
    name = params.name
    args = dict(params.arguments or {})
    session_id = args.pop("session_id", None)
    session = sessions.get(session_id)
    if name == registry.session_tool_name and session is None:
        session = sessions.create()
    result = await registry.call(name, args, session)
    if name == registry.session_tool_name and session is not None:
        result = {**result, "session_id": session.session_id}
    content = result.get("content") or [{"type": "text", "text": str(result)}]
    texts: list[ContentBlock] = [TextContent(type="text", text=c["text"]) for c in content if c.get("type") == "text"]
    return CallToolResult(content=texts, is_error=bool(result.get("isError")))


_mcp = Server("siebel-crm", on_list_tools=_list_tools, on_call_tool=_call_tool)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Start the spec watcher on the server's loop; stop it cleanly on shutdown
    # so no asyncio task leaks (and tests see no dangling-task warnings).
    watcher.start()
    try:
        yield
    finally:
        await watcher.stop()


app = FastAPI(
    title="MCP Siebel",
    description="MCP-style tool server for Oracle Siebel 26.2+",
    version=__version__,
    lifespan=_lifespan,
)

Instrumentator().instrument(app).expose(app)
app.add_middleware(CorrelationMiddleware)


class SessionCreateRequest(BaseModel):
    username: str
    password: str | None = None


class SessionCreateResponse(BaseModel):
    session_id: str
    result: dict[str, Any]


class ToolCallRequest(BaseModel):
    session_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


async def require_api_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": settings.mcp_mode,
        "uptime": int(time.monotonic() - started_at),
        "sessions": sessions.count(),
        **registry.health_status(),
    }


@app.get("/sessions/{session_id}", dependencies=[Depends(require_api_token)])
async def get_session(session_id: str) -> dict[str, Any]:
    """Return 200 if the session exists and is authenticated, 404 otherwise."""
    session = sessions.get(session_id)
    if session is None or not (session.client or session.demo_authenticated):
        raise HTTPException(status_code=404, detail="Session not found or not authenticated")
    return {"session_id": session_id, "username": session.username}


@app.get("/tools", dependencies=[Depends(require_api_token)])
async def list_tools(request: Request) -> Response:
    """The exposed tool list, ETagged with the spec_version. If-None-Match that
    matches the current version -> 304. The body carries spec_version too."""
    snap = registry.snapshot()
    etag = f'"{snap.spec_version}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(
        content={"tools": snap.exposed_tools, "spec_version": snap.spec_version},
        headers={"ETag": etag},
    )


@app.get("/mcp/tools", dependencies=[Depends(require_api_token)])
async def list_mcp_tools(request: Request) -> Response:
    return await list_tools(request)


@app.get("/tools/events", dependencies=[Depends(require_api_token)])
async def tools_events() -> StreamingResponse:
    """SSE stream of tools_changed events (one per real spec change, with the
    new spec_version) plus a periodic keepalive. Same auth as GET /tools."""
    return StreamingResponse(
        sse_events_stream(registry, sse_subs),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/sessions", dependencies=[Depends(require_api_token)])
async def create_session(request: SessionCreateRequest) -> SessionCreateResponse:
    session = sessions.create()
    result = await registry.call(
        registry.session_tool_name,
        {"username": request.username, "password": request.password},
        session,
    )
    return SessionCreateResponse(session_id=session.session_id, result=result)


@app.post("/tools/{tool_name}", dependencies=[Depends(require_api_token)])
async def call_tool(tool_name: str, request: ToolCallRequest) -> dict[str, Any]:
    session = sessions.get(request.session_id)
    if tool_name == registry.session_tool_name and session is None:
        session = sessions.create()
    result = await registry.call(tool_name, request.arguments, session)
    if tool_name == registry.session_tool_name and session is not None:
        result = {**result, "session_id": session.session_id}
    return result


@app.post("/mcp/call/{tool_name}", dependencies=[Depends(require_api_token)])
async def call_mcp_tool(tool_name: str, request: ToolCallRequest) -> dict[str, Any]:
    return await call_tool(tool_name, request)


# MCP SSE transport endpoints — used by Claude Desktop
@app.get("/sse")
async def sse_endpoint(request: Request) -> None:
    async with _sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await _mcp.run(
            streams[0],
            streams[1],
            _mcp.create_initialization_options(NotificationOptions(tools_changed=True)),
        )


app.mount("/messages/", app=_sse.handle_post_message)
