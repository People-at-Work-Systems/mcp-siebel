"""Tool handlers, loaded by the registry from ``tool_specs.json``.

Each tool's metadata (description + input_schema) and ``kind`` live in
``mcp_siebel/specs/tool_specs.json``. A ``"kind": "python"`` entry points here
via ``"handler": "mcp_siebel.handlers.<module>:<func>"``; the registry resolves
those references with ``importlib`` and only permits modules under this package
(see ``mcp_siebel.tools._resolve_handler``). Generic executors (the other kinds)
live under ``mcp_siebel.executors`` and do not point here.

Modules are grouped by domain:

* ``_common`` — shared helpers (auth gate, result constructors, field
  validation) used across the domain modules and by the executors.
* ``auth`` — session establishment.
* ``metadata`` — catalogue / describe / LOV reads.
* ``records`` — aggregate / get / create / update / delete on top-level records.
* ``children`` — child-record create and attachments.
* ``util`` — date arithmetic, query-syntax reference, web search.
"""
