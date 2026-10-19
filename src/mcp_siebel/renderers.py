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

from .handlers._common import _format_hierarchical_record, _format_record, _json_text, _text
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


# The bc_read renderers take a context dict built by the bc_read executor: the
# resolved BO/BC, the record id, and the record the client returned. Two
# families so a spec can't pair the wrong renderer with the wrong mode:
# ``record`` (flat — mode "single") and ``hierarchical_record`` (strips the
# client's ``__``-prefixed child-availability keys — mode "with_children").
# Their output is byte-identical to the former get_record /
# get_record_with_children handlers.


def _record_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The single-record prose: a header naming the BO/BC and id, then the
    record (one ``  key: value`` line per non-empty field)."""
    return _text(
        f"{ctx['business_object']}/{ctx['business_component']} record {ctx['id']}:\n{_format_record(ctx['record'])}"
    )


def _record_hierarchical_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The with-children record prose: same header, then the record with the
    ``__``-prefixed child-availability keys stripped (see
    ``_format_hierarchical_record``)."""
    return _text(
        f"{ctx['business_object']}/{ctx['business_component']} record {ctx['id']}:\n"
        f"{_format_hierarchical_record(ctx['record'])}"
    )


# The describe renderers (families ``list`` / ``describe`` / ``repository``) take
# a context dict built by the bc_describe executor: the resolved BO/BC plus
# either the business-object list, a BC descriptor, or the repository field
# list. Their output is byte-identical to the former metadata handlers.


def _list_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The business-objects list: a header with the count then one bullet each."""
    objects = ctx["business_objects"]
    return _text(f"Available Business Objects ({len(objects)}):\n" + "\n".join(f"  - {item}" for item in objects))


def _list_json(ctx: dict[str, Any]) -> dict[str, Any]:
    """The business-objects list as a JSON array for a programmatic caller."""
    return _json_text({"business_objects": ctx["business_objects"]})


def _bc_field_prose(descriptor: Any) -> str:
    """One ``  - name: type (flags)`` line per field (the former
    SchemaCache.field_summary). Moved here so the renderer owns the format."""
    lines: list[str] = []
    for field in descriptor.fields:
        flags: list[str] = []
        if field.required:
            flags.append("required")
        if field.read_only:
            flags.append("read-only")
        if field.max_length:
            flags.append(f"max {field.max_length}")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"  - {field.name}: {field.type}{suffix}")
    return "\n".join(lines)


def _describe_bc_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The BC descriptor prose: a BO/BC header, the field list, a total."""
    descriptor = ctx["descriptor"]
    return _text(
        f"{ctx['business_object']} / {ctx['business_component']}\n\n"
        f"{_bc_field_prose(descriptor)}\n\nTotal fields: {len(descriptor.fields)}"
    )


def _describe_bc_json(ctx: dict[str, Any]) -> dict[str, Any]:
    """The BC descriptor as JSON: BO/BC and per-field name/type/required/
    read_only/max_length (the REST data-API field list)."""
    descriptor = ctx["descriptor"]
    return _json_text(
        {
            "business_object": ctx["business_object"],
            "business_component": ctx["business_component"],
            "fields": [
                {
                    "name": f.name,
                    "type": f.type,
                    "required": f.required,
                    "read_only": f.read_only,
                    "max_length": f.max_length,
                }
                for f in descriptor.fields
            ],
        }
    )


def _repository_prose(ctx: dict[str, Any]) -> dict[str, Any]:
    """The repository field list prose: a header, a total, then one line per
    field (sorted by name) with type, length, flags, LOV/join/defaults — or the
    'no repository metadata' line when the field list is empty."""
    bo, bc, fields = ctx["business_object"], ctx["business_component"], ctx["fields"]
    if not fields:
        return _text(
            f"No repository metadata found for {bo}/{bc}. "
            "The workspace metadata API may not be accessible. "
            "Use describe_business_object for the REST field list instead."
        )
    lines = [f"Repository metadata: {bo} / {bc}", f"Total fields: {len(fields)}", ""]
    for f in sorted(fields, key=lambda x: str(x.get("Name", ""))):
        name = f.get("Name", "")
        ftype = f.get("Type", "")
        length = f.get("Length") or f.get("length") or ""
        flags = []
        if str(f.get("Required", "")).upper() in ("Y", "TRUE", "1"):
            flags.append("Required")
        if str(f.get("ReadOnly", "")).upper() in ("Y", "TRUE", "1"):
            flags.append("ReadOnly")
        if str(f.get("Calculated", "")).upper() in ("Y", "TRUE", "1"):
            flags.append("Calculated")
        pre = f.get("PreDefaultValue") or ""
        post = f.get("PostDefaultValue") or ""
        lov = f.get("LOVType") or f.get("LOV Type") or ""
        join = f.get("JoinName") or f.get("Join") or ""

        detail_parts = []
        if length:
            detail_parts.append(f"len={length}")
        if flags:
            detail_parts.append(", ".join(flags))
        if lov:
            detail_parts.append(f"LOV={lov}")
        if join:
            detail_parts.append(f"Join={join}")
        if pre:
            detail_parts.append(f"PreDefault={pre!r}")
        if post:
            detail_parts.append(f"PostDefault={post!r}")

        detail = f"  [{', '.join(detail_parts)}]" if detail_parts else ""
        lines.append(f"  {name} ({ftype}){detail}")
    return _text("\n".join(lines))


def _repository_json(ctx: dict[str, Any]) -> dict[str, Any]:
    """The repository field list as JSON: per-field name/type/LOV/flags/length
    (no empty-list special case — an empty field list serialises as ``[]``)."""
    fields = ctx["fields"]
    return _json_text(
        {
            "business_object": ctx["business_object"],
            "business_component": ctx["business_component"],
            "fields": [
                {
                    "name": f.get("Name", ""),
                    "type": f.get("Type", ""),
                    "lov_type": f.get("LOVType") or f.get("LOV Type") or "",
                    "required": str(f.get("Required", "")).upper() in ("Y", "TRUE", "1"),
                    "read_only": str(f.get("ReadOnly", "")).upper() in ("Y", "TRUE", "1"),
                    "calculated": str(f.get("Calculated", "")).upper() in ("Y", "TRUE", "1"),
                    "max_length": f.get("Length") or f.get("length"),
                }
                for f in fields
            ],
        }
    )


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
    ("record_prose", "record", _record_prose),
    ("record_hierarchical_prose", "hierarchical_record", _record_hierarchical_prose),
    ("list_prose", "list", _list_prose),
    ("list_json", "list", _list_json),
    ("describe_bc_prose", "describe", _describe_bc_prose),
    ("describe_bc_json", "describe", _describe_bc_json),
    ("repository_prose", "repository", _repository_prose),
    ("repository_json", "repository", _repository_json),
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
