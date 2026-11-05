"""Child-record handlers: attachments. The create-child-record handler moved
to the generic ``bc_upsert`` executor (see executors/)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..common import (
    _error,
    _error_result,
    _require_auth,
    _resolve_bo_bc,
    _text,
)
from ..siebel_client import SiebelError

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


async def _download_attachment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Download one attachment. Returns the file contents base64-encoded when
    the decoded size is under ``attachment_max_bytes``; over the cap (or when the
    row carries no payload), returns metadata only (name, extension, size,
    attachment id) plus a notice. Counterpart to ``_upload_attachment``."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    try:
        record = await auth.client.download_attachment(
            resolved,
            bc,
            str(args["parent_id"]),
            str(args["attachment_bc"]),
            str(args["id"]),
        )
    except SiebelError as exc:
        return _error_result(exc)
    if not isinstance(record, dict):
        record = {}
    payload = record.get("Attachment Id") or ""
    name = record.get("FileName") or ""
    ext = record.get("FileExt") or ""
    size = record.get("File Size")
    attachment_id = str(args["id"])
    metadata = (
        f"Attachment: {name}.{ext}".rstrip(".") + f"\nAttachment Id: {attachment_id}"
        + (f"\nFile Size: {size}" if size not in (None, "") else "")
    )
    if not payload:
        return _text(f"{metadata}\nNo file payload was returned for this attachment.")
    # base64 length -> approximate decoded byte count (3 bytes per 4 chars).
    decoded_bytes = len(payload) * 3 // 4
    cap = ctx.settings.attachment_max_bytes
    if decoded_bytes > cap:
        return _text(
            f"{metadata}\nDecoded size ~{decoded_bytes:,} bytes exceeds the "
            f"SIEBEL_ATTACHMENT_MAX_BYTES cap ({cap:,}). Raise the cap or choose a "
            "smaller file; the file contents were not returned."
        )
    return _text(f"{metadata}\n\n{payload}")
