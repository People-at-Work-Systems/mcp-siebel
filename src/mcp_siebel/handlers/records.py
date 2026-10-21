"""The ``aggregate_data`` handler (kind ``python``).

The get / create / update / delete record handlers moved to the generic
``bc_read`` / ``bc_upsert`` / ``bc_delete`` executors (see executors/). The
field-curation ranking (``_rank_fallback_fields``) and the scoped-query
fallback (``_record_via_query`` / ``_read_fields``) that the ``bc_read``
``curated_query`` fallback reuses moved to ``mcp_siebel.executors._fallback``
so the framework layer does not import from this package. What stays here is
the ``aggregate_data`` handler — still kind ``python`` because it builds a
paginated query loop with no generic executor yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..common import (
    _error,
    _error_result,
    _require_auth,
    _text,
)
from ..siebel_client import SiebelError

if TYPE_CHECKING:
    from ..tools import ToolContext


async def _aggregate_data(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved = await auth.cache.resolve_business_object(str(args["business_object"]))
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    bc = str(args.get("business_component") or resolved)
    field = str(args["field"])
    operation = str(args["operation"])
    if operation not in {"sum", "avg", "min", "max", "count"}:
        return _error("operation must be one of: sum, avg, min, max, count.")
    offset = 0
    record_count = 0
    values: list[float] = []
    truncated = False
    for page in range(ctx.settings.aggregate_max_pages):
        try:
            result = await auth.client.query(
                resolved,
                bc,
                search_spec=args.get("searchspec"),
                fields=[field] if operation != "count" else None,
                page_size=ctx.settings.aggregate_page_size,
                offset=offset,
            )
        except SiebelError as exc:
            if exc.status_code == 404:
                break
            return _error_result(exc)
        record_count += len(result.items)
        if operation != "count":
            for item in result.items:
                try:
                    values.append(float(item[field]))
                except (KeyError, TypeError, ValueError):
                    pass
        if not result.has_more or result.next_start_row is None:
            break
        offset = result.next_start_row
    else:
        truncated = True
    if operation == "count":
        value: float = float(record_count)
    elif not values:
        return _text(f'No numeric values found in field "{field}" across {record_count} records.')
    elif operation == "sum":
        value = sum(values)
    elif operation == "avg":
        value = sum(values) / len(values)
    elif operation == "min":
        value = min(values)
    else:
        value = max(values)
    suffix = f"\nResult truncated after {ctx.settings.aggregate_max_pages} pages." if truncated else ""
    rendered = f"{int(value):,}" if value.is_integer() else f"{value:,.2f}"
    return _text(f"{resolved}/{bc}.{field} {operation}: **{rendered}**\nRecords processed: {record_count}{suffix}")
