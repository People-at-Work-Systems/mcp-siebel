"""Session-establishment handler: ``authenticate``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..common import _error, _text
from ..config import AppSettings
from ..schema_cache import SchemaCache
from ..siebel_client import SiebelClient, SiebelError

if TYPE_CHECKING:
    from ..tools import ToolContext

log = logging.getLogger("mcp-siebel")


async def _authenticate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session
    if session is None:
        return _error("No session was provided.")
    # Belt-and-braces against session hijack via rebind: the transports always
    # create a fresh session for the session tool, so this session should be
    # brand-new (no client yet). If an already-authenticated session reaches
    # here, refuse to rebind it to a different identity — the caller must start
    # a new session instead.
    if session.client is not None:
        return _error("Session is already authenticated; start a new session instead.")
    username = str(args.get("username") or "")
    password = args.get("password")
    if not username:
        return _error("Authentication failed: username is required.")
    if ctx.settings.demo_enabled:
        session.username = username
        session.demo_authenticated = True
        return _text(f"Demo mode active for {username}. All data shown is sample data.")
    if ctx.settings.mcp_mode == "standalone" and not password:
        return _error("Authentication failed: password is required in standalone mode.")

    # The persistent client is always the SSO (header-based) client — every
    # subsequent data call authenticates via the SSO header, not the password —
    # so the SSO path is what actually determines whether data calls will work.
    client = _sso_client(ctx.settings, username)

    if ctx.settings.mcp_mode == "standalone":
        # Probe the SSO identity with a real data query and treat ANY failure as
        # auth failure. The catalogue (/data/describe) is guest-accessible on
        # some Siebel instances and can't validate the identity — a bogus header
        # still gets a 200 there — so we query the configured probe BO/BC: a 200
        # means the SSO header is trusted, any error means it isn't. (The old
        # Basic probe would "proceed anyway" on a 500 and report a false
        # "Authenticated".)
        try:
            await client.query(
                ctx.settings.probe_business_object,
                ctx.settings.probe_business_component,
                page_size=1,
            )
        except SiebelError as exc:
            log.warning(
                "SSO probe failed for '%s' (BO/BC %s/%s): HTTP %s — %s",
                username,
                ctx.settings.probe_business_object,
                ctx.settings.probe_business_component,
                exc.status_code,
                exc.response_body[:200] if exc.response_body else "",
            )
            return _error(f"Authentication failed: could not establish a Siebel session for '{username}'.")
        except Exception as exc:  # noqa: BLE001 — transport errors aren't SiebelError
            log.warning("SSO probe error for '%s': %s", username, exc)
            return _error(f"Authentication failed: could not reach Siebel for '{username}'.")

    session.username = username
    session.client = client
    session.cache = SchemaCache(client)
    return _text(f"Authenticated as {username}. You can now use all Siebel tools.")


def _sso_client(settings: AppSettings, username: str) -> SiebelClient:
    return SiebelClient(
        base_url=settings.base_url,
        auth_mode="sso",
        sso_header_name=settings.sso_header,
        sso_username=username,
        page_size=settings.default_page_size,
        request_timeout_ms=settings.request_timeout_ms,
        max_concurrent_requests=settings.max_concurrent_requests,
        verify_tls=settings.verify_tls,
        ca_bundle=settings.ca_bundle,
    )
