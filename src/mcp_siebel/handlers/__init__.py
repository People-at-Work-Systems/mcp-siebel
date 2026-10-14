"""Tool handlers, loaded by the registry from ``tool_specs.json``.

Each tool's metadata (description + input_schema) lives in
``mcp_siebel/specs/tool_specs.json``; its handler is a ``"kind": "python"``
entry pointing here via ``"handler": "mcp_siebel.handlers.<module>:<func>"``.
The registry resolves those references with ``importlib`` and only permits
modules under this package (see ``mcp_siebel.tools._resolve_handler``).

Modules are grouped by domain:

* ``_common`` — shared helpers (auth gate, result constructors, renderers)
  used across the domain modules below.
* ``auth`` — session establishment.
* ``metadata`` — catalogue / describe / LOV reads.
* ``records`` — BC query / upsert / delete on top-level records.
* ``children`` — child-record query / create and attachments.
* ``util`` — date arithmetic, query-syntax reference, web search.
"""
