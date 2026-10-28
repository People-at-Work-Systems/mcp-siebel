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
| `SessionStore` externalization (Redis/shared backend) | ○ | ● | | |
| Cross-replica `tools_changed` fan-out (Redis pub/sub) | | ● | | |
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
- **`SessionStore` is in-memory** (`tools.py:146` — `self._sessions` is a plain
  dict), so it is the one piece of truly global state that blocks horizontal
  scaling: a restart drops every session, and two replicas behind a load
  balancer are not interchangeable (a `session_id` may hit the replica that
  doesn't hold it → forced re-auth). Make it a **pluggable interface with an
  in-memory default and a Redis/shared backend** — design the interface in
  0.2.2 (while building per-tenant sessions), ship the Redis backend in 0.3.0
  with the container image and `gateway` mode. Cheap specifically in SSO mode:
  the per-user state is just `session_id → username` (the `SiebelClient`
  re-sends the SSO header every call; no live Siebel session token to keep
  warm), so the store is a small mapping, not a warm session pool. Because that
  mapping is a capability — whoever controls it controls which Siebel identity
  the server asserts via the SSO header — the externalized store has hard
  requirements: **high-entropy unguessable `session_id`s**; **TLS to the store**;
  **store-side ACL + encryption at rest**; **network isolation** (the store is
  not reachable outside mcp-net/private); **tenant-scoped lookups** (validate
  `tenant_id` on read, not just write — a tenant A `session_id` must never
  resolve in tenant B's namespace); and **no Siebel credentials or SSO secrets
  in the store** (SSO mode holds none; the store keeps only
  `session_id → (tenant_id, username)`). Evicting a `session_id` in the store
  *is* the revocation mechanism.
- **`schema_cache`** — BC descriptors differ per Siebel, so the cache must be
  **at least per-tenant**. But the cache is **per-session (per-user) today**
  (`auth.py:76`), populated from Siebel *as that user*, and that per-user grain
  must be preserved unless sharing is proven safe. Sharing one cache across
  users *within* a tenant is an optimization that is only safe if
  `/data/{bo}/{bc}/describe` returns an identical field set across all
  responsibilities and field-level-visibility profiles in that tenant. For
  banking/government deployments the **default is per-user (per-session)
  caching**; per-tenant sharing is an explicit, per-tenant opt-in that requires
  evidence the describe response is responsibility-invariant. The cache must
  never be shared across tenants.
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

### Security posture (banking/government bar)

The multi-tenancy and session-externalization work **adds routing and isolation;
it must not re-implement or broaden authorization.** Per-user data visibility —
objects, fields, rows — stays enforced by **Siebel on every call**, via the
per-user SSO header, exactly as today. The server never decides what a user may
see; it only decides *which tenant's Siebel* and *which user's header* to send,
and both are pinned to the caller.

- **Enforcement boundary unchanged.** Each tool call still sends the caller's
  SSO header to that tenant's Siebel; Siebel applies responsibilities,
  field-level security, and sharing rules. No authz logic moves into the MCP
  server, and no path widens what a user can reach.
- **No cross-tenant or cross-user leakage.** Tenant is resolved from the API key
  and pinned at the SSE handshake; sessions are namespaced by tenant and
  validated on every call; a tenant A request can never resolve to tenant B's
  client or to another user's header. Within a tenant, each user still
  `authenticate`s as themselves and gets their own `SiebelClient` + `SchemaCache`.
- **Metadata is treated as potentially sensitive.** `schema_cache` defaults to
  per-user (see above); per-tenant sharing is opt-in only with evidence that
  `describe` is responsibility-invariant. Field *names/types/existence* can be
  protected data in regulated deployments.
- **The externalized session store is a capability store** (see the Session store
  bullet): TLS, ACL, encryption at rest, network isolation, tenant-scoped reads,
  no Siebel secrets, eviction = revocation.
- **Audit trail.** Every call is attributable to `(tenant_id, username,
  session_id)` via the existing correlation middleware; for regulated
  deployments this is the audit record, so tenant + user must appear on every
  log line/span and every tool-invocation trace.

### Composes with later releases

- **`gateway` mode (0.3.0)** is orthogonal: gateway decides *how* you
  authenticate to Siebel (forward an SSO header); multi-tenancy decides *which*
  Siebel. A multi-tenant gateway fronts several Siebels and routes the right
  SSO header per tenant.
- **OAuth/Keycloak (0.3.0)** sits alongside API keys: OAuth for human SSO per
  tenant, API keys for machine clients per tenant, both resolving to the same
  `(tenant_id, caller)` context.
- **`SessionStore` externalization (0.3.0)** is what makes the 0.3.0 cloud
  deployment real: the container image + `gateway` mode only deliver HA and
  rolling deploys once replicas share sessions. The interface is designed in
  0.2.2 so the 0.3.0 backend is a drop-in.

## 0.3.0 — Publishing, cloud deployment & gateway auth

**Theme:** ship the first installable release and make it runnable as a
cloud-hosted, multi-replica, HA service with real enterprise auth. **Redis
becomes the shared-state backbone** that makes horizontal scaling real — the
0.2.2 `SessionStore` interface gets its Redis backend here, plus the
cross-replica fan-out and shared rate-limit counters that multi-replica
correctness requires. It builds on the 0.2.2 multi-tenancy spine and carries
the banking/government security posture forward unchanged.

### Redis as the shared-state backbone

The 0.2.2 `SessionStore` interface ships an in-memory default; 0.3.0 adds the
**Redis backend** and two more Redis-backed shared-state uses that HA needs:

- **Sessions** — `session_id → (tenant_id, username)`. The capability store
  from the 0.2.2 security posture: high-entropy ids, TLS, ACL + encryption at
  rest, network isolation, tenant-scoped keys (`t:{tenant_id}:sess:{session_id}`),
  tenant validated on read, no Siebel/SSO secrets held, TTL + explicit delete =
  revocation. **Per-tenant envelope encryption**: the session payload (the
  `username` — it must be recoverable, since it is sent as the SSO header, so it
  cannot be hashed) is encrypted under a per-tenant data-encryption key wrapped
  by a master key, so a single Redis compromise or a misconfigured operator ACL
  exposes only encrypted ciphertext, not every tenant's SSO identities. The
  per-tenant DEK lives in the secret store, not Redis.
- **Cross-replica `tools_changed` fan-out (pub/sub)** — today the hot-reload
  watcher notifies only clients connected to *this* process (MCP
  `notifications/tools/list_changed`, REST ETag/SSE). With N replicas, a spec
  edit on one replica leaves clients on the others uninformed — an HA
  correctness gap. Redis pub/sub fans the reload event (+ global `content_hash`)
  to every replica so all connected clients get the notification. **The payload
  is global-only**: it carries only the global spec reload signal and the
  global `content_hash` — never any tenant-specific or key-scoped tool
  information (a client re-lists and receives its own scoped view). **Forward
  lock for 0.4.0**: when per-tenant spec overrides arrive, the fan-out must
  become **tenant-scoped** (a per-tenant channel), otherwise one tenant learns
  that another tenant's tool set changed and the hash of its exposed tools — a
  cross-tenant metadata leak. Design the channel namespace now so 0.4.0 is a
  routing change, not a redesign.
- **Per-tenant rate-limit counters** — the 0.2.2 per-key/per-tenant rate limits
  need *shared* counters to be effective across replicas; a per-process counter
  is gameable by request rotation. Redis integer counters keyed by
  `(tenant_id, key_id)` with TTL windows. **Tenant-scoped access only**: every
  read/write is issued under the caller's pinned tenant namespace, and no API or
  code path reads, aggregates, or enumerates another tenant's counters — another
  tenant's request rate / traffic volume is a metadata leak (traffic analysis),
  not just an integer.

An **optional shared `schema_cache` backend** is also Redis-backed, but only for
tenants where `/data/{bo}/{bc}/describe` is proven responsibility/FLS-invariant
(the 0.2.2 opt-in). It collapses redundant describe calls across users and
replicas to one per tenant. **Default off** — per-user (per-session) caching
remains the safe default for regulated deployments.

### Packaging

- **PyPI publish** — first `v*` tag fires the release workflow; `pip install
  mcp-siebel[http]` replaces the git-install instructions.
- **Published container image** — versioned OCI image for the HTTP/SSE
  transport.
- **Redis client is a new extra** (e.g. `[redis]` or `[ha]`), gated like
  `[http]` — the base stdio install must not pull it. The packaging invariant
  (`test_packaging.py`: base import pulls no gated deps) extends to the Redis
  client; the stdio path imports it lazily, same as the `[http]` deps.

### Auth modes

- **`gateway` SSO-proxy mode** — finish the mode so the server accepts a
  gateway-resolved identity (SSO header / JWT-mapped username) instead of
  username+password `authenticate`. The connector stays IdP-agnostic; OAuth/JWKS
  lives upstream in the gateway. This is the seam the 0.2.2 multi-tenancy rides.
- **OAuth 2.0 / Keycloak token exchange** — re-introduce what the 0.2.1 cleanup
  removed (the dead stub), this time real: OAuth for human SSO per tenant, API
  keys (from 0.2.2) for machine clients per tenant, both resolving to the same
  `(tenant_id, caller)` context the Redis session store indexes.

### Operability

- **`/ready` vs `/health` split** — `/health` = process up; `/ready` = Siebel
  reachable (lightweight probe-BO/BC query) **and** Redis reachable when the
  Redis backend is active, for load balancers.
- **CLI introspection** — `mcp-siebel --list-tools` / `--check-config` to dump
  the resolved tool set and validate env/spec/tenant config without starting a
  server.

### HA & security notes (carry the 0.2.2 posture forward)

- Redis is on a private network (mcp-net/data-net), not exposed; TLS + ACL +
  encryption at rest; least-privilege Redis user per backend use.
- **Redis-unreachable failure mode is fail-closed.** In HA mode a request
  depends on Redis for session lookup; if Redis is unreachable, calls requiring
  a session are **rejected** (not served, not degraded) and `/ready` goes
  unhealthy so the load balancer pulls the replica. There is **no silent
  fallback to in-memory sessions** in a multi-replica deployment — such a
  fallback would stop revocation from propagating and let sessions live
  per-replica, a security regression. The in-memory `SessionStore`/`SchemaCache`
  exist only as the **single-replica/stdio** backend, not as an HA fallback.
- **Isolation is logical, not physical.** A shared Redis with key namespacing +
  ACL + per-tenant envelope encryption is the standard multi-tenant SaaS bar and
  acceptable for banking/government deployments **whose threat model treats the
  Redis/operator boundary as trusted-but-hardened** (TLS, ACL, encryption at
  rest, per-tenant DEKs). It is a single shared trust domain. If the requirement
  instead mandates **per-tenant physical or cryptographic isolation of the
  session store**, shared Redis does not meet it — the deployment must use
  per-tenant Redis instances (physical) or rely on the per-tenant envelope
  encryption above as the cryptographic equivalent. State this assumption per
  deployment; do not leave it implicit.
- Rolling deploys no longer drop sessions (the Redis store survives); a replica
  restart rehydrates from Redis.
- Audit: `(tenant_id, username, session_id)` on every log line/span and
  tool-invocation trace, unchanged from 0.2.2; Redis holds the mapping the audit
  trail refers to.