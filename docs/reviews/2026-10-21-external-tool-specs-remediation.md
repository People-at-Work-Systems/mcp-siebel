# Review remediation — `feat/external-tool-specs`

> **Status:** All 13 findings addressed. Remediation lives on
> `docs/review-external-tool-specs` (the review branch), with the refactor
> code merged in via `--no-ff` so the review doc, the refactor, and the fixes
> coexist on one branch.
>
> - **Date:** 2026-10-21
> - **Reviews:** addresses every finding in
>   [`2026-10-20-external-tool-specs-review.md`](2026-10-20-external-tool-specs-review.md)
> - **Base merged:** `feat/external-tool-specs` @ `d98b4ff` (clean `--no-ff` merge,
>   no conflicts — the review branch only added the review file)
> - **Method:** Each fix is covered by an automated test (named below); the full
>   suite is green (`uv run python -m pytest -q` → 377 passed), `ruff` and `mypy`
>   clean. No live-Siebel smoke was run (no dev instance at hand); the equivalent
>   behaviors are pinned by the tests.

## Commits

| Commit | Findings | Subject |
|---|---|---|
| `62d052c` | — | `merge: feat/external-tool-specs into review branch` |
| `37cbfbf` | 1, 2 | `fix(security): MCP_API_TOKEN alias + guard SSE transport` |
| `231fc68` | 3, 6 | `fix(security): bind pins win over caller args + arg validation` |
| `a3f0640` | 4 | `fix(gating): make enabled mandatory (fail-closed)` |
| `561b748` | 5, 7, 8 | `fix(security): reload on call, stop session rebind, default TLS verify on` |
| `e54ad14` | 9, 10, 11, 12 | `fix(spec): upsert child-update/render-family, $ref siblings, depth limit` |
| `b745486` | 13 | `refactor(kinds): centralize allowed/required option enforcement` |
| `dddbd67` | — | `docs: review-remediation env vars + install notes` |

## Finding-by-finding

### 1 (Critical) — `MCP_API_TOKEN` never read
`AppSettings.api_token` now reads **both** `MCP_API_TOKEN` (the documented name)
and the legacy `SIEBEL_API_TOKEN` via `AliasChoices` — a single alias would have
silently dropped the prefixed form, so `AliasChoices` keeps both working.
`populate_by_name=True` keeps the field settable by name. The HTTP server logs a
startup warning when no token is set.
*Files:* `src/mcp_siebel/config.py`, `src/mcp_siebel/http_server.py`.
*Tests:* `tests/test_api_token.py` (env population for both names; `/tools` 401/200).

