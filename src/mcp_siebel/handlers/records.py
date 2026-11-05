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
    _validate_fields_arg,
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


def _normalise_fields_arg(raw: Any) -> list[str] | None:
    """Accept a list or comma-separated string; return a clean list or None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    else:
        parts = [p.strip() for p in raw]
    clean = [p for p in parts if p]
    return clean if clean else None


def _render_records_table(rows: list[dict[str, Any]], fields: list[str] | None) -> str:
    if not rows:
        return "(no records found)"
    columns = fields if fields else [k for k in rows[0].keys()]
    if not columns:
        return "(no records found)"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(str(row.get(col, "")) for col in columns) + " |"
        for row in rows
    ]
    return "\n".join([header, sep, *body])


async def _query_all_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Page through every record matching a searchspec, up to a per-call row
    ceiling (``query_max_records``). A caller-supplied ``max_records`` is clamped
    to the setting. Mirrors the ``_aggregate_data`` multi-page loop: advances
    ``offset = next_start_row`` until ``has_more`` is false or the ceiling is
    hit; a 404 is treated as an empty result. The result is marked truncated when
    the ceiling stops the loop before the end."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved = await auth.cache.resolve_business_object(str(args["business_object"]))
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    bc = str(args.get("business_component") or resolved)
    fields_err = _validate_fields_arg(args.get("fields"))
    if fields_err:
        return _error(fields_err)
    fields = _normalise_fields_arg(args.get("fields"))
    ceiling = min(
        int(args.get("max_records") or ctx.settings.query_max_records),
        ctx.settings.query_max_records,
    )
    rows: list[dict[str, Any]] = []
    offset = 0
    truncated = False
    total_count: int | None = None
    while True:
        try:
            result = await auth.client.query(
                resolved,
                bc,
                search_spec=args.get("searchspec"),
                fields=fields,
                sort_spec=args.get("sort_spec"),
                page_size=ctx.settings.aggregate_page_size,
                offset=offset,
            )
        except SiebelError as exc:
            if exc.status_code == 404:
                break
            return _error_result(exc)
        if total_count is None:
            total_count = result.total_count
        rows.extend(result.items)
        if len(rows) >= ceiling:
            rows = rows[:ceiling]
            truncated = True
            break
        if not result.has_more or result.next_start_row is None:
            break
        offset = result.next_start_row
    body = _render_records_table(rows, fields)
    footer = f"Records: {len(rows)}"
    if total_count is not None:
        footer += f" of {total_count}"
    if truncated:
        footer += f" (truncated at ceiling {ceiling})"
    return _text(f"{body}\n\n{footer}")
