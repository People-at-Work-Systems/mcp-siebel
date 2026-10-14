"""Child-record handlers: attachments. The create-child-record handler moved
to the generic ``bc_upsert`` executor (see executors/)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._common import (
    _error,
    _require_auth,
    _resolve_bo_bc,
    _text,
)

if TYPE_CHECKING:
    from ..tools import ToolContext


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
