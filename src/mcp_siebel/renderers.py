"""Named result renderers for the generic executors.

A renderer turns the structured query result dict (the shape produced by
``_common._result_dict`` / ``_empty_result``) into the final tool-result dict
that ``ToolRegistry.call`` returns. Renderers are code, registered at import
time; only the JSON spec is hot-reloaded. An unknown render name is rejected at
spec load time (see ``executors.query``).

The renderers here move the prose/JSON/count formatting previously inlined in
the per-tool handlers; their output is byte-identical to those handlers. No tool
names live in this module — a renderer is named for what it does, not for the
tool that uses it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .handlers._common import _format_record, _json_text, _text

Renderer = Callable[[dict[str, Any]], dict[str, Any]]


def _query_prose(result: dict[str, Any]) -> dict[str, Any]:
    """The paginated prose format: a header (count | page size | has-more) then
    one numbered block per record, or '(no records found)'."""
    rows = [
        f"Record {result['offset'] + idx + 1}:\n{_format_record(item)}"
        for idx, item in enumerate(result["items"])
    ]
    total = result.get("totalCount")
    count_info = f"Total: {total}" if total is not None else f"Returned: {len(result['items'])}"
    more = (
        f"Has more: YES - call again with offset={result.get('nextStartRow')}"
        if result.get("hasMore")
        else "Has more: NO (end of results)"
    )
    return _text(
        f"{count_info} | Page size: {result['pageSize']} | {more}\n\n"
        + ("\n\n".join(rows) or "(no records found)")
    )


def _child_query_prose(result: dict[str, Any]) -> dict[str, Any]:
    """The child-records prose format: one numbered block per record, no header,
    or '(no child records found)'."""
    rows = [
        f"Record {result['offset'] + idx + 1}:\n{_format_record(item)}"
        for idx, item in enumerate(result["items"])
    ]
    return _text("\n\n".join(rows) or "(no child records found)")


def _query_json(result: dict[str, Any]) -> dict[str, Any]:
    """The structured-JSON format: the result dict serialised for programmatic
    callers."""
    return _json_text(result)


def _count_prose(result: dict[str, Any]) -> dict[str, Any]:
    """The count format: a single line with the total, or 'not available'."""
    total = result.get("totalCount")
    return _text(f"Total records: **{total}**" if total is not None else "Count not available.")


RENDERERS: dict[str, Renderer] = {
    "query_prose": _query_prose,
    "child_query_prose": _child_query_prose,
    "query_json": _query_json,
    "count_prose": _count_prose,
}
