"""Named result renderers for the generic executors.

A renderer turns a structured result dict into the final tool-result dict that
``ToolRegistry.call`` returns. Renderers are code, registered at import time;
only the JSON spec is hot-reloaded. An unknown render name — or a render name
from the wrong *family* for its kind — is rejected at spec load time (see
``validate_render``).

Each renderer declares a **family** naming the shape of dict it consumes:
``query`` and ``count`` take the paginated query-result dict
(``_common._result_dict`` / ``_empty_result``); ``upsert`` and ``delete`` take
an upsert/delete context dict (resolved BO/BC, record, id/parent_id). A kind
accepts only the families that match its result shape, so a bc_query tool can't
accidentally use an upsert renderer (which would KeyError at runtime). The
``_RENDERER_REGISTRY`` tuples are the single source of truth for both the
``RENDERERS`` map and the ``RENDERER_FAMILIES`` map.

No tool names live in this module — a renderer is named for what it does, not
for the tool that uses it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .handlers._common import _format_record, _json_text, _text
from .kinds import _SpecError

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


def _count_json(result: dict[str, Any]) -> dict[str, Any]:
    """The count result as structured JSON (the paginated result dict serialised
    — totalCount carries the count; items is empty because the count path forces
    page_size=1)."""
    return _json_text(result)


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


# Single source of truth: (name, family, renderer). ``family`` names the shape
# of dict the renderer consumes; kinds accept only matching families (see
# ``validate_render``). Adding a renderer means adding a tuple here.
_RENDERER_REGISTRY: list[tuple[str, str, Renderer]] = [
    ("query_prose", "query", _query_prose),
    ("child_query_prose", "query", _child_query_prose),
    ("query_json", "query", _query_json),
    ("count_prose", "count", _count_prose),
    ("count_json", "count", _count_json),
    ("create_prose", "upsert", _create_prose),
    ("create_json", "upsert", _create_json),
    ("update_prose", "upsert", _update_prose),
    ("child_create_prose", "upsert", _child_create_prose),
    ("delete_prose", "delete", _delete_prose),
]

RENDERERS: dict[str, Renderer] = {name: fn for name, _, fn in _RENDERER_REGISTRY}
RENDERER_FAMILIES: dict[str, str] = {name: fam for name, fam, _ in _RENDERER_REGISTRY}


def validate_render(*, kind: str, name: str, render: Any, accepted_families: set[str]) -> str:
    """Validate the ``render`` option for a kind: it must name a known renderer
    whose family is in ``accepted_families``. Returns the render name on success;
    raises ``_SpecError`` naming the entry, the renderer and the expected family
    on failure. Each kind calls this in place of a bare ``render in RENDERERS``
    check so a wrong-family render is caught at load time, not as a runtime
    KeyError."""
    if not isinstance(render, str) or render not in RENDERERS:
        raise _SpecError(f"{kind} entry {name!r} has unknown render {render!r} (known: {sorted(RENDERERS)})")
    family = RENDERER_FAMILIES[render]
    if family not in accepted_families:
        raise _SpecError(
            f"{kind} entry {name!r}: render {render!r} is family {family!r}, "
            f"expected {sorted(accepted_families)}"
        )
    return render
