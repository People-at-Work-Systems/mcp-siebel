"""Echo handler that surfaces the ``preferred_fields`` it received.

A ``python``-kind tool whose handler returns the first entry of the
``Probe`` preferred-fields list it was handed in its ``ToolContext``. It is
the fixture behind the hot-reload atomicity test: because the handler's result
is derived from ``ctx.preferred_fields`` while the tool's *presence* is derived
from the tool map, a call that mixed two spec versions would return the wrong
marker — so a clean (atomic) reload never produces it. No auth, no Siebel call.

No tool names live in framework code; this is a handler module (the escape
hatch), not framework. The tool that uses it is defined in the test spec, not
the bundled one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..common import _text

if TYPE_CHECKING:
    from ..tools import ToolContext


async def probe(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    fields = ctx.preferred_fields.get("Probe")
    return _text(fields[0] if fields else "(none)")
