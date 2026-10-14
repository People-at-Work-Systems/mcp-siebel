"""Session-establishment handler: ``authenticate``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..config import AppSettings
from ..schema_cache import SchemaCache
from ..siebel_client import SiebelClient, SiebelError
from ._common import _error, _text

if TYPE_CHECKING:
    from ..tools import ToolContext

log = logging.getLogger("mcp-siebel")


async def _authenticate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session
    if session is None:
        return _error("No session was provided.")
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


async def _authenticated_client(settings: AppSettings, username: str, password: str) -> SiebelClient | None:
    """Return a ready-to-use SiebelClient for standalone mode.

    Tries token auth (POST /auth) first — required by Siebel 26.x instances that
    don't accept raw Basic Auth on data endpoints. Falls back to Basic Auth for
    older or differently-configured instances. Returns None if credentials are
    invalid (401/403).
    """
    # Attempt 1: token auth
    token_client = SiebelClient(
        base_url=settings.base_url,
        auth_mode="token",
        username=username,
        password=password,
        page_size=settings.default_page_size,
        request_timeout_ms=settings.request_timeout_ms,
        max_concurrent_requests=settings.max_concurrent_requests,
    )
    try:
        await token_client.login()
        log.info("Siebel authentication succeeded via token auth")
        return token_client
    except SiebelError as exc:
        if exc.status_code in {401, 403}:
            return None  # Bad credentials — no point trying basic
        log.info(
            "Token auth endpoint returned HTTP %s — falling back to Basic Auth",
            exc.status_code,
        )

    # Attempt 2: basic auth — validate with a lightweight probe
    basic_client = _basic_client(settings, username, password)
    try:
        await basic_client.list_business_objects()
        basic_client.page_size = settings.default_page_size
        log.info("Siebel authentication succeeded via Basic Auth")
        return basic_client
    except SiebelError as exc:
        if exc.status_code in {401, 403}:
            return None
        # 500 / other — Siebel may restrict the catalogue endpoint but credentials
        # could still be valid; proceed and let the real tool call surface any error.
        log.warning(
            "Basic Auth probe returned HTTP %s — proceeding anyway: %s",
            exc.status_code,
            exc.response_body[:200] if exc.response_body else "",
        )
        basic_client.page_size = settings.default_page_size
        return basic_client


def _basic_client(settings: AppSettings, username: str, password: str) -> SiebelClient:
    return SiebelClient(
        base_url=settings.base_url,
        auth_mode="basic",
        username=username,
        password=password,
        page_size=1,
        request_timeout_ms=settings.request_timeout_ms,
        max_concurrent_requests=settings.max_concurrent_requests,
    )


def _sso_client(settings: AppSettings, username: str) -> SiebelClient:
    return SiebelClient(
        base_url=settings.base_url,
        auth_mode="sso",
        sso_header_name=settings.sso_header,
        sso_username=username,
        page_size=settings.default_page_size,
        request_timeout_ms=settings.request_timeout_ms,
        max_concurrent_requests=settings.max_concurrent_requests,
    )
