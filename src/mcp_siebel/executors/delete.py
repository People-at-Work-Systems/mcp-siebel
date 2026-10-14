"""The ``bc_delete`` executor: delete a Business Component record by Id.

This is the generic executor behind the spec kind ``bc_delete``. It replaces the
per-tool ``_delete_record`` handler: the BO-not-found check and the delete
dispatch live here once, parameterised by the spec entry's options (``render``
and the optional ``bind``). Argument names are derived from the previous handler
and from ``siebel_client.py`` — the external tool interface is unchanged.

No tool names live in this module. The executor is bound to a spec entry at load
time (``_assemble_bc_delete`` closes over the entry's options) and the resulting
callable is the single handler ``ToolRegistry.call`` invokes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..handlers._common import _require_auth, _resolve_bo_bc, _text
from ..kinds import _COMMON_FIELDS, _KindSpec, _SpecError
from ..renderers import RENDERERS
from ._validation import validate_bind_and_required

if TYPE_CHECKING:
    from ..tools import ToolContext

_DELETE_ALLOWED_OPTIONS = frozenset({"render", "bind"})
_DELETE_REQUIRED_OPTIONS = frozenset({"render"})
# Parameters execute_bc_delete actually reads from the effective args.
_DELETE_BIND_KEYS = frozenset({"business_object", "business_component", "id"})


# ---------------------------------------------------------------------------
# Load-time validation of kind-specific options
# ---------------------------------------------------------------------------


def _validate_bc_delete(entry: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    options = set(entry.keys()) - _COMMON_FIELDS
    extra = options - _DELETE_ALLOWED_OPTIONS
    if extra:
        raise _SpecError(f"unknown option(s) {sorted(extra)} for bc_delete entry {name!r}")
    missing = _DELETE_REQUIRED_OPTIONS - entry.keys()
    if missing:
        raise _SpecError(f"bc_delete entry {name!r} missing required option(s) {sorted(missing)}")
    render = entry.get("render")
    if not isinstance(render, str) or render not in RENDERERS:
        raise _SpecError(f"bc_delete entry {name!r} has unknown render {render!r} (known: {sorted(RENDERERS)})")
    validate_bind_and_required(
        kind="bc_delete",
        entry=entry,
        name=name,
        schema=schema,
        known_bind_keys=_DELETE_BIND_KEYS,
        required_params=("business_object", "id"),
    )


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


async def execute_bc_delete(args: dict[str, Any], ctx: ToolContext, options: dict[str, Any]) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    effective = {**(options.get("bind") or {}), **args}
    resolved, bc = await _resolve_bo_bc(effective, auth)
    if not resolved:
        return _text(f'Business Object "{effective["business_object"]}" not found.')
    await auth.client.delete_record(resolved, bc, str(effective["id"]))
    return RENDERERS[options["render"]](
        {"business_object": resolved, "business_component": bc, "id": effective["id"]}
    )


def _assemble_bc_delete(entry: dict[str, Any], name: str) -> Any:
    options: dict[str, Any] = {"render": entry["render"]}
    if "bind" in entry:
        options["bind"] = entry["bind"]

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await execute_bc_delete(args, ctx, options)

    return handler


DELETE_KIND: _KindSpec = _KindSpec(
    name="bc_delete",
    allowed_access=frozenset({"delete"}),
    allowed_options=_DELETE_ALLOWED_OPTIONS,
    required_options=_DELETE_REQUIRED_OPTIONS,
    validate=_validate_bc_delete,
    assemble=_assemble_bc_delete,
)