### 2 (Critical) — MCP SSE transport bypassed the token guard
`/sse` now carries `require_api_token`; `/messages/` (a mounted sub-app that
can't take route deps) is guarded by a scoped HTTP middleware for paths
starting with `/sse` or `/messages/`. The guard body is factored into
`_check_api_token`, shared by the dependency and the middleware (the middleware
returns 401 directly rather than raising, which would surface as 500).
*Files:* `src/mcp_siebel/http_server.py`.
*Tests:* `tests/test_api_token.py` (`/sse` and `/messages/` 401 without header,
not-401 with).

### 3 (Critical) — caller args overrode the spec `bind`
The bind/args merge order is flipped in all five executors (`bc_upsert`,
`bc_query`, `bc_read`, `bc_delete`, `bc_describe`) so the spec bind wins:
`{**args, **bind}`. Bound keys are disjoint from `input_schema.properties`
(enforced at load), so finding 6's `additionalProperties: false` additionally
rejects a caller-supplied bound key as an unknown property.
*Files:* `src/mcp_siebel/executors/{upsert,query,read,delete,describe}.py`.
*Tests:* `tests/test_bind_pinning.py`.

### 4 (High) — `enabled` defaulted true (fail-open)
`enabled` is now **mandatory** on every entry — no silent default. The schema
lists it in `toolEntry.required` and drops `default: true`; `_valid_spec_entry`
requires it present and boolean (and now returns a specific reason naming the
failed field); `ToolSpec` construction reads `entry["enabled"]` directly. The
bundled spec presets `enabled: true` on the 19 read tools and keeps
`enabled: false` on the 7 gated tools (19/7, 26 total).
*Files:* `src/mcp_siebel/specs/tool_specs.{json,schema.json}`, `src/mcp_siebel/tools.py`.
*Tests:* `tests/test_gating.py` (`test_omitting_enabled_is_rejected`,
`test_new_tool_without_enabled_is_rejected`), `tests/test_tool_specs.py`
(all 26 carry `enabled`), `tests/test_spec_schema.py` (`enabled` required, no
default).

### 5 (High) — `call()` did not reload
`ToolRegistry.call()` now hot-reloads the spec before dispatch (it previously
read a stale snapshot, so a tool flipped to disabled in an override spec was still
callable when the caller never invoked `list_tools()` first). The snapshot
reference is re-read exactly once after the reload.
*Files:* `src/mcp_siebel/tools.py`.
*Tests:* `tests/test_gating.py` (`test_call_reloads_disabled_tool_without_prior_list`).

### 6 (High) — arguments never validated
`jsonschema>=4.20` is now an explicit runtime dep. A `Draft202012Validator` per
tool is built at load time and cached on `ToolSnapshot.validators`; `call()`
validates `arguments` before dispatch and returns a specific
param/expected/received error (via `_format_arg_errors`) instead of the
internal mask. The bundled `input_schema`s are tightened with
`additionalProperties: false` on each top-level schema, `minimum: 1`/`maximum:100`
on `page_size`, and `minimum: 0` on `offset` (the `$defs` are inlined by
`_deref`, so the bounds reach every referencing tool).
*Files:* `pyproject.toml`, `src/mcp_siebel/specs/tool_specs.json`,
`src/mcp_siebel/tools.py`, `src/mcp_siebel/common.py`.
*Tests:* `tests/test_arg_validation.py` (missing required, wrong type, min/max,
unknown property, valid-args-pass-through).

> **Schema refinement beyond the plan.** The bundled `fields` `$def` was typed
> `array`-only, but the tool's documented contract (and `_validate_fields_arg`)
> also accepts a comma-separated **string**. Strict validation would have
> rejected that string form, so the `$def` was widened to
> `type: ["array", "string"]` — validation stays accurate *and* the string
> form the executor has always supported still works. `items` constrains only
> the array form; bounds (`page_size`/`offset`) are integer-only by design so
> string numeric args are now rejected with a clean validation error (the
> pagination tests were updated to the integer contract).

### 7 (High) — session hijack via `authenticate` rebind
Both transports now **always** create a fresh session for the session tool,
ignoring any caller-supplied `session_id` for that call. The caller gets the
new `session_id` back; the old id keeps working for its original owner. Belt-and-braces
in `_authenticate`: an already-authenticated session is refused rather than
rebound.
*Files:* `src/mcp_siebel/http_server.py`, `src/mcp_siebel/stdio.py`,
`src/mcp_siebel/handlers/auth.py`.
*Tests:* `tests/test_session_rebind.py` (transport always-create, no rebind;
rebind guard).

### 8 (High) — TLS verification disabled
`AppSettings` gains `verify_tls` (default `true`, env `SIEBEL_VERIFY_TLS`) and
`ca_bundle` (env `SIEBEL_CA_BUNDLE`). `SiebelClient` resolves `verify` to the
CA bundle path when set, else the `verify_tls` flag, and passes it to `httpx`
(replacing the hardcoded `verify=False` on every outbound call). `_sso_client`
forwards both. Against a self-signed dev Siebel, set `SIEBEL_CA_BUNDLE` (or
`SIEBEL_VERIFY_TLS=false` for dev only).
*Files:* `src/mcp_siebel/config.py`, `src/mcp_siebel/siebel_client.py`,
`src/mcp_siebel/handlers/auth.py`.
*Tests:* `tests/test_siebel_client_tls.py` (`_verify` default true / false / bundle;
httpx receives the right `verify`).

### 9 (Medium) — `bc_upsert` ignored `scope` for updates
`operation: "update"` with `scope: "child"` is now rejected at load (the update
path ignores scope and would treat a child row as a root row). Implementing
child-update is out of scope.
*Files:* `src/mcp_siebel/executors/upsert.py`.
*Tests:* `tests/test_bc_upsert_delete_executor.py` (`test_upsert_update_child_rejected`).

### 10 (Medium) — `bc_upsert` accepted any upsert-family renderer
The `upsert` render family is split into `create` / `update` / `child_create`,
and the accepted family is computed from operation/scope: root create → `create`,
child create → `child_create`, update → `update`. A create tool can no longer
render an update (or vice versa). The schema's render enum stays permissive; the
loader tightens.
*Files:* `src/mcp_siebel/renderers.py`, `src/mcp_siebel/executors/upsert.py`.
*Tests:* `tests/test_bc_upsert_delete_executor.py` (create-with-update-render,
update-with-create-render, child-create-with-root-render).

### 11 (Medium) — `_deref` silently discarded sibling keys
`_deref` now raises `_SpecError` when a `$ref` carries sibling keys (previously
it silently dropped them). The bundled spec has none, so this is non-breaking.
*Files:* `src/mcp_siebel/tools.py`.
*Tests:* `tests/test_deref.py` (`test_deref_rejects_ref_with_sibling_keys`).

### 12 (Medium) — deep spec escaped `_SpecError`
`_deref` caps `$ref` chain depth at 100 so a deep non-cyclic chain surfaces as
`_SpecError` ("too deep") instead of an uncaught `RecursionError`. The
`_deref` and `_load_valid_tools` catches broaden to
`except (_SpecError, RecursionError)`, keeping the keep-last-valid-on-reload /
clear-on-first-load contract.
*Files:* `src/mcp_siebel/tools.py`.
*Tests:* `tests/test_deref.py` (2000-deep chain → `_SpecError`; reload with a
deep chain keeps the last valid set).

### 13 (Low) — `_KindSpec` option lists were dead
`_load_valid_tools` now enforces each kind's `allowed_options`/`required_options`
once, centrally, before dispatching to the per-kind validator. The duplicate
extra/missing blocks are removed from all six executors and from
`_validate_python`; the per-executor `_X_ALLOWED_OPTIONS`/`_X_REQUIRED_OPTIONS`
constants stay as the single source populating `_KindSpec`.
*Files:* `src/mcp_siebel/tools.py`, `src/mcp_siebel/executors/*.py`.
*Tests:* `tests/test_tool_specs.py` (`test_registry_enforces_kind_options`); the
existing per-kind unknown/missing tests now pass via the registry.

## Verification

1. `uv run ruff check src tests` — clean.
2. `uv run mypy src` — clean.
3. `uv run python -m pytest -q` → **377 passed**.
4. `jsonschema` is now an explicit runtime dep (verified via
   `importlib.metadata.requires('mcp-siebel')`).
5. Bundled spec: 26 entries, 19 `enabled: true` / 7 `enabled: false`, none
   missing; `additionalProperties: false` on every top-level `input_schema`;
   `page_size` min 1 / max 100; `offset` min 0.

Live-Siebel smoke (steps 4–7 in the plan's verification) was not run; the
equivalent paths are covered by the tests above.