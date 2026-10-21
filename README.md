# mcp-siebel

An [MCP](https://modelcontextprotocol.io/) server for **Oracle Siebel CRM 26.2+**.
Exposes Siebel REST as MCP tools — querying, creating, updating, and deleting
records; LOV and repository metadata; aggregations; attachments; date math; and
web search — over two transports: **stdio** (for Claude Desktop and other stdio
MCP clients) and **HTTP/SSE** (for remote clients).

## Status & disclaimer

This is a **work in progress**, shared with the community for **educational
purposes**. Expect rough edges, breaking changes without notice, and gaps in
coverage. It is not a supported product.

**Siebel deployment scope.** The server is developed and tested against a
**Siebel vanilla demo database**. Adapting it to a **customized Siebel
deployment** will likely require adaptation work on **both** sides:

- on the **Siebel side** — REST-enabling the business objects/components you
  want to query, granting the connecting identity the right responsibilities,
  and configuring SSO/OAuth;
- on the **mcp-siebel side** — field names, business-component assumptions
  baked into the tools and the bundled system prompt, and the auth-probe
  BO/BC may all need adjustment to match your Siebel.

Do not expect it to work unmodified against a production-customized Siebel.

## Siebel prerequisites

- Oracle Siebel **26.2+** with the **REST API** enabled (SWSE / Siebel REST
  interface).
- Network access from this server to the Siebel REST endpoint
  (`SIEBEL_BASE_URL`, or `SIEBEL_HOST` + `SIEBEL_SCHEME`).
- A Siebel identity: either **SSO header** auth (`SIEBEL_SSO_HEADER`, default
  `SBL_USER`) in standalone mode, or **OAuth/Keycloak**
  (`SIEBEL_OAUTH_CLIENT_ID` / `SIEBEL_OAUTH_CLIENT_SECRET` /
  `SIEBEL_KEYCLOAK_TOKEN_URL`).
- The business objects/components the tools query must be **REST-enabled** and
  accessible to that identity.
- An **auth-probe BO/BC** (`SIEBEL_PROBE_BUSINESS_OBJECT` /
  `SIEBEL_PROBE_BUSINESS_COMPONENT`, default `Account`/`Account`) that is
  REST-enabled **and** not readable by a bogus/guest identity — `authenticate`
  uses it to validate the SSO header. Override per instance if `Account` is
  guest-readable on your Siebel.
- *(Optional)* a **Brave Search API key** (`SIEBEL_BRAVE_SEARCH_API_KEY`).
  `web_search` is `enabled: false` in the bundled spec — flip `enabled: true`
  on its entry in an override spec (`SIEBEL_TOOLS_SPEC_PATH`) to turn it on. It
  sends free text to an outside service, so it stays off by default.
- *(Optional)* a **bearer token** (`MCP_API_TOKEN`) to guard the HTTP/SSE server.

## Install

The package is not yet on PyPI — install from the Git repo. (Once the first
`v*` tag is cut, the release workflow publishes to PyPI and these collapse to
`pip install mcp-siebel` / `pip install mcp-siebel[http]`.)

```bash
# stdio transport (Claude Desktop) — no fastapi needed
pip install git+https://github.com/People-at-Work-Systems/mcp-siebel.git
# or with uv
uv pip install git+https://github.com/People-at-Work-Systems/mcp-siebel.git

# stdio + HTTP/SSE server
pip install "git+https://github.com/People-at-Work-Systems/mcp-siebel.git#egg=mcp-siebel[http]"
uv pip install "mcp-siebel[http] @ git+https://github.com/People-at-Work-Systems/mcp-siebel.git"
```

To get the `mcp-siebel` / `mcp-siebel-http` console scripts on `PATH` as a uv
tool:

```bash
uv tool install mcp-siebel --from git+https://github.com/People-at-Work-Systems/mcp-siebel.git
```

> **Requires `mcp>=2.0,<3`.** The server uses the mcp 2.x lowlevel `Server`
> API (`on_list_tools` / `on_call_tool` constructor kwargs).
>
> Note: the `mcp` SDK itself depends on `uvicorn` and `starlette` (core deps,
> not extras), so a base install does pull them transitively. The `[http]` extra
> gates only `fastapi` and `prometheus-fastapi-instrumentator` — the deps the
> HTTP/SSE server adds on top of what `mcp` already brings.

> Ready-to-paste MCP server entries for Claude Desktop, Claude Code, Cursor,
> VS Code + Copilot, Cline, Zed, and ChatGPT (remote) live in
> [docs/mcp-clients.md](docs/mcp-clients.md).

## Quick start — stdio (Claude Desktop)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "siebel": {
      "command": "mcp-siebel",
      "env": {
        "SIEBEL_BASE_URL": "https://siebel.example.com/siebel/v1.0",
        "SIEBEL_SCHEME": "https"
      }
    }
  }
}
```

The first tool to call is `authenticate` with the user's Siebel username and
password; it returns a `session_id` that every subsequent tool call takes as its
`session_id` argument.

## HTTP/SSE mode

Run the server (requires the `[http]` extra):

```bash
mcp-siebel-http          # uvicorn on 0.0.0.0:8201
```

Point Claude Desktop at it via [`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "siebel": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8201/sse"]
    }
  }
}
```

REST surface: `GET /health`, `GET /tools`, `POST /tools/{name}`,
`POST /sessions`, `GET /sessions/{id}`, `GET /sse`, `POST /messages/`.

## Configuration

All config is environment-variable driven (`SIEBEL_` prefix; `MCP_API_TOKEN`
guards the HTTP server if set). See [`.env.example`](.env.example) for the full
list. The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `SIEBEL_BASE_URL` | *(empty — required)* | Siebel REST endpoint, e.g. `https://siebel.example.com/siebel/v1.0` |
| `SIEBEL_HOST` | *(empty)* | Alternative to `BASE_URL`: build the URL from host + `SIEBEL_SCHEME` |
| `SIEBEL_SCHEME` | `http` | Used only when composing from `SIEBEL_HOST` |
| `SIEBEL_MCP_MODE` | `standalone` | Auth mode |
| `SIEBEL_SSO_HEADER` | `SBL_USER` | SSO header name (standalone mode) |
| `SIEBEL_VERIFY_TLS` | `true` | Verify the Siebel server TLS certificate. Against a self-signed dev Siebel, set `SIEBEL_CA_BUNDLE` to trust it (verification stays on) or set this to `false` (dev only — disables verification). |
| `SIEBEL_CA_BUNDLE` | *(unset → OS roots)* | Path to a CA bundle file used to verify the Siebel certificate. Overrides `SIEBEL_VERIFY_TLS` when set, so a self-signed cert can be trusted with verification still on. |
| `SIEBEL_TOOLS_SPEC_PATH` | *(unset → bundled)* | Override the tool-spec JSON file (see [docs/tool-specs.md](docs/tool-specs.md)). The spec defines the complete tool set — kinds, per-tool `enabled` flag, handler references, `preferred_fields` — not just per-tool description/inputSchema. Hot-reloaded (watcher + mtime fallback); first-load failure aborts startup. |
| `SIEBEL_TOOLS_SPEC_WATCH` | `true` | Start a file watcher that pushes spec changes to the registry as they happen (debounced ~300 ms; atomic temp-file + rename replaces detected). Set `false` to rely on the lazy mtime check on the next `tools/list` / `GET /tools`. Python handler modules are not watched — a code change needs a restart. |
| *(per-tool `enabled`)* | *(mandatory)* | The single on/off switch for a tool, authored in the spec (not an env var). Mandatory on every entry — there is no silent default, so a deployer opts in to each tool explicitly. The bundled spec sets `enabled: false` on the 7 write/delete/external tools — `create_record`, `create_record_json`, `create_child_record`, `update_record`, `upload_attachment`, `delete_record`, `web_search` — and `enabled: true` on the rest, so the server is read-only by default. Flip `enabled: true` on an entry in an override spec (`SIEBEL_TOOLS_SPEC_PATH`) to turn a tool on. |
| `SIEBEL_SYSTEM_PROMPT_PATH` | *(unset → bundled)* | Override the bundled Siebel system prompt |
| `MCP_API_TOKEN` | *(unset → no auth)* | Bearer token guard on the HTTP/SSE server. `SIEBEL_API_TOKEN` is an alias for the same guard. |

