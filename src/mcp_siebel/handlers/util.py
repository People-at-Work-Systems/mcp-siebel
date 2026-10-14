"""Utility handlers: date arithmetic, query-syntax reference, web search."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from ._common import _error, _text

if TYPE_CHECKING:
    from ..tools import ToolContext


async def _get_query_syntax(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _text(
        "Use <> for not equal; never use !=.\n"
        "Field names go in square brackets: [Status] = 'Open'.\n"
        "Use LIKE with * wildcards: [Name] LIKE 'Acme*'.\n"
        "Combine with AND/OR and parenthesize compound expressions.\n"
        "Dates may use Today(), Timestamp(), and arithmetic such as Today()-30.\n"
        "Siebel may return 404 when a query has no records."
    )


async def _calculate_date(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    expr = str(args.get("expression", "")).strip().lower()
    today = date.today()
    weekdays = {"sunday": 6, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5}
    result: date | None = None
    if expr == "today":
        result = today
    elif expr == "tomorrow":
        result = today + timedelta(days=1)
    elif expr == "yesterday":
        result = today - timedelta(days=1)
    elif expr.startswith("next "):
        target = weekdays.get(expr.removeprefix("next "))
        if target is not None:
            result = today + timedelta(days=((target - today.weekday()) % 7) or 7)
    elif expr.startswith("in ") and expr.endswith(" days"):
        result = today + timedelta(days=int(expr.split()[1]))
    elif expr == "end of month":
        result = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if result is None:
        return _error(f'Could not parse date expression: "{args.get("expression")}".')
    return _text(result.strftime("%m/%d/%Y (%A)"))


async def _web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.settings.brave_search_api_key:
        return _error("Web search is not configured. Set BRAVE_SEARCH_API_KEY.")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": args["query"], "count": int(args.get("count") or 5)},
            headers={"Accept": "application/json", "X-Subscription-Token": ctx.settings.brave_search_api_key},
        )
    if response.status_code >= 400:
        return _error(f"Brave Search API error: {response.status_code}")
    results = (response.json().get("web") or {}).get("results") or []
    lines = []
    for item in results:
        lines.extend([f"**{item.get('title')}**", f"URL: {item.get('url')}", str(item.get("description") or ""), ""])
    return _text("\n".join(lines) or f'No results found for: "{args["query"]}"')
