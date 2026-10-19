"""The ``static_text`` executor: return a fixed text.

This is the generic executor behind the spec kind ``static_text``. It replaces
the per-tool ``_get_query_syntax`` handler: the text lives in the spec entry's
``text`` option (a string, or a list of strings joined with ``\\n`` so long
texts stay readable in JSON), and the executor returns it verbatim as the tool's
text content. No auth, no Siebel call — like the handler it replaces, the tool
works without a session.

No tool names live in this module. The executor is bound to a spec entry at load
time (``_assemble_static_text`` closes over the joined text) and the resulting
callable is the single handler ``ToolRegistry.call`` invokes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..common import _text
from ..kinds import _COMMON_FIELDS, _KindSpec, _SpecError

if TYPE_CHECKING:
    from ..tools import ToolContext

_STATIC_TEXT_ALLOWED_OPTIONS = frozenset({"text"})
_STATIC_TEXT_REQUIRED_OPTIONS = frozenset({"text"})


def _validate_static_text(entry: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    options = set(entry.keys()) - _COMMON_FIELDS
    extra = options - _STATIC_TEXT_ALLOWED_OPTIONS
    if extra:
        raise _SpecError(f"unknown option(s) {sorted(extra)} for static_text entry {name!r}")
    missing = _STATIC_TEXT_REQUIRED_OPTIONS - entry.keys()
    if missing:
        raise _SpecError(f"static_text entry {name!r} missing required option(s) {sorted(missing)}")
    text = entry.get("text")
    if isinstance(text, list):
        if not all(isinstance(line, str) for line in text):
            raise _SpecError(f"static_text entry {name!r} 'text' list must contain only strings")
    elif not isinstance(text, str):
        raise _SpecError(f"static_text entry {name!r} 'text' must be a string or a list of strings")


def _assemble_static_text(entry: dict[str, Any], name: str) -> Any:
    raw = entry["text"]
    text = "\n".join(raw) if isinstance(raw, list) else raw

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return _text(text)

    return handler


STATIC_TEXT_KIND: _KindSpec = _KindSpec(
    name="static_text",
    allowed_access=frozenset({"read"}),
    allowed_options=_STATIC_TEXT_ALLOWED_OPTIONS,
    required_options=_STATIC_TEXT_REQUIRED_OPTIONS,
    validate=_validate_static_text,
    assemble=_assemble_static_text,
)
