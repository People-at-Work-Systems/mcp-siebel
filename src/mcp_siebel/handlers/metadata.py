"""Catalogue / describe / LOV read handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..siebel_client import SiebelError
from ._common import _error, _error_result, _json_text, _require_auth, _text

if TYPE_CHECKING:
    from ..tools import ToolContext


async def _list_business_objects(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    objects = await auth.cache.get_business_objects()
    return _text(f"Available Business Objects ({len(objects)}):\n" + "\n".join(f"  - {item}" for item in objects))


async def _describe_business_object(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _text(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)
    try:
        descriptor = await auth.cache.get_descriptor(resolved, bc)
    except SiebelError as exc:
        # The requested BC isn't part of `resolved`'s Integration Object —
        # most often the model described a *child* component, e.g.
        # describe_business_object(Account, Contact). Contact lives in its
        # own Business Object, so retry describing it under its own BO
        # instead of returning a 50257 "not mapped" error. Skip the retry
        # when the BC is the BO itself (nothing to fall back to).
        if "SBL-EAI-50257" not in exc.response_body or bc.lower() == resolved.lower():
            return _error_result(exc, "describe_business_object")
        own_bo = await auth.cache.resolve_business_object(bc)
        if not own_bo or own_bo.lower() == resolved.lower():
            return _error_result(exc, "describe_business_object")
        try:
            descriptor = await auth.cache.get_descriptor(own_bo, bc)
        except SiebelError as retry_exc:
            return _error_result(retry_exc, "describe_business_object")
        # The describe actually ran under the BC's own BO; reflect that in
        # the header so the field list isn't mislabelled as the parent BO's.
        resolved = own_bo
    return _text(
        f"{resolved} / {bc}\n\n{auth.cache.field_summary(descriptor)}\n\nTotal fields: {len(descriptor.fields)}"
    )


async def _describe_business_object_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Structured-JSON variant of describe_business_object — the REST data API
    field list (the same source the dashboard render uses successfully). More
    reliable than describe_repository on instances where the workspace metadata
    API + Repository BC fallback return nothing. Used by the CRM navigator for
    the result-list columns + the name field. No LOV types here — those come
    from describe_repository_json (best-effort) where available."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _error(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)
    try:
        descriptor = await auth.cache.get_descriptor(resolved, bc)
    except SiebelError as exc:
        return _error_result(exc, "describe_business_object_json")
    return _json_text(
        {
            "business_object": resolved,
            "business_component": bc,
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


async def _describe_repository(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _text(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)

    fields = await auth.client.describe_repository_bc(resolved, bc)
    if not fields:
        return _text(
            f"No repository metadata found for {resolved}/{bc}. "
            "The workspace metadata API may not be accessible. "
            "Use describe_business_object for the REST field list instead."
        )

    lines = [f"Repository metadata: {resolved} / {bc}", f"Total fields: {len(fields)}", ""]
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


async def _list_business_objects_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Structured-JSON variant of list_business_objects. The prose variant
    renders a text list, which is fine for the LLM but not for a caller that
    needs the array."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    objects = await auth.cache.get_business_objects()
    return _json_text({"business_objects": objects})


async def _describe_repository_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Structured-JSON variant of describe_repository. Returns per-field name,
    type, LOV type (so the caller can fetch LOV values), and flags — not the
    prose field list the LLM-oriented variant renders."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _error(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)
    fields = await auth.client.describe_repository_bc(resolved, bc)
    return _json_text(
        {
            "business_object": resolved,
            "business_component": bc,
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


async def _get_list_of_values(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    lov_types = args.get("lov_types") or []
    inputs = {
        "body": {
            "ListsTopElementQuery": {
                "MessageId": "",
                "MessageType": "Integration Object",
                "IntObjectName": "List Query",
                "IntObjectFormat": "Siebel Hierarchical",
                "ListOfList Query": {
                    "List": [
                        {"Active": "Y", "Language Code": ctx.settings.language_code, "Type": lov_type}
                        for lov_type in lov_types
                    ]
                },
            }
        }
    }
    response = await auth.client.call_business_service("EAILOVService", "GetListOfValues", inputs)
    entries = ((response or {}).get("ListsTopElementResult") or {}).get("List") or []
    result: dict[str, list[str]] = {lov_type: [] for lov_type in lov_types}
    for entry in entries:
        values = (entry.get("List Value") or []) if isinstance(entry, dict) else []
        result[str(entry.get("Type"))] = [
            str(value.get("Display Value", "")) for value in values if isinstance(value, dict)
        ]
    return _json_text(result)
