"""LOV read handler. The catalogue / describe handlers moved to the generic
``bc_describe`` executor (see executors/); only the List-of-Values handler —
which builds a business-service payload and has no generic executor yet — stays
here as a ``python``-kind tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..common import _error_result, _json_text, _require_auth, _text
from ..siebel_client import SiebelError

if TYPE_CHECKING:
    from ..tools import ToolContext


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


async def _list_rest_enabled_components(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Discover the Business Components actually exposed through the REST data
    API. With a ``business_object``, lists BCs under that one BO. Without one,
    enumerates every BO (capped by ``discovery_max_business_objects``) and lists
    BCs per BO — an N+1 of describe calls mitigated by the SchemaCache and the
    cap. A BO whose describe fails shows ``(none)`` rather than aborting the
    sweep. Builds on ``list_business_objects`` and ``describe_repository``."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    if args.get("business_object"):
        resolved = await auth.cache.resolve_business_object(str(args["business_object"]))
        if not resolved:
            return _text(f'Business Object "{args["business_object"]}" not found.')
        try:
            bcs = await auth.cache.get_business_components(resolved)
        except SiebelError as exc:
            return _error_result(exc)
        if not bcs:
            return _text(f"{resolved} exposes no Business Components through the REST data API.")
        comp_lines = "\n".join(f"  - {bc}" for bc in bcs)
        return _text(f"{resolved} — REST-enabled Business Components ({len(bcs)}):\n{comp_lines}")
    bos = await auth.cache.get_business_objects()
    total = len(bos)
    cap = ctx.settings.discovery_max_business_objects
    truncated = total > cap
    bos = bos[:cap]
    body_lines: list[str] = []
    for bo in bos:
        try:
            bcs = await auth.cache.get_business_components(bo)
        except SiebelError:
            bcs = []
        label = ", ".join(bcs) if bcs else "(none)"
        body_lines.append(f"  {bo}: {label}")
    header = f"Business Objects with REST-enabled Business Components ({len(bos)}/{total}"
    header += ", truncated" if truncated else ""
    header += "):"
    return _text(header + ("\n" + "\n".join(body_lines) if body_lines else "\n(no Business Objects found)"))
