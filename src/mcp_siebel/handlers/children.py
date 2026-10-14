"""Child-record handlers: query / create under a parent, and attachments."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..siebel_client import SiebelError
from ._common import (
    _empty_result,
    _error,
    _error_result,
    _format_record,
    _json_text,
    _require_auth,
    _resolve_bo_bc,
    _result_dict,
    _text,
    _validate_fields_arg,
)

if TYPE_CHECKING:
    from ..tools import ToolContext

log = logging.getLogger("mcp-siebel")


async def _query_child(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    # Same placeholder guard as records._query — see there for rationale.
    bad_fields = _validate_fields_arg(args.get("fields"))
    if bad_fields:
        return _error(bad_fields)
    try:
        result = await auth.client.query_child_records(
            resolved,
            bc,
            str(args["parent_id"]),
            str(args["child_business_component"]),
            search_spec=args.get("searchspec"),
            fields=args.get("fields"),
            sort_spec=args.get("sort_spec"),
            page_size=int(args.get("page_size") or 20),
            offset=int(args.get("offset") or 0),
        )
    except SiebelError as exc:
        if exc.status_code == 404:
            return _empty_result(args)
        return _error_result(exc)
    return _result_dict(result)


async def _query_child_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await _query_child(args, ctx)
    if "items" not in result:
        return result
    rows = [f"Record {result['offset'] + idx + 1}:\n{_format_record(item)}" for idx, item in enumerate(result["items"])]
    return _text("\n\n".join(rows) or "(no child records found)")


async def _query_child_records_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await _query_child(args, ctx)
    return result if "items" not in result else _json_text(result)


async def _create_child_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    result = await auth.client.create_child_record(
        resolved, bc, str(args["parent_id"]), str(args["child_business_component"]), args["data"]
    )
    return _text(f"Created child record(s) under {resolved}/{bc}/{args['parent_id']}:\n{_format_record(result)}")


async def _upload_attachment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    descriptor = await auth.cache.get_descriptor(resolved, str(args["attachment_bc"]))
    names = [field.name for field in descriptor.fields]
    file_name_field = next((name for name in names if name.endswith("FileName")), None)
    file_ext_field = next((name for name in names if name.endswith("FileExt")), None)
    if not file_name_field or not file_ext_field:
        return _error(f"Could not find FileName/FileExt fields in {args['attachment_bc']}.")
    await auth.client.upload_attachment(
        resolved,
        bc,
        str(args["parent_id"]),
        str(args["attachment_bc"]),
        file_name_field,
        file_ext_field,
        str(args["file_name"]),
        str(args["file_ext"]),
        str(args["file_content_base64"]),
        args.get("comment"),
    )
    return _text(f"Attachment uploaded successfully: {args['file_name']}.{args['file_ext']}")
