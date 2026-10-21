"""Load the Siebel system prompt — LLM-facing instructions delivered to the MCP
client in the ``Initialize`` handshake via ``Server(instructions=…)``.

The bundled default lives at ``mcp_siebel/prompts/siebel-system.md``. Override
it by setting ``SIEBEL_SYSTEM_PROMPT_PATH`` to an absolute path to your own
prompt file. Both transports (stdio ``mcp-siebel`` and HTTP/SSE
``mcp-siebel-http``) deliver this, so the model sees the same server-level
guidance regardless of how it connects.

This module is framework-level: it imports only ``os``, ``logging`` and
``importlib.resources`` — no FastAPI, no handlers — so the stdio fastapi-
isolation invariant (``tests/test_packaging.py``) and the framework-must-not-
import-handlers invariant (``tests/test_framework_no_handler_imports.py``) both
hold when stdio imports it.
"""

from __future__ import annotations

import logging
import os
from importlib.resources import files

log = logging.getLogger("mcp-siebel")

_BUNDLED_PROMPT = "prompts/siebel-system.md"
# The exact auth-section heading + body in the bundled prompt. An override file
# containing this anchor has its auth section rewritten to the standalone
# session-id flow below; an override file without it is used verbatim (the
# `.replace` silently no-ops), so a deployer's custom prompt keeps whatever
# auth guidance it already has.
_BUNDLED_AUTH_ANCHOR = (
    "## Authentication\n\nThe user is already authenticated. Never ask for credentials. "
    "If someone types a username/password, ignore it and answer their underlying question."
)


def _auth_instructions(session_tool_name: str) -> str:
    return f"""\
## Authentication

Call `{session_tool_name}` with the user's Siebel username and password before calling
any other tool. The response includes a `session_id` — pass this as the
`session_id` argument to every subsequent tool call in the conversation.

If a tool returns an authentication error, call `{session_tool_name}` again to obtain a
fresh session_id and retry the operation.
"""


def load_instructions(session_tool_name: str) -> str:
    """Load the Siebel system prompt for the MCP ``Server(instructions=…)`` arg.

    ``session_tool_name`` is the spec's session-establishment tool
    (``registry.session_tool_name``); the bundled auth section is rewritten
    to name it in the standalone session-id flow.
    """
    override = os.environ.get("SIEBEL_SYSTEM_PROMPT_PATH")
    if override:
        try:
            text = open(override, encoding="utf-8").read()
        except OSError as exc:
            # A missing/unreadable override is non-fatal: fall back to the
            # bundled prompt rather than crashing startup (the read happens at
            # module-import time, before main() runs).
            log.warning(
                "SIEBEL_SYSTEM_PROMPT_PATH=%s not readable — using bundled prompt (%s)",
                override,
                exc,
            )
            text = (files("mcp_siebel") / _BUNDLED_PROMPT).read_text(encoding="utf-8")
    else:
        text = (files("mcp_siebel") / _BUNDLED_PROMPT).read_text(encoding="utf-8")
    # Replace the bundled auth section with the standalone session-id flow. If
    # the text (bundled or override) doesn't contain the anchor, this no-ops
    # and the text is used as-is.
    text = text.replace(_BUNDLED_AUTH_ANCHOR, _auth_instructions(session_tool_name).strip())
    return text