The bundled system prompt lives at `mcp_siebel/prompts/siebel-system.md`. Point
`SIEBEL_SYSTEM_PROMPT_PATH` at your own file to replace it entirely.

**Read-only by default.** The bundled spec sets `enabled: false` on its 7
write/delete/external tools — `create_record`, `create_record_json`,
`create_child_record`, `update_record`, `upload_attachment`, `delete_record`,
`web_search` — so they are hidden and refused. To turn one on, flip
`enabled: true` on its entry in an override spec pointed at by
`SIEBEL_TOOLS_SPEC_PATH` — no env var. Discover the current tool set from the
server itself (`list_tools` over stdio, or `GET /tools` on the HTTP server).

> The tool set is defined by a JSON spec file — `mcp_siebel/specs/tool_specs.json`
> by default, overridable with `SIEBEL_TOOLS_SPEC_PATH`. The spec defines the
> complete tool set (kinds, per-tool `enabled` flag, handler references,
> `preferred_fields`), not just per-tool description/inputSchema. A spec edit is
> hot-reloaded: a
> `watchfiles` watcher (`SIEBEL_TOOLS_SPEC_WATCH`, default on) pushes the change
> to connected clients — MCP clients get `notifications/tools/list_changed` (a
> hint; not every client re-lists immediately), and HTTP REST clients get an
> `ETag`/`If-None-Match`→304 on `GET /tools` and a `tools_changed` SSE event on
> `GET /tools/events`. A failed reload sends nothing and keeps the last valid set.
> See [docs/tool-specs.md](docs/tool-specs.md) for the file format, every tool
> kind, the per-tool `enabled` flag, bind/`allowed_fields`/`searchspec_template`,
> how to add a tool (JSON-only or via a `python` handler), and the full
> hot-reload semantics.

## Development

```bash
pip install -e .[dev]
pytest
ruff check .
mypy src/mcp_siebel
```

## Origin

This project originated as part of UnifiedCRM, developed by People at Work
Systems Software, and was extracted into a standalone package.

## License

MIT — see [LICENSE](LICENSE).