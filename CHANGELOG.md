# Changelog

All notable changes to mcp-siebel are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-10-21

Remediation of the external design/security review of the 0.2.0 refactor, plus a
follow-up documentation round. Review inputs and the per-finding mapping live in
[`docs/reviews/`](docs/reviews/). PyPI publishing is still deferred — installs
pull from the git repo for now.

### Security
- `MCP_API_TOKEN` is now actually read (it was declared but never consumed):
  an `AppSettings` alias accepting both `MCP_API_TOKEN` and the legacy
  `SIEBEL_API_TOKEN`. The MCP SSE transport (`/sse`, `/messages/`) is now
  guarded by the same token — previously it bypassed the guard entirely.
- Spec `bind` now wins over caller args (`{**args, **bind}`), so a pinned
  Business Component can no longer be overridden by the caller.
- Tool-call arguments are now validated against the tool's resolved
  `input_schema` before dispatch (`jsonschema>=4.20` is now an explicit runtime
  dep). A missing required, wrong-type, out-of-range, or unknown property returns
  a specific param/expected/received error instead of the internal mask.
  Every bundled `input_schema` carries `additionalProperties: false`; `page_size`
  is min 1 / max 100, `offset` min 0.
- `enabled` is mandatory on every spec entry — no silent fail-open default. Bundled
  spec: 19 `enabled: true` / 7 `enabled: false` (read-only by default).
- `ToolRegistry.call()` hot-reloads the spec before dispatch, so a tool flipped
  to `enabled: false` in an override spec is refused even when the caller
  never re-listed.
- Session hijack via `authenticate` rebind is closed: both transports always
  create a fresh session for the session tool; the old id keeps working for its
  original owner.
- TLS verification is on by default (`SIEBEL_VERIFY_TLS`, `SIEBEL_CA_BUNDLE`),
  replacing hardcoded `verify=False` on every outbound Siebel call.

### Changed
- `bc_upsert` rejects `operation: "update"` with `scope: "child"` (child-update
  was never implemented; the update path ignores scope). The `upsert` render
  family is split into `create` / `update` / `child_create` so a create tool
  can't render an update (or vice versa).
- `_deref` rejects a `$ref` carrying sibling keys (previously silently dropped)
  and caps `$ref` chain depth at 100 (a deep non-cyclic chain is a load
  error, not an uncaught `RecursionError`).
- Allowed/required option enforcement is centralized in the registry from each
  `_KindSpec`; the duplicate extra/missing checks are removed from the six
  executors.
- `SIEBEL_SYSTEM_PROMPT_PATH` is now delivered on both transports: the loader is
  extracted into a shared `system_prompt.py` so the HTTP/SSE `Server` gets
  `instructions=` too (previously only stdio delivered it). A non-existent
  override now falls back to the bundled prompt instead of crashing stdio
  startup.

### Removed
- Dead OAuth/Keycloak stub: `oauth_client_id`, `oauth_client_secret`, and
  `keycloak_token_url` were declared in `config.py` but never read by any
  runtime code (`SiebelClient` has no oauth mode; no token-exchange code
  exists). Removed from `config.py`, `.env.example`, the README "Siebel
  prerequisites", and the publish-design doc.

### Added
- `CLAUDE.md` — project map for Claude Code sessions.
- Full env-var reference: the README Configuration table is expanded from an
  "essentials" table (11 rows) to the complete reference (all 23 env vars,
  grouped). `docs/mcp-clients.md` points to it.
- `docs/reviews/2026-10-21-external-tool-specs-remediation.md` — the
  per-finding remediation write-up.

## [0.2.0] - 2026-10-20

Spec-driven tool registry: the tool set is now defined by a JSON spec file
(`src/mcp_siebel/specs/tool_specs.json`) — not by code — and hot-reloaded on
change. Migrated to mcp 2.x.

### Added
- Spec-driven tool registry: each tool's kind, description, `input_schema`,
  per-tool `enabled` flag, and (for the `python` kind) handler reference come
  from the spec. Generic executors (one per kind: `bc_query`, `bc_upsert`,
  `bc_read`, `bc_delete`, `bc_describe`, `static_text`) bind to an entry at
  load time.
- Hot-reload: a `watchfiles` watcher (`SIEBEL_TOOLS_SPEC_WATCH`) plus a lazy
  mtime fallback rebuild the tool set on a spec edit; connected clients are
  notified — MCP clients get `notifications/tools/list_changed`, HTTP REST
  clients get an `ETag`/`If-None-Match`→304 on `GET /tools` and a
  `tools_changed` SSE event on `GET /tools/events`. Keep-last-valid-on-reload /
  clear-on-first-load contract.
- JSON Schema for `tool_specs.json` (`tool_specs.schema.json`), referenced via
  `"$schema"` for editor validation/autocomplete.
- Per-tool `enabled` flag (authored in the spec) replaces the
  `SIEBEL_ALLOW_WRITE` / `SIEBEL_ALLOW_DELETE` / `SIEBEL_ALLOW_EXTERNAL`
  access-gate env vars. The bundled spec ships read-only by default (7
  write/delete/external tools `enabled: false`).
- Framework/handler layering refactor: shared helpers move out of `handlers/`
  into framework modules (`common.py`, `executors/`, `kinds.py`, `renderers.py`).
  Framework code no longer imports from `handlers/` (pinned by
  `tests/test_framework_no_handler_imports.py`).
- `bc_read` executor; `get_record` and `get_record_with_children` migrated to
  it (per-mode render family + per-mode bind keys).
- Atomic `ToolSnapshot` (swapped under a lock; carries `spec_version` and a
  SHA-256 `content_hash` of the exposed tool list).

### Changed
- Migrated to mcp 2.x (`on_list_tools`/`on_call_tool` constructor kwargs).
- `get_query_syntax`'s `user_question` argument is now optional.

## [0.1.0] - 2026-10-13

Initial release. MCP server for Oracle Siebel CRM 26.2+.

### Added
- stdio (`mcp-siebel`) and HTTP/SSE (`mcp-siebel-http`, the `[http]` extra)
  transports.
- SSO-header auth (`SIEBEL_SSO_HEADER`); server-side per-session Siebel
  authentication.
- The initial tool set: query/count, get/list records, describe, create/update/
  delete records, attachments, aggregate, web search, calculate date, get query
  syntax.
- Auth-probe (`SIEBEL_PROBE_BUSINESS_OBJECT`/`_COMPONENT`) to validate the SSO
  identity with a real data query.
- `MCP_API_TOKEN` bearer guard on the HTTP server.
- Bundled Siebel system prompt; `SIEBEL_SYSTEM_PROMPT_PATH` override.
- Prometheus `/metrics`, `/health`.
- Demo mode (`SIEBEL_DEMO_ENABLED`) serving sample data without Siebel I/O.

[0.2.0...v0.2.1]: https://github.com/People-at-Work-Systems/mcp-siebel/compare/v0.2.0...v0.2.1
[0.1.0...v0.2.0]: https://github.com/People-at-Work-Systems/mcp-siebel/compare/v0.1.0...v0.2.0