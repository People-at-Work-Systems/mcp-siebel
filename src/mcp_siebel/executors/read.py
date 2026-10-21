"""The ``bc_read`` executor: read one record (with or without children).

This is the generic executor behind the spec kind ``bc_read``. It replaces the
per-tool ``_get_record`` / ``_get_record_with_children`` handlers: the BO-not-found
check, the placeholder-field guard, the optional ``curated_query`` fallback (the
scoped-query retry that names fields so a misconfigured integration component
doesn't make a record unreadable), and the child-records read all live here once,
parameterised by the spec entry's options (``mode``, ``render``, optional
``fallback`` and ``bind``). Argument names are derived from the previous handlers
and from ``siebel_client.py`` — the external tool interface is unchanged.

The ``curated_query`` fallback reuses ``executors._fallback._record_via_query`` so the
field-curation ranking (``_rank_fallback_fields``) and the curated
``preferred_fields`` intersection stay in one place; the mapping is read from
``ctx.preferred_fields`` (hot-reloaded from the spec). The prose formatting lives
in named renderers (family ``record``); this executor resolves the data and hands
the renderer a context dict.

No tool names live in this module. The executor is bound to a spec entry at load
time (``_assemble_bc_read`` closes over the entry's options) and the resulting
callable is the single handler ``ToolRegistry.call`` invokes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..common import _error, _require_auth, _resolve_bo_bc, _text, _validate_fields_arg
from ..kinds import _KindSpec, _SpecError
from ..renderers import RENDERERS, validate_render
from ..siebel_client import SiebelError
from ._fallback import _record_via_query
from ._validation import validate_bind_and_required

if TYPE_CHECKING:
    from ..tools import ToolContext

log = logging.getLogger("mcp-siebel")

_MODES = {"single", "with_children"}
_FALLBACKS = {"curated_query"}
_BC_READ_ALLOWED_OPTIONS = frozenset({"mode", "render", "fallback", "bind"})
_BC_READ_REQUIRED_OPTIONS = frozenset({"mode", "render"})
# Parameters execute_bc_read actually reads from the effective args (the bind
# values merged with the caller's input). A bind key outside the mode's set is a
# typo (or a key that would silently do nothing for that mode), rejected at load.
# ``fields`` is read only in mode "single"; binding it for "with_children" would
# be silently dropped, so it is not a known key for that mode.
_BC_READ_BIND_KEYS = frozenset({"business_object", "business_component", "id", "fields"})
_BC_READ_BIND_KEYS_WITHOUT_FIELDS = frozenset({"business_object", "business_component", "id"})
# Per-kind bind value types: BO/BC/id are str; fields is a list of field names.
_BC_READ_BIND_TYPES: dict[str, str] = {
    "business_object": "str",
    "business_component": "str",
    "id": "str",
    "fields": "list[str]",
}


# ---------------------------------------------------------------------------
# Load-time validation of kind-specific options
# ---------------------------------------------------------------------------


def _validate_bc_read(entry: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    # Allowed/required options are enforced centrally in ToolRegistry._load_valid_tools
    # from this kind's _KindSpec; this callback holds only bc_read-specific logic.
    mode = entry.get("mode")
    if mode not in _MODES:
        raise _SpecError(
            f"bc_read entry {name!r} has invalid mode {mode!r} (expected 'single' or 'with_children')"
        )
    # Per-mode render family: mode "single" takes the flat record renderers,
    # "with_children" the hierarchical ones (which strip the client's __-prefixed
    # child-availability keys). Pairing the wrong family would emit __-keys as
    # prose, so the wrong family is rejected at load time.
    accepted_render_families = {"record"} if mode == "single" else {"hierarchical_record"}
    validate_render(kind="bc_read", name=name, render=entry.get("render"), accepted_families=accepted_render_families)
    fallback = entry.get("fallback")
    if fallback is not None:
        if fallback not in _FALLBACKS:
            raise _SpecError(
                f"bc_read entry {name!r} has invalid fallback {fallback!r} (expected 'curated_query')"
            )
        # The scoped-query fallback reads one record by Id; it has no meaning for
        # the with-children path (which fetches child records, not a field list).
        if mode != "single":
            raise _SpecError(
                f"bc_read entry {name!r}: fallback {fallback!r} is only valid with mode 'single'"
            )
    # Every mode reads business_object and id; both must be bound or required so
    # the executor is never called without a value for a parameter it dereferences.
    # ``fields`` is only a known bind key for mode "single".
    bind_keys = _BC_READ_BIND_KEYS if mode == "single" else _BC_READ_BIND_KEYS_WITHOUT_FIELDS
    validate_bind_and_required(
        kind="bc_read",
        entry=entry,
        name=name,
        schema=schema,
        known_bind_keys=bind_keys,
        required_params=("business_object", "id"),
        bind_types=_BC_READ_BIND_TYPES,
    )


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


async def execute_bc_read(args: dict[str, Any], ctx: ToolContext, options: dict[str, Any]) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    # bind = fixed values from the spec; args = the caller's input. They are
    # disjoint (a bound key may not also be an input_schema property), so a plain
    # merge gives the effective argument set.
    # Spec bind wins over caller args — a pinned bind key cannot be overridden.
    effective = {**args, **(options.get("bind") or {})}
    resolved, bc = await _resolve_bo_bc(effective, auth)
    if not resolved:
        return _text(f'Business Object "{effective["business_object"]}" not found.')
    record_id = str(effective["id"])
    render = options["render"]

    if options["mode"] == "with_children":
        record = await auth.client.get_record_with_children(resolved, bc, record_id)
        if not record:
            return _text("No record found.")
        return RENDERERS[render](
            {"business_object": resolved, "business_component": bc, "id": record_id, "record": record}
        )

    # mode "single"
    bad_fields = _validate_fields_arg(effective.get("fields"))
    if bad_fields:
        return _error(bad_fields)
    fields = effective.get("fields")
    try:
        record = await auth.client.get_record(resolved, bc, record_id, fields)
    except SiebelError as exc:
        # A 404 already comes back as None, so an exception here means the read
        # itself failed. The curated_query fallback retries naming the fields
        # before giving up: a misconfigured integration component shouldn't make
        # a record unreadable when a scoped query can read it. If that fails too,
        # the error propagates (and the registry masks it) — it names the
        # offending field, which is what an administrator needs.
        if options.get("fallback") != "curated_query":
            raise
        log.warning("get_record direct read failed (%s); retrying as a scoped query", exc)
        record = await _record_via_query(auth, resolved, bc, record_id, fields, ctx.preferred_fields)
        if record is None:
            raise
    if not record:
        return _text("No record found.")
    return RENDERERS[render](
        {"business_object": resolved, "business_component": bc, "id": record_id, "record": record}
    )


def _assemble_bc_read(entry: dict[str, Any], name: str) -> Any:
    options: dict[str, Any] = {"mode": entry["mode"], "render": entry["render"]}
    if "fallback" in entry:
        options["fallback"] = entry["fallback"]
    if "bind" in entry:
        options["bind"] = entry["bind"]

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await execute_bc_read(args, ctx, options)

    return handler


BC_READ_KIND: _KindSpec = _KindSpec(
    name="bc_read",
    allowed_options=_BC_READ_ALLOWED_OPTIONS,
    required_options=_BC_READ_REQUIRED_OPTIONS,
    validate=_validate_bc_read,
    assemble=_assemble_bc_read,
)
