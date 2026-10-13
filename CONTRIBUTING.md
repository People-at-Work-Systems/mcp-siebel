# Contributing

## Development setup

```bash
pip install -e .[dev]
pytest            # existing suite (mocked HTTP — no live Siebel needed)
ruff check .
mypy src/mcp_siebel  # src only; tests carry pre-existing annotation debt
```

The `test_packaging.py` and `test_no_sibling_refs.py` guards enforce two
invariants that must hold on every change:

- **FastAPI isolation** — `import mcp_siebel.stdio` must not pull `fastapi` or
  `prometheus_fastapi_instrumentator` into `sys.modules` (those are imported
  only by `http_server.py`, which the stdio path never touches). `uvicorn` and
  `starlette` are NOT gated this way — the `mcp` SDK imports them transitively
  as core deps, so they arrive on any install regardless of `[http]`. Do not
  add a `fastapi`/`prometheus` import to `__init__.py`, `stdio.py`, `tools.py`,
  or `_http_entry.py`.
- **Zero coupling** — the package must not reference any sibling service, in
  code, comments, docstrings, or the bundled prompt. See
  `tests/test_no_sibling_refs.py` for the forbidden list.

## Releasing

1. Run a secret scan: `gitleaks detect --source . --no-git` (or `trufflehog`).
2. Check the `mcp` changelog for breaking changes in the 2.x line; bump the
   `mcp>=2.0,<3` pin if a 3.0 lands.
3. Bump `version` in `pyproject.toml` (Semantic Versioning).
4. Commit, tag `vX.Y.Z`, push the tag.
5. The `release.yml` workflow builds and publishes to PyPI via OIDC trusted
   publishing (configured once in the PyPI web UI).

## A note on the `mcp` pin

The server uses the mcp 2.x lowlevel `Server` API — handlers are registered
via the `on_list_tools=` / `on_call_tool=` constructor kwargs (mcp 2.0 removed
the `@Server.list_tools()` / `@Server.call_tool()` decorators the 1.x code
used). The pin is `mcp>=2.0,<3`. The stdio and SSE transports
(`stdio_server`, `SseServerTransport` / `connect_sse` / `handle_post_message`),
`Server.run()`, and `create_initialization_options()` all kept their v1
signatures in v2, so the migration was confined to the handler registration.