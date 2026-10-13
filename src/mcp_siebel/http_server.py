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

The first tool to call is ``authenticate`` with username/password to obtain a
``session_id`` required by all subsequent Siebel tools.

REST surface: /tools, /sessions, /tools/{name}, /mcp/tools, /mcp/call/{name},
/health, /metrics.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from mcp.server import Server, ServerRequestContext
from mcp.server.sse import SseServerTransport
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from starlette.requests import Request

from .config import load_settings
from .context import CorrelationMiddleware, session_id_var, turn_id_var
from .tools import SessionStore, ToolRegistry


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
started_at = time.monotonic()


# ---------------------------------------------------------------------------
# MCP SSE transport — wired to the shared registry/sessions above
# ---------------------------------------------------------------------------

_sse = SseServerTransport("/messages/")


async def _list_tools(
    ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
) -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"])
            for t in registry.list_tools()
        ]
    )


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
    if name == "authenticate" and session is not None:
        result = {**result, "session_id": session.session_id}
    content = result.get("content") or [{"type": "text", "text": str(result)}]
    texts: list[Any] = [TextContent(type="text", text=c["text"]) for c in content if c.get("type") == "text"]
    return CallToolResult(content=texts, is_error=bool(result.get("isError")))


_mcp = Server("siebel-crm", on_list_tools=_list_tools, on_call_tool=_call_tool)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MCP Siebel",
    description="MCP-style tool server for Oracle Siebel 26.2+",
    version="0.1.0",
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
    }


@app.get("/sessions/{session_id}", dependencies=[Depends(require_api_token)])
async def get_session(session_id: str) -> dict[str, Any]:
    """Return 200 if the session exists and is authenticated, 404 otherwise."""
    session = sessions.get(session_id)
    if session is None or not (session.client or session.demo_authenticated):
        raise HTTPException(status_code=404, detail="Session not found or not authenticated")
    return {"session_id": session_id, "username": session.username}


@app.get("/tools", dependencies=[Depends(require_api_token)])
async def list_tools() -> dict[str, Any]:
    return {"tools": registry.list_tools()}


@app.get("/mcp/tools", dependencies=[Depends(require_api_token)])
async def list_mcp_tools() -> dict[str, Any]:
    return await list_tools()


@app.post("/sessions", dependencies=[Depends(require_api_token)])
async def create_session(request: SessionCreateRequest) -> SessionCreateResponse:
    session = sessions.create()
    result = await registry.call(
        "authenticate",
        {"username": request.username, "password": request.password},
        session,
    )
    return SessionCreateResponse(session_id=session.session_id, result=result)


@app.post("/tools/{tool_name}", dependencies=[Depends(require_api_token)])
async def call_tool(tool_name: str, request: ToolCallRequest) -> dict[str, Any]:
    session = sessions.get(request.session_id)
    if tool_name == "authenticate" and session is None:
        session = sessions.create()
    result = await registry.call(tool_name, request.arguments, session)
    if tool_name == "authenticate" and session is not None:
        result = {**result, "session_id": session.session_id}
    return result


@app.post("/mcp/call/{tool_name}", dependencies=[Depends(require_api_token)])
async def call_mcp_tool(tool_name: str, request: ToolCallRequest) -> dict[str, Any]:
    return await call_tool(tool_name, request)


# MCP SSE transport endpoints — used by Claude Desktop
@app.get("/sse")
async def sse_endpoint(request: Request) -> None:
    async with _sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await _mcp.run(streams[0], streams[1], _mcp.create_initialization_options())


app.mount("/messages/", app=_sse.handle_post_message)
