# CLAUDE.md — mcp-siebel

Guidance for Claude Code working in this repo. Read this first; it is the map of
the project, the architecture, the invariants that must not be broken, and how to
run things.

## What this is

mcp-siebel is an MCP (Model Context Protocol) tool server for Oracle Siebel
26.2+. The tool set is **spec-driven**: a JSON file (`src/mcp_siebel/specs/tool_specs.json`)
defines every tool — its kind, description, `input_schema`, per-tool `enabled`
flag, and (for the `python` kind) a handler reference. The registry reads it at
startup, resolves a generic executor per tool from the entry's `kind`, and
hot-reloads it on change. Two transports: **stdio** (`mcp-siebel`, default) and
**HTTP/SSE** (`mcp-siebel-http`, the `[http]` extra). Siebel auth is held
server-side per session.

Authoring reference: [`docs/tool-specs.md`](docs/tool-specs.md).
Review/remediation history: [`docs/reviews/`](docs/reviews/).
Roadmap (planning, not commitments): [`ROADMAP.md`](ROADMAP.md).

## Repo layout

```
src/mcp_siebel/
  config.py          AppSettings (env-driven, SIEBEL_ prefix + MCP_API_TOKEN alias)
  tools.py           ToolRegistry, spec loader, _deref, ToolSnapshot, call()
  kinds.py            _KindSpec, _SpecError, _COMMON_FIELDS (no deps on tools/executors)
  common.py          shared framework helpers (_error, _require_auth, _format_arg_errors, …)
  renderers.py        renderer registry + families; validate_render
  siebel_client.py   httpx AsyncClient wrapper; TLS verify (verify_tls/ca_bundle)
  schema_cache.py     BC descriptor cache
  context.py         SessionContext, ToolContext, correlation middleware
  stdio.py            stdio transport (no fastapi import)
  http_server.py       FastAPI + MCP SSE transport (the [http] extra)
  notifications.py    tools/list_changed + REST ETag/SSE fan-out
  watcher.py          watchfiles spec hot-reload watcher
  executors/          one generic executor per kind (bc_query, bc_upsert, bc_read,
                      bc_delete, bc_describe, static_text); _validation.py (bind/required)
  handlers/           the "python"-kind escape hatch (auth, records, children, metadata,
                      probe, util). Framework code must NOT import from here.
  specs/
    tool_specs.json         the bundled tool spec (the single source of truth)
    tool_specs.schema.json  JSON Schema for editor validation (authoring aid)
  prompts/siebel-system.md  the bundled system prompt
tests/                 ~377 tests, mocked HTTP (no live Siebel needed)
docs/                  tool-specs.md, mcp-clients.md, reviews/, specs/
```

Entry points: `mcp-siebel` → `stdio:main`; `mcp-siebel-http` → `_http_entry:main`.

## Architecture

**Spec → loader → snapshot → call.** `_load_valid_tools` runs the full pipeline
(parse with duplicate-key detection → `_deref` `$ref` → validate → per-entry kind +
executor + `enabled`/session-rule resolution) and, on success, builds a fresh
immutable `ToolSnapshot` and swaps the registry's single `_snapshot` reference
under a lock. `list_tools()` and `call()` read that reference exactly once per
request, so a request never mixes data from two spec versions.

**Per-tool `enabled` is mandatory** (fail-closed — no silent default). The
bundled spec sets `enabled: false` on the 7 write/delete/external tools
(read-only by default) and `enabled: true` on the other 19.

**Argument validation at call time.** `call()` hot-reloads, then validates
`arguments` against the tool's resolved `input_schema` (a `Draft202012Validator`
built per tool at load time and cached on `ToolSnapshot.validators`) before
dispatch, returning a specific param/expected/received error (via
`_format_arg_errors`) — not the internal mask. `additionalProperties: false`
is set on every bundled top-level `input_schema`; `page_size` is min 1 / max 100
and `offset` min 0.

**Bind pins win.** `bind` fixes values merged as `{**args, **bind}` — a pinned
key cannot be overridden by the caller. Bound keys are disjoint from
`input_schema.properties` (enforced at load), so a caller supplying a bound key
is rejected as an unknown property.

