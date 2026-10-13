# mcp-siebel

An [MCP](https://modelcontextprotocol.io/) server for **Oracle Siebel CRM 26.2+**.
Exposes Siebel REST as MCP tools — querying, creating, updating, and deleting
records; LOV and repository metadata; aggregations; attachments; date math; and
web search — over two transports: **stdio** (for Claude Desktop and other stdio
MCP clients) and **HTTP/SSE** (for remote clients).

## Install

```bash
# stdio transport (Claude Desktop) — no fastapi needed
pip install mcp-siebel

# stdio + HTTP/SSE server
pip install mcp-siebel[http]
```

> **Requires `mcp>=1.0,<2`.** The `mcp` 2.x line is not yet supported; the
> package pins `mcp<2` because 2.0 removed the decorator API this server uses.
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
| `SIEBEL_DISABLED_TOOLS` | `delete_record` | Comma-separated tool names to disable |
| `SIEBEL_SYSTEM_PROMPT_PATH` | *(unset → bundled)* | Override the bundled Siebel system prompt |
| `MCP_API_TOKEN` | *(unset → no auth)* | Bearer token guard on the HTTP server |

The bundled system prompt lives at `mcp_siebel/prompts/siebel-system.md`. Point
`SIEBEL_SYSTEM_PROMPT_PATH` at your own file to replace it entirely.

The tool set is `SIEBEL_DISABLED_TOOLS`-driven and can change; discover the
current set from the server itself (`list_tools` over stdio, or `GET /tools` on
the HTTP server) rather than from a static list.

## Development

```bash
pip install -e .[dev]
pytest
ruff check .
mypy .
```

## Origin

This project originated as part of UnifiedCRM, developed by People at Work
Systems Software, and was extracted into a standalone package.

## License

MIT — see [LICENSE](LICENSE).