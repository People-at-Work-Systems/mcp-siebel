# Roadmap

A living planning document for mcp-siebel. It is **not a commitment**: the
project is a work in progress and ships breaking changes without notice (see
the README disclaimer). Use it to coordinate direction; track what actually
landed in [`CHANGELOG.md`](CHANGELOG.md).

Legend for the matrix: **●** slotted for that release · **○** partial work
lands there · **–** not planned · blank = not yet slotted.

## Feature × release matrix

| Feature | 0.2.2 | 0.3.0 | 0.4.0 | 0.5.0 | 0.6.0 | Future |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| **Tool-set gaps** | | | | | | |
| Attachment download | ● | | | | | |
| "Scroll all" cursor helper | ● | | | | | |
| Repository discovery (REST-enabled BO/BC) | ● | | | | | |
| Batch / multi-BC tool | | | | | | ● |
| **CLI & operability** | | | | | | |
| `--list-tools` | | ● | | | | |
| `--check-config` | | ● | | | | |
| `--validate-spec <file>` | | ● | | | | |
| `/ready` vs `/health` split | | ● | | | | |
| Structured, documented log format | | ● | | | | |
| **Publishing** | | | | | | |
| PyPI publish (first `v*` tag → workflow) | | ● | | | | |
| Published container image | | ● | | | | |
| **Extensibility** | | | | | | |
| External tool specs | | | ● | | | |
| Embedding guide | | | ● | ○ | | |
| **Identity seams** | | | | | | |
| Pluggable auth hook | | | | ● | | |
| Gateway / SSO header passthrough | | | | ● | | |
| **MCP surface & observability** | | | | | | |
| MCP `resources` | | | | | ● | |
| MCP `prompts` | | | | | ● | |
| OpenTelemetry tracing | | | | | ● | |
| **Host responsibility (out of scope)** | | | | | | |
| Redis `SessionStore` | | | | | | – |
| Per-key scopes and rate limits | | | | | | – |
| Multi-tenancy | | | | | | – |

## 0.2.2 — Tool-set gaps

**Theme:** close the most visible gaps in the bundled tool set.

- **Attachment download** — a counterpart to the existing `upload_attachment`
  (currently disabled). It follows the same fail-closed `enabled` convention.
  It needs a size cap and a decision on returning content versus a reference.
- **"Scroll all" cursor helper** — pagination over `query_data` with an upper
  bound on the number of records fetched.
- **Repository discovery (REST-enabled BO/BC)** — builds on
  `list_business_objects` and `describe_repository`, so clients can find what
  is actually exposed.

## 0.3.0 — CLI, operability & publishing

**Theme:** make the server easy to check, run and install.

- **`mcp-siebel --list-tools`** — prints the resolved tool set with kind and
  enabled state.
- **`mcp-siebel --check-config`** — validates env and spec without starting a
  server. It exits non-zero on failure so it works in CI.
- **`mcp-siebel --validate-spec <file>`** — validates an external spec against
  the schema, which fits the `feat/external-tool-specs` work.
- **`/ready` vs `/health` split** — `/health` means the process is up;
  `/ready` means Siebel is reachable.
- **Structured logs** — the existing correlation id plus a documented log
  format, so a host can ingest them.
- **PyPI publish** — the first `v*` tag fires the release workflow, replacing
  the git-install instructions.
- **Published container image** — an image for the HTTP/SSE transport,
  stateless and single-replica by default.

## 0.4.0 — Extensibility

**Theme:** let a host project adapt mcp-siebel to its own needs without forking
or patching it.

- **External tool specs** — a host supplies its own tool-spec file that is
  layered over the bundled one: it can add tools, override descriptions and
  schemas, and switch tools on or off via `enabled`. The bundled spec stays
  untouched, and the existing validation, fail-closed `enabled` and
  hot-reload rules apply to the combined result.
- **Embedding guide** — documentation for integrating the server into another
  project, covering both ways of running it (mounted in a host app, or as a
  separate sidecar process). It lists which environment variables and spec
  files the host owns, and what the server guarantees.

## 0.5.0 — Identity seams

**Theme:** let a host decide who a caller is, without mcp-siebel growing user
management. Siebel keeps enforcing what each user may see, as it does today.

- **Pluggable auth hook** — one narrow interface: a callable that receives the
  incoming request and returns a caller identity or rejects the request. The
  default stays today's `MCP_API_TOKEN` check. A host such as UnifiedCRM or a
  gateway supplies its own callable. This is the only auth extension point;
  API keys, tenants and OAuth are deliberately left to the host.
- **Gateway / SSO header passthrough** — accept an identity already resolved
  upstream (for example a username set by an SSO gateway) instead of requiring
  username and password through `authenticate`. The server does no IdP or
  token handling itself. It is the first implementation built on the auth hook.
- **Embedding guide update** — add the auth section: how to plug in a hook and
  how to run behind a gateway.

## 0.6.0 — MCP surface & observability

**Theme:** expose more of what the server knows to MCP clients, and make it
observable. Both are additive and optional.

- **MCP `resources`** — expose repository metadata (business objects,
  components, fields) as MCP resources, so clients can read it directly
  instead of calling a tool.
- **MCP `prompts`** — publish reusable prompt templates, starting with the
  bundled Siebel system prompt.
- **OpenTelemetry tracing** — spans for tool calls and Siebel requests, tied
  to the existing correlation id. It ships as an optional extra and is imported
  lazily, so a base install pulls no tracing dependencies. The packaging test
  is extended to cover it.

## Future / on request

- **Batch / multi-BC tool** — query or update several business components in
  one call.

## Out of scope (host responsibility)

These belong to the architecture around the server. They are only revisited if
a host project shows it cannot handle them itself.

- **Redis `SessionStore`** — shared session state for multiple replicas.
- **Per-key scopes and rate limits** — better enforced at the gateway or
  reverse proxy.
- **Multi-tenancy** — one process serving several Siebel backends. Run one
  instance per Siebel instead.
