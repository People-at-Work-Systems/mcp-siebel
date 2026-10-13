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
- *(Optional)* a **Brave Search API key** (`SIEBEL_BRAVE_SEARCH_API_KEY`) if you
  want the `web_search` tool to function.
- *(Optional)* a **bearer token** (`MCP_API_TOKEN`) to guard the HTTP/SSE server.

## Install

```bash
# stdio transport (Claude Desktop) — no fastapi needed
pip install mcp-siebel

# stdio + HTTP/SSE server
pip install mcp-siebel[http]
```

> **Requires `mcp>=2.0,<3`.** The server uses the mcp 2.x lowlevel `Server`
> API (`on_list_tools` / `on_call_tool` constructor kwargs).
>
> Note: the `mcp` SDK itself depends on `uvicorn` and `starlette` (core deps,
> not extras), so a base `pip install mcp-siebel` does pull them transitively.
> The `[http]` extra gates only `fastapi` and `prometheus-fastapi-instrumentator`
> — the deps the HTTP/SSE server adds on top of what `mcp` already brings.

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
| `SIEBEL_ALLOW_WRITE` | `false` | Enable the write tools (create/update/create-child/upload-attachment). Default off — read-only. |
| `SIEBEL_ALLOW_DELETE` | `false` | Enable `delete_record`. Default off — read-only. |
| `SIEBEL_DISABLED_TOOLS` | *(empty)* | Additional comma-separated tool names to disable on top of the two gates |
| `SIEBEL_SYSTEM_PROMPT_PATH` | *(unset → bundled)* | Override the bundled Siebel system prompt |
| `MCP_API_TOKEN` | *(unset → no auth)* | Bearer token guard on the HTTP server |

The bundled system prompt lives at `mcp_siebel/prompts/siebel-system.md`. Point
`SIEBEL_SYSTEM_PROMPT_PATH` at your own file to replace it entirely.

**Read-only by default.** With `SIEBEL_ALLOW_WRITE` and `SIEBEL_ALLOW_DELETE`
both `false` (the default), the write and delete tools are hidden and refused.
Set the relevant flag to `true` to enable them. `SIEBEL_DISABLED_TOOLS` disables
additional individual tools on top. Discover the current tool set from the
server itself (`list_tools` over stdio, or `GET /tools` on the HTTP server).

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