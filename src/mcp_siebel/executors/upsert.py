"""The ``bc_upsert`` executor: create or update a Business Component record.

This is the generic executor behind the spec kind ``bc_upsert``. It replaces the
per-tool ``_create_record`` / ``_create_record_json`` / ``_update_record`` /
``_create_child_record`` handlers: the BO-not-found check, the empty-data guard,
the optional ``allowed_fields`` allowlist, and the create/update/child-create
dispatch all live here once, parameterised by the spec entry's options
(``operation``, ``scope``, ``render``, and the optional ``bind`` /
``allowed_fields``). Argument names are derived from the previous handlers and
from ``siebel_client.py`` — the external tool interface is unchanged.

No tool names live in this module. The executor is bound to a spec entry at load
time (``_assemble_bc_upsert`` closes over the entry's options) and the resulting
callable is the single handler ``ToolRegistry.call`` invokes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..common import _error, _require_auth, _resolve_bo_bc, _text
from ..kinds import _COMMON_FIELDS, _KindSpec, _SpecError
from ..renderers import RENDERERS, validate_render
from ._validation import validate_bind_and_required

if TYPE_CHECKING:
    from ..tools import ToolContext

_OPERATIONS = {"create", "update"}
_SCOPES = {"root", "child"}
_UPSERT_ALLOWED_OPTIONS = frozenset({"operation", "scope", "render", "bind", "allowed_fields"})
_UPSERT_REQUIRED_OPTIONS = frozenset({"operation", "scope", "render"})
# Parameters execute_bc_upsert actually reads from the effective args.
_UPSERT_BIND_KEYS = frozenset(
    {"business_object", "business_component", "data", "id", "parent_id", "child_business_component"}
)
# Per-kind bind value types: BO/BC/id/parent/child_bc are str; data is an object.
_UPSERT_BIND_TYPES: dict[str, str] = {
    "business_object": "str",
    "business_component": "str",
    "data": "object",
    "id": "str",
    "parent_id": "str",
    "child_business_component": "str",
}


# ---------------------------------------------------------------------------
# Load-time validation of kind-specific options
# ---------------------------------------------------------------------------


def _validate_bc_upsert(entry: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    options = set(entry.keys()) - _COMMON_FIELDS
    extra = options - _UPSERT_ALLOWED_OPTIONS
    if extra:
        raise _SpecError(f"unknown option(s) {sorted(extra)} for bc_upsert entry {name!r}")
    missing = _UPSERT_REQUIRED_OPTIONS - entry.keys()
    if missing:
        raise _SpecError(f"bc_upsert entry {name!r} missing required option(s) {sorted(missing)}")
    operation = entry.get("operation")
    if operation not in _OPERATIONS:
        raise _SpecError(
            f"bc_upsert entry {name!r} has invalid operation {operation!r} (expected 'create' or 'update')"
        )
    scope = entry.get("scope")
    if scope not in _SCOPES:
        raise _SpecError(f"bc_upsert entry {name!r} has invalid scope {scope!r} (expected 'root' or 'child')")
    render = entry.get("render")
    validate_render(kind="bc_upsert", name=name, render=render, accepted_families={"upsert"})
    allowed = entry.get("allowed_fields")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(isinstance(f, str) for f in allowed)
    ):
        raise _SpecError(f"bc_upsert entry {name!r} 'allowed_fields' must be a list of strings")

    # Required-parameter coverage: update needs id; child create needs parent_id
    # and child_business_component; every path needs business_object. (data is
    # guarded at runtime, not here — the empty-data check is a value check.)
    if operation == "update":
        required_params: tuple[str, ...] = ("business_object", "id")
    elif scope == "child":
        required_params = ("business_object", "parent_id", "child_business_component")
    else:
        required_params = ("business_object",)
    validate_bind_and_required(
        kind="bc_upsert",
        entry=entry,
        name=name,
        schema=schema,
        known_bind_keys=_UPSERT_BIND_KEYS,
        required_params=required_params,
        bind_types=_UPSERT_BIND_TYPES,
    )
    # Policy: binding business_component locks the tool to a fixed BC, so the
    # caller's data must be constrained by a non-empty allowed_fields list —
    # otherwise a bound-BC tool accepts arbitrary field names, defeating the
    # point of pinning the BC.
    bind = entry.get("bind")
    if isinstance(bind, dict) and "business_component" in bind:
        if not isinstance(allowed, list) or not allowed:
            raise _SpecError(
                f"bc_upsert entry {name!r}: binding business_component requires a non-empty allowed_fields list"
            )


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


async def execute_bc_upsert(args: dict[str, Any], ctx: ToolContext, options: dict[str, Any]) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    # Spec bind wins over caller args — a pinned bind key cannot be overridden.
    effective = {**args, **(options.get("bind") or {})}
    resolved, bc = await _resolve_bo_bc(effective, auth)
    if not resolved:
        return _text(f'Business Object "{effective["business_object"]}" not found.')
    data = effective.get("data")
    if not isinstance(data, dict) or not data:
        return _error('The "data" parameter is required and must be a non-empty object.')
    allowed = options.get("allowed_fields")
    if allowed is not None:
        bad = [k for k in data if k not in allowed]
        if bad:
            return _error(
                f"Field(s) {bad} are not allowed for this tool. Allowed fields: {allowed}."
            )

    operation = options["operation"]
    scope = options["scope"]
    render = options["render"]
    if operation == "create":
        if scope == "child":
            record = await auth.client.create_child_record(
                resolved,
                bc,
                str(effective["parent_id"]),
                str(effective["child_business_component"]),
                data,
            )
            render_ctx: dict[str, Any] = {
                "business_object": resolved,
                "business_component": bc,
                "parent_id": effective["parent_id"],
                "record": record,
            }
        else:
            record = await auth.client.create_record(resolved, bc, data)
            render_ctx = {"business_object": resolved, "business_component": bc, "record": record}
    else:  # update
        record = await auth.client.update_record(resolved, bc, str(effective["id"]), data)
        render_ctx = {
            "business_object": resolved,
            "business_component": bc,
            "id": effective["id"],
            "record": record,
        }
    return RENDERERS[render](render_ctx)


def _assemble_bc_upsert(entry: dict[str, Any], name: str) -> Any:
    options: dict[str, Any] = {
        "operation": entry["operation"],
        "scope": entry["scope"],
        "render": entry["render"],
    }
    if "bind" in entry:
        options["bind"] = entry["bind"]
    if "allowed_fields" in entry:
        options["allowed_fields"] = entry["allowed_fields"]

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await execute_bc_upsert(args, ctx, options)

    return handler


UPSERT_KIND: _KindSpec = _KindSpec(
    name="bc_upsert",
    allowed_options=_UPSERT_ALLOWED_OPTIONS,
    required_options=_UPSERT_REQUIRED_OPTIONS,
    validate=_validate_bc_upsert,
    assemble=_assemble_bc_upsert,
)
