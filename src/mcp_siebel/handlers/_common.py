"""Shared helpers for the tool handlers.

The domain handler modules (``auth``, ``metadata``, ``records``, ``children``,
``util``) build their results and gate on auth through these. Nothing here is
tool-specific — no tool names, no per-tool branching — so it stays out of
``tools.py``, which only owns the registry and the handler resolver.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ..siebel_client import SiebelError, SiebelRecord

if TYPE_CHECKING:
    # Framework types are referenced only in annotations; importing them at
    # runtime would create a cycle (tools.py imports this module for
    # _error_result). ``from __future__ import annotations`` keeps the names
    # unevaluated, so a TYPE_CHECKING-only import satisfies type checkers
    # without pulling tools.py at runtime.
    from ..tools import ToolContext

log = logging.getLogger("mcp-siebel")


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _json_text(data: Any) -> dict[str, Any]:
    return _text(json.dumps(data, default=str))


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _error_result(exc: Exception, tool_name: str = "") -> dict[str, Any]:
    if isinstance(exc, SiebelError):
        log.error(
            "Siebel error in tool %s: HTTP %s — %s",
            tool_name,
            exc.status_code,
            exc.response_body[:500] if exc.response_body else "(no body)",
        )
        if exc.status_code == 401:
            return _error("The Siebel session has expired. Refresh the page or log in again.")
    else:
        log.error("Unexpected error in tool %s: %s", tool_name, exc, exc_info=True)
    # isError (not _text) so the agent loop knows the tool call failed and can
    # surface a user-friendly message instead of treating the masked text as a
    # successful result.
    return _error("[INTERNAL - do not display verbatim] The Siebel operation could not be completed.")


def _require_auth(ctx: ToolContext) -> Any:
    if ctx.session and ctx.session.client and ctx.session.cache:
        return ctx.session
    return _error("Not authenticated. Call the authenticate tool first.")


async def _resolve_bo_bc(args: dict[str, Any], auth: Any) -> tuple[str | None, str]:
    resolved = await auth.cache.resolve_business_object(str(args["business_object"]))
    return resolved, str(args.get("business_component") or resolved or args["business_object"])


def _result_dict(result: Any) -> dict[str, Any]:
    return {
        "items": result.items,
        "totalCount": result.total_count,
        "hasMore": result.has_more,
        "nextStartRow": result.next_start_row,
        "offset": result.offset,
        "pageSize": result.page_size,
    }


def _empty_result(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": [],
        "totalCount": 0,
        "hasMore": False,
        "offset": int(args.get("offset") or 0),
        "pageSize": int(args.get("page_size") or 20),
    }


def _format_record(record: SiebelRecord) -> str:
    lines = []
    for key, value in record.items():
        if value in (None, ""):
            continue
        if key == "Link" and isinstance(value, list):
            children = [item.get("name") for item in value if isinstance(item, dict) and item.get("rel") == "child"]
            if children:
                lines.append(f"  Children: {', '.join(str(c) for c in children)}")
            continue
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _format_hierarchical_record(record: SiebelRecord) -> str:
    visible = {key: value for key, value in record.items() if not key.startswith("__")}
    return _format_record(visible)


# Tokens the LLM emits as placeholders when it doesn't know a real value.
# Rejecting them up front (instead of forwarding to Siebel) avoids the
# SBL-EAI-50258 "Invalid fields: '<?>'" round-trip and the broken-default
# retry that follows (see records._query). Compared case-insensitively.
_PLACEHOLDER_FIELD_TOKENS = {
    "<?>",
    "?",
    "??",
    "unknown",
    "n/a",
    "<field>",
    "<fields>",
    "<name>",
    "<value>",
    "<placeholder>",
    "...",
}


def _validate_fields_arg(raw: Any) -> str | None:
    """Return a guidance error if `fields` contains placeholder tokens the
    LLM emits when it doesn't know real field names; None when the arg is
    absent or looks like real field names."""
    if raw is None:
        return None
    parts = [p.strip() for p in (raw.split(",") if isinstance(raw, str) else raw)]
    bad = [p for p in parts if p and p.lower() in _PLACEHOLDER_FIELD_TOKENS]
    if not bad:
        return None
    return (
        f"'fields' must be real Siebel field names, but got placeholder(s) {bad!r}. "
        "Call describe_business_object first to get valid field names, then pass them "
        "comma-separated — or omit 'fields' to use the default set. Do not guess and "
        "do not pass placeholders like '<?>'."
    )
