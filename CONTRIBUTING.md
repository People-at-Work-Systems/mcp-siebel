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
2. Check the `mcp` changelog — the `mcp<2` pin must stay until the 2.x
   decorator-API migration is done.
3. Bump `version` in `pyproject.toml` (Semantic Versioning).
4. Commit, tag `vX.Y.Z`, push the tag.
5. The `release.yml` workflow builds and publishes to PyPI via OIDC trusted
   publishing (configured once in the PyPI web UI).

## A note on the `mcp` pin

`mcp` 2.0 removed the `@McpServer.list_tools()` / `@McpServer.call_tool()`
decorators that `stdio.py` and `http_server.py` rely on. The pin
(`mcp>=1.0,<2`) is deliberate. Migrating to `mcp` 2.x is a separate task.