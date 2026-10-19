"""The ``bc_describe`` executor: catalogue / describe a Business Component.

This is the generic executor behind the spec kind ``bc_describe``. It replaces
the per-tool ``_list_business_objects`` / ``_list_business_objects_json`` /
``_describe_business_object`` / ``_describe_business_object_json`` /
``_describe_repository`` / ``_describe_repository_json`` handlers. The ``source``
option selects the path:

* ``business_objects`` — list the available Business Objects;
* ``bc_descriptor`` — describe a BO/BC (the REST data-API field list), with the
  SBL-EAI-50257 child-BC fallback that re-resolves the BC's own BO and retries;
* ``repository`` — the workspace-repository field list (richer than the
  descriptor).

Argument names are derived from the previous handlers and from
``siebel_client.py`` / ``schema_cache.py`` — the external tool interface is
unchanged. The prose/JSON formatting lives in named renderers (families ``list``,
``describe``, ``repository``); this executor resolves the data and hands the
renderer a context dict.

No tool names live in this module. The executor is bound to a spec entry at load
time (``_assemble_bc_describe`` closes over the entry's options) and the
resulting callable is the single handler ``ToolRegistry.call`` invokes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..common import _error, _error_result, _require_auth, _text
from ..kinds import _COMMON_FIELDS, _KindSpec, _SpecError
from ..renderers import RENDERERS, validate_render
from ..siebel_client import SiebelError
from ._validation import validate_bind_and_required

if TYPE_CHECKING:
    from ..tools import ToolContext

_SOURCES = {"business_objects", "bc_descriptor", "repository"}
# Per-source render family (the shape of dict the renderer consumes).
_SOURCE_FAMILIES: dict[str, set[str]] = {
    "business_objects": {"list"},
    "bc_descriptor": {"describe"},
    "repository": {"repository"},
}
_DESCRIBE_ALLOWED_OPTIONS = frozenset({"source", "render", "bind"})
_DESCRIBE_REQUIRED_OPTIONS = frozenset({"source", "render"})
_DESCRIBE_BIND_KEYS = frozenset({"business_object", "business_component"})
_DESCRIBE_BIND_TYPES: dict[str, str] = {"business_object": "str", "business_component": "str"}
_BO_NOT_FOUND = 'Business Object "{bo}" not found. Use list_business_objects to see available options.'


# ---------------------------------------------------------------------------
# Load-time validation of kind-specific options
# ---------------------------------------------------------------------------


def _validate_bc_describe(entry: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    options = set(entry.keys()) - _COMMON_FIELDS
    extra = options - _DESCRIBE_ALLOWED_OPTIONS
    if extra:
        raise _SpecError(f"unknown option(s) {sorted(extra)} for bc_describe entry {name!r}")
    missing = _DESCRIBE_REQUIRED_OPTIONS - entry.keys()
    if missing:
        raise _SpecError(f"bc_describe entry {name!r} missing required option(s) {sorted(missing)}")
    source = entry.get("source")
    if source not in _SOURCES:
        raise _SpecError(
            f"bc_describe entry {name!r} has invalid source {source!r} "
            f"(expected {sorted(_SOURCES)})"
        )
    validate_render(
        kind="bc_describe", name=name, render=entry.get("render"), accepted_families=_SOURCE_FAMILIES[source]
    )
    # business_objects takes no BO argument; bc_descriptor and repository need business_object.
    required_params: tuple[str, ...] = () if source == "business_objects" else ("business_object",)
    validate_bind_and_required(
        kind="bc_describe",
        entry=entry,
        name=name,
        schema=schema,
        known_bind_keys=_DESCRIBE_BIND_KEYS,
        required_params=required_params,
        bind_types=_DESCRIBE_BIND_TYPES,
    )


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


async def _resolve_business_object(effective: dict[str, Any], auth: Any) -> tuple[str | None, str, str]:
    """Resolve the BO and derive the BC. Returns (resolved, bc, raw_bo). Mirrors
    the former handlers: bc defaults to the resolved BO when omitted."""
    raw_bo = str(effective["business_object"])
    resolved = await auth.cache.resolve_business_object(raw_bo)
    bc = str(effective.get("business_component") or resolved or raw_bo)
    return resolved, bc, raw_bo


async def _descriptor_with_fallback(auth: Any, resolved: str, bc: str) -> tuple[str | None, Any]:
    """Fetch the BC descriptor, applying the SBL-EAI-50257 child-BC fallback:
    when the BC isn't mapped under ``resolved`` (most often a child component
    described under its parent BO), re-resolve the BC's own BO and retry under
    that. Returns (own_bo, outcome): on success own_bo is the BO the describe
    ran under (None if it stayed under ``resolved``) and outcome is the
    descriptor; on failure own_bo is None and outcome is the SiebelError to
    surface."""
    try:
        descriptor = await auth.cache.get_descriptor(resolved, bc)
    except SiebelError as exc:
        if "SBL-EAI-50257" not in exc.response_body or bc.lower() == resolved.lower():
            return None, exc
        own_bo = await auth.cache.resolve_business_object(bc)
        if not own_bo or own_bo.lower() == resolved.lower():
            return None, exc
        try:
            descriptor = await auth.cache.get_descriptor(own_bo, bc)
        except SiebelError as retry_exc:
            return None, retry_exc
        return own_bo, descriptor
    return None, descriptor


async def execute_bc_describe(args: dict[str, Any], ctx: ToolContext, options: dict[str, Any]) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    source = options["source"]
    render = options["render"]

    if source == "business_objects":
        objects = await auth.cache.get_business_objects()
        return RENDERERS[render]({"business_objects": objects})

    effective = {**(options.get("bind") or {}), **args}
    resolved, bc, raw_bo = await _resolve_business_object(effective, auth)
    if not resolved:
        # The prose variants return _text (informational); the JSON variants
        # return _error (so a programmatic caller can branch on isError).
        msg = _BO_NOT_FOUND.format(bo=raw_bo)
        return _error(msg) if options["json_output"] else _text(msg)

    if source == "bc_descriptor":
        own_bo, outcome = await _descriptor_with_fallback(auth, resolved, bc)
        if isinstance(outcome, SiebelError):
            return _error_result(outcome)
        return RENDERERS[render](
            {"business_object": own_bo or resolved, "business_component": bc, "descriptor": outcome}
        )

    # repository
    fields = await auth.client.describe_repository_bc(resolved, bc)
    return RENDERERS[render]({"business_object": resolved, "business_component": bc, "fields": fields})


def _assemble_bc_describe(entry: dict[str, Any], name: str) -> Any:
    options: dict[str, Any] = {"source": entry["source"], "render": entry["render"]}
    if "bind" in entry:
        options["bind"] = entry["bind"]
    # The BO-not-found path branches on whether the render is a JSON one. Every
    # JSON renderer in the registry is named ``..._json``, so the suffix is the
    # convention; this is set once at load time.
    options["json_output"] = entry["render"].endswith("_json")

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await execute_bc_describe(args, ctx, options)

    return handler


DESCRIBE_KIND: _KindSpec = _KindSpec(
    name="bc_describe",
    allowed_access=frozenset({"read"}),
    allowed_options=_DESCRIBE_ALLOWED_OPTIONS,
    required_options=_DESCRIBE_REQUIRED_OPTIONS,
    validate=_validate_bc_describe,
    assemble=_assemble_bc_describe,
)
