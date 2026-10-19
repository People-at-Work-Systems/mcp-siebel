"""Tool handlers, loaded by the registry from ``tool_specs.json``.

Each tool's metadata (description + input_schema) and ``kind`` live in
``mcp_siebel/specs/tool_specs.json``. A ``"kind": "python"`` entry points here
via ``"handler": "mcp_siebel.handlers.<module>:<func>"``; the registry resolves
those references with ``importlib`` and only permits modules under this package
(see ``mcp_siebel.tools._resolve_handler``). Generic executors (the other kinds)
live under ``mcp_siebel.executors`` and do not point here.

This package holds only the remaining ``python``-kind tools and what they alone
use. The shared helpers they used to own (result constructors, auth gate,
field validation, record formatting) moved to ``mcp_siebel.common`` so the
framework layer (executors, renderers, tools) never imports from here — the
layering invariant pinned by ``tests/test_framework_no_handler_imports.py``.
The curated-query fallback helpers moved to ``mcp_siebel.executors._fallback``.

Modules are grouped by domain:

* ``auth`` — session establishment.
* ``metadata`` — the List-of-Values read (catalogue/describe moved to the
  ``bc_describe`` executor).
* ``records`` — the ``aggregate_data`` handler.
* ``children`` — child-record attachments.
* ``util`` — date arithmetic, web search.
"""