**Allowed/required options are enforced once, centrally** in `_load_valid_tools`
from each `_KindSpec`; the per-executor `validate` callbacks hold only
kind-specific logic (enums, render family, bind, bind types).

**Hot-reload.** A `watchfiles` watcher (started by both transports) plus a
lazy mtime fallback in `list_tools()`/`snapshot()`/`call()` rebuilds the
snapshot on a spec change. Keep-last-valid-on-reload / clear-on-first-load: a
failed reload keeps the last valid set serving and logs; a failed first load
aborts startup (non-zero exit). `call()` reloads before dispatch too, so a
tool flipped to `enabled: false` is refused even with no prior `list_tools()`.

**`_deref` rules.** Only `#/$defs/<name>` refs; cycles rejected; a `$ref` with
sibling keys rejected (merge the definition instead); `$ref` chain depth capped
at 100 (a deeper non-cyclic chain is a load error, not a crash).

**Session establishment.** Exactly one entry marks `establishes_session: true`
(the transports read its name from `session_tool_name`). Both transports always
create a FRESH session for the session tool — a caller-supplied `session_id`
for that call is ignored, so `authenticate` can't rebind an existing session to
a different identity.

**TLS.** `SiebelClient` verifies the Siebel certificate by default
(`SIEBEL_VERIFY_TLS=true`; `SIEBEL_CA_BUNDLE` overrides to a CA bundle path).
`verify=False` is never hardcoded; dev-only opt-out via `SIEBEL_VERIFY_TLS=false`.

**Token guard.** `MCP_API_TOKEN` (alias `SIEBEL_API_TOKEN`) guards the HTTP/SSE
server when set; `/sse` and `/messages/` are guarded too (the mounted `/messages/`
sub-app via a scoped middleware). The HTTP server logs a startup warning when no
token is set.

## Invariants pinned by tests (do not break)

- **`tests/test_packaging.py`** — `import mcp_siebel.stdio` must not pull
  `fastapi`/`prometheus_fastapi_instrumentator` into `sys.modules` (those are
  `[http]`-gated). Do not add such an import to `__init__.py`, `stdio.py`,
  `tools.py`, or `common.py`.
- **`tests/test_framework_no_handler_imports.py`** — framework code
  (`tools`, `kinds`, `common`, `renderers`, `executors/`, `context`,
  `notifications`, `watcher`, `siebel_client`, `schema_cache`) must not import
  from `mcp_siebel.handlers`. Handlers are the escape hatch, not a framework
  dependency.
- **`tests/test_no_sibling_refs.py`** — the bundled spec has no `$ref` with
  sibling keys.
- **`tests/test_spec_schema.py`** — the JSON Schema's render enums cover every
  renderer; the schema's kinds match the registry's `_KINDS`; `enabled` is
  required (no default).
- **`tests/test_tool_specs.py`** — every bundled entry declares a known kind
  with valid options, resolves a real handler (for `python`), and carries an
  explicit `enabled`; all 26 load.

## Running things

```bash
uv sync --extra dev          # install runtime + dev/http extras
uv run python -m pytest -q   # full suite (~377 tests, mocked HTTP — no live Siebel)
uv run ruff check src tests
uv run mypy src              # src only; tests carry pre-existing annotation debt
```

Config is env-driven (`SIEBEL_` prefix; `MCP_API_TOKEN`/`SIEBEL_API_TOKEN`
alias). Full table: [`README.md`](README.md) § Configuration; client setup:
[`docs/mcp-clients.md`](docs/mcp-clients.md).

## Conventions

- **No tool names in `tools.py`/`executors/`/`kinds.py`/`common.py`.** Adding a
  tool is a spec edit (and, for the `python` kind, a handler function); the
  framework stays name-free.
- **Match the surrounding code** — comment density, naming, the `_`prefixed
  internal helper style, `from __future__ import annotations` at the top.
- **Commits** end with `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
  Don't push unless asked.
- The bundled spec is normalized to a conventional key order (description, kind,
  enabled, …kind-specific options…, input_schema last); `json.dumps(indent=2,
  ensure_ascii=True)` with no trailing newline round-trips it cleanly.