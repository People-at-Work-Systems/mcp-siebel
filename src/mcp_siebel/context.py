"""Correlation-ID context for log/trace correlation.

``session_id`` and ``turn_id`` tie every log line and downstream HTTP call for
one turn together, so the whole chain can be found in Loki/Grafana with a
single ``{session_id="..."}`` query instead of grepping each service's logs
separately.

``CorrelationMiddleware`` binds inbound ``X-Session-Id``/``X-Turn-Id`` request
headers to these contextvars; ``correlation_headers()`` reads them back out to
attach to any outbound call. A handler that resolves/creates its own
session_id or turn_id should call ``session_id_var.set(...)`` /
``turn_id_var.set(...)`` directly with the authoritative value once known.
"""

from __future__ import annotations

from contextvars import ContextVar

session_id_var: ContextVar[str] = ContextVar("session_id", default="")
turn_id_var: ContextVar[str] = ContextVar("turn_id", default="")


def correlation_headers() -> dict[str, str]:
    """Headers to attach to any outgoing request so the callee's logs (and
    any further hop it makes) stay tagged with the same session_id/turn_id."""
    headers: dict[str, str] = {}
    if session_id_var.get():
        headers["X-Session-Id"] = session_id_var.get()
    if turn_id_var.get():
        headers["X-Turn-Id"] = turn_id_var.get()
    return headers


class CorrelationMiddleware:
    """Pure ASGI middleware — binds X-Session-Id/X-Turn-Id request headers to
    the contextvars above for the lifetime of the request.

    Implemented as raw ASGI rather than Starlette's BaseHTTPMiddleware because
    it must pass scope/receive/send straight through unmodified: some services
    in this system (mcp-siebel) mount a raw ASGI sub-app for MCP's SSE
    transport, and BaseHTTPMiddleware's response-buffering wrapper breaks that
    transport. This middleware never touches send/receive, so it's safe
    everywhere, including in front of mounted sub-apps and SSE/streaming
    responses.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        session_id = headers.get(b"x-session-id", b"").decode()
        turn_id = headers.get(b"x-turn-id", b"").decode()

        session_token = session_id_var.set(session_id)
        turn_token = turn_id_var.set(turn_id)
        try:
            await self.app(scope, receive, send)
        finally:
            session_id_var.reset(session_token)
            turn_id_var.reset(turn_token)
