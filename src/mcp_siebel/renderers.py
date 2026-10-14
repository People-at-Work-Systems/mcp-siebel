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


# The upsert / delete renderers take a *context* dict (not the query result
# dict): the resolved BO/BC, the record the client returned (None for delete),
# and the id / parent_id the message names. Each renderer reads only the keys
# its message needs; the executor builds the context for the path it took.


def _create_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The create-record prose format: a header naming the BO/BC then the record."""
    return _text(
        f"Created record in {ctx['business_object']}/{ctx['business_component']}:\n{_format_record(ctx['record'])}"
    )


def _create_json(ctx: dict[str, Any]) -> dict[str, Any]:
    """The create-record JSON format: the created record serialised for a
    programmatic caller (the new Id is in the result)."""
    return _json_text(ctx["record"])


def _update_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The update-record prose format: names the id and BO/BC, then the record."""
    return _text(
        f"Updated record {ctx['id']} in {ctx['business_object']}/{ctx['business_component']}:\n"
        f"{_format_record(ctx['record'])}"
    )


def _child_create_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The child-create prose format: names the BO/BC/parent then the record."""
    return _text(
        f"Created child record(s) under {ctx['business_object']}/{ctx['business_component']}/{ctx['parent_id']}:\n"
        f"{_format_record(ctx['record'])}"
    )


def _delete_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The delete format: a single line confirming which record was removed."""
    return _text(f"Deleted record {ctx['id']} from {ctx['business_object']}/{ctx['business_component']}.")


RENDERERS: dict[str, Renderer] = {
    "query_prose": _query_prose,
    "child_query_prose": _child_query_prose,
    "query_json": _query_json,
    "count_prose": _count_prose,
    "create_prose": _create_prose,
    "create_json": _create_json,
    "update_prose": _update_prose,
    "child_create_prose": _child_create_prose,
    "delete_prose": _delete_prose,
}
