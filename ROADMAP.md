# Roadmap

A living planning document for mcp-siebel. It is **not a commitment** — the
project is a work in progress and ships breaking changes without notice (see
the README disclaimer). Use it to coordinate direction; track what actually
landed in [`CHANGELOG.md`](CHANGELOG.md).

Legend for the matrix: **●** slotted for that release · **○** foundation /
partial work lands there · **–** not planned · blank = not yet slotted.

## Feature × release matrix

| Feature | 0.2.2 | 0.3.0 | 0.4.0 | Future |
|---|:--:|:--:|:--:|:--:|
| **Multi-tenancy & API keys** | | | | |
| API keys (replace shared `MCP_API_TOKEN`) | ● | | | |
| Key → tenant resolution + SSE pinning | ● | | | |
| Per-key scopes / rate limits / audit | ○ | ● | | |
| Per-tenant Siebel connection + pool | ● | | | |
| Per-tenant session store (namespaced) | ● | | | |
| Per-tenant `schema_cache` | ● | | | |
| Per-tenant tool-spec `enabled` overrides | | | ● | |
| Per-tenant system prompt | | | ● | |
| Per-tenant quotas / metrics / lifecycle | | | ○ | ● |
| **Publishing & deployment** | | | | |
| PyPI publish (first `v*` tag → workflow) | | ● | | |
| Published container image | | ● | | |
| **Auth modes** | | | | |
| `gateway` SSO-proxy mode | | ● | | |
| OAuth 2.0 / Keycloak token exchange | | ● | | |
| **MCP surface** | | | | |
| MCP `resources` (repository metadata) | | | ● | |
| MCP `prompts` (reusable templates) | | | ● | |
| **Tool-set gaps** | | | | |
| Attachment download | | | ● | |
| Repository discovery (REST-enabled BO/BC) | | | ● | |
| Batch / multi-BC tool | | | | ● |
| "Scroll all" cursor helper | | | | ● |
| **Operability** | | | | |
| `/ready` vs `/health` split | | ● | | |
| CLI introspection (`--list-tools`, `--check-config`) | | ● | | |
| OpenTelemetry tracing | | | ● | |

## 0.2.2 — Multi-tenancy & API keys

**Theme:** turn the server from "one shared token, one shared Siebel, standalone
only" into "many keys, many tenants, each isolated to its own Siebel." This is
the first release that can front several Siebel deployments from one cloud-hosted
instance.

Multi-tenancy here means **one mcp-siebel process serving several Siebel
backends**, one per tenant. The work is threading a `tenant_id` through
everything that is currently global, and isolating per-tenant state. It builds
directly on the 0.2.1 security hardening (token guard, session-hijack fix,
bind-wins) and reuses the 0.2.0 hot-reload machinery.

### Two identity layers (keep them apart)

1. **Client → MCP server** — "are you allowed to talk to this server?" Today the
   single shared `MCP_API_TOKEN`. Becomes **per-tenant API keys**.
2. **MCP server → Siebel** — "whose Siebel session is this?" Today per-user via
   the `authenticate` tool returning a `session_id`. **Stays per-user.**

The API key is the front door (layer 1); the Siebel session is the identity
(layer 2). They are orthogonal — a key does not replace the per-user Siebel
session. Each end user still `authenticate`s against *their tenant's* Siebel to
get an isolated session.

### What is global today and must become per-tenant

- **`AppSettings` / connection config** — one `SIEBEL_*` set today. Becomes a
  tenant registry: base defaults + `tenant_id → TenantConfig` overrides, loaded
  from a `SIEBEL_TENANTS_PATH` file, hot-reloaded with the existing watcher.
- **`SiebelClient`** — one global today. Becomes **one per tenant**: own httpx
  client, own base URL, own TLS bundle, own concurrency pool. This is the hard
  isolation requirement — a tenant's calls only ever hit its Siebel.
- **Session store** — per-user today but all against the same Siebel. Must be
  **namespaced by tenant** (`(tenant_id, session_id)`). Cross-tenant
  session_id replay must fail. `authenticate` resolves the tenant from context
  and opens the Siebel session against that tenant's client.
- **`schema_cache`** — BC descriptors differ per Siebel; must be per-tenant.
- **`ToolRegistry` / spec** — one global tool set today. 0.2.2 ships a **shared
  spec** across tenants (scoped enforcement is per-key, not per-tenant-spec yet);
  per-tenant `enabled` overrides follow in 0.4.0.
- **System prompt** — shared in 0.2.2; per-tenant override in 0.4.0.
- **API keys** — each key maps to a tenant; per-key scopes live *inside* that
  tenant.
- **Metrics / logs / rate limits** — Prometheus label by `tenant_id` (bound the
  cardinality), correlation id carries tenant on every line/span, concurrency
  caps per tenant.

### Tenant resolution

Pick **one** mechanism and make it authoritative: **API key → tenant**. The
bearer key resolves to a `tenant_id`. A URL hint (`/t/{tenant}/sse`) or `Host`
may help route the SSE handshake, but the **key is the trust root** — never
trust a tenant id supplied in a message body.

### Transport boundary

- **HTTP/SSE is the multi-tenant surface.** Resolve the tenant from the bearer
  key at `/sse` connect, **pin it on the connection**, and every `messages/`
  post inherits that pinned tenant. Refuse anything that does not match.
- **stdio is single-tenant by nature** (one local process). Bind it to one
  tenant via env, or declare stdio out of scope for multi-tenancy. Do not fake
  it.

### Phasing inside 0.2.2

- **Phase 0 — multi-key, single Siebel (foundation).** API keys with
  key→tenant mapping, per-key scopes, per-key rate limits, audit attribution.
  Reuses the `MCP_API_TOKEN`/`SIEBEL_API_TOKEN` aliasing pattern (keep the
  single token as a legacy key for backward compat). One shared Siebel still.
- **Phase 1 — real multi-tenancy.** Per-tenant `SiebelClient` + connection
  pool, per-tenant session store, tenant resolution at the HTTP boundary,
  per-tenant `schema_cache`. This is the first version that is honestly
  multi-tenant.

Per-tenant tool-spec overrides, per-tenant system prompt, and the full
quotas/metrics/lifecycle story move to **0.4.0** (Phase 2/3 below).

### Invariants (mirror the 0.2.1 hardening style)

- **No cross-tenant session reuse** — namespace sessions by tenant; validate
  tenant on every call.
- **SSE pins tenant at handshake** — never re-read tenant from a message body
  mid-stream.
- **Per-tenant concurrency caps *and* a global cap** — one noisy tenant must not
  starve the server or other tenants.
- **Keep-last-valid on tenant-config reload** — don't drop live sessions when a
  tenant's config changes (same contract as spec reload).
- **Per-tenant secrets** — tenant Siebel credentials / SSO headers must not
  live in one plaintext file; mount per-tenant secrets or use an external store.
- **Keys stored hashed at rest** (SHA-256), compared constant-time.
- **Add a "no cross-tenant access" test** alongside the existing session-hijack
  and framework-isolation tests.

### Composes with later releases

- **`gateway` mode (0.3.0)** is orthogonal: gateway decides *how* you
  authenticate to Siebel (forward an SSO header); multi-tenancy decides *which*
  Siebel. A multi-tenant gateway fronts several Siebels and routes the right
  SSO header per tenant.
- **OAuth/Keycloak (0.3.0)** sits alongside API keys: OAuth for human SSO per
  tenant, API keys for machine clients per tenant, both resolving to the same
  `(tenant_id, caller)` context.