"""LOV read handler. The catalogue / describe handlers moved to the generic
``bc_describe`` executor (see executors/); only the List-of-Values handler —
which builds a business-service payload and has no generic executor yet — stays
here as a ``python``-kind tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._common import _json_text, _require_auth

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
