# Enlisting mcp-siebel in MCP clients

mcp-siebel ships two transports, both from the same package. The package is not
yet on PyPI — install it from Git (see the README's [Install](../README.md#install)
section):

```bash
# stdio only
pip install git+https://github.com/People-at-Work-Systems/mcp-siebel.git
# stdio + HTTP/SSE
pip install "git+https://github.com/People-at-Work-Systems/mcp-siebel.git#egg=mcp-siebel[http]"
```

| Transport | Console script | Install extra | Typical clients |
|---|---|---|---|
| **stdio** | `mcp-siebel` | base install | Claude Desktop, Claude Code, Cursor, VS Code + Copilot, Cline, Zed |
| **HTTP/SSE** | `mcp-siebel-http` | `[http]` | Clients that connect to a URL (ChatGPT Connectors; Claude Desktop via `mcp-remote`) |

The stdio server is the default and needs no extra deps. The HTTP/SSE server
(`mcp-siebel-http`) listens on `0.0.0.0:8201` with the MCP SSE stream at
`GET /sse` (override the port in `_http_entry.py` or your process manager).

> **Read-only by default.** The bundled spec sets `enabled: false` on the 7
> write/delete/external tools. To turn one on, flip `enabled: true` on its entry
> in an override spec pointed at by `SIEBEL_TOOLS_SPEC_PATH` — no env-var gate.
> See [tool-specs.md](tool-specs.md#per-tool-enabled-flag).

## Required / common env vars

| Var | Required | Purpose |
|---|---|---|
| `SIEBEL_BASE_URL` | **yes** | Siebel REST endpoint, e.g. `https://siebel.example.com/siebel/v1.0` |
| `SIEBEL_MCP_MODE` | no | `standalone` (default — the server authenticates each user) or `gateway` (SSO header proxy) |
| `SIEBEL_TOOLS_SPEC_PATH` | no | Override the bundled tool spec (see [tool-specs.md](tool-specs.md)) |
| `SIEBEL_TOOLS_SPEC_WATCH` | no | `true` (default) — hot-reload spec edits; `false` to rely on the lazy mtime check |
| `MCP_API_TOKEN` | no | Bearer-token guard on the HTTP/SSE server (unset = no auth). `SIEBEL_API_TOKEN` is an alias for the same guard. |
| `SIEBEL_VERIFY_TLS` | no | `true` (default) — verify the Siebel server certificate. Against a self-signed dev Siebel, set `SIEBEL_CA_BUNDLE` to trust it, or `false` to disable verification (dev only). |

> These are the client-setup essentials. See [README.md § Configuration](../README.md#configuration)
> for the complete env-var reference (connection, Siebel tuning, session lifetime,
> demo, system prompt, secrets, …).

`command` can be any of:
- the venv binary directly: `/path/to/mcp-siebel/.venv/bin/mcp-siebel`
- `uv run mcp-siebel` (from a checkout)
- `mcp-siebel` (if installed and on `PATH`)

The snippets below use the venv-binary form. Swap in whichever fits your install.

---

## stdio entries (local server, launched by the client)

### Claude Desktop

File: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS);
`%APPDATA%\Claude\claude_desktop_config.json` (Windows).

```jsonc
{
  "mcpServers": {
    "siebel": {
      "command": "/Users/you/Development/mcp-siebel/.venv/bin/mcp-siebel",
      "env": {
        "SIEBEL_BASE_URL": "https://siebel.example.com/siebel/v1.0",
        "SIEBEL_MCP_MODE": "standalone",
        "SIEBEL_TOOLS_SPEC_PATH": "/Users/you/mcp-siebel-test/tool_specs.json",
        "SIEBEL_TOOLS_SPEC_WATCH": "true"
      }
    }
  }
}
```

### Claude Code

Project-scoped file: `.mcp.json` in the repo root (committed, shared with the
team). User-scoped: `~/.claude.json`. Or use the CLI:

```bash
claude mcp add siebel -- /Users/you/Development/mcp-siebel/.venv/bin/mcp-siebel
# then set env vars via `claude mcp add` prompts, or edit .mcp.json
```

`.mcp.json`:

```jsonc
{
  "mcpServers": {
    "siebel": {
      "type": "stdio",
      "command": "/Users/you/Development/mcp-siebel/.venv/bin/mcp-siebel",
      "env": {
        "SIEBEL_BASE_URL": "https://siebel.example.com/siebel/v1.0",
        "SIEBEL_MCP_MODE": "standalone",
        "SIEBEL_TOOLS_SPEC_PATH": "/Users/you/mcp-siebel-test/tool_specs.json",
        "SIEBEL_TOOLS_SPEC_WATCH": "true"
      }
    }
  }
}
```

### Cursor

File: `~/.cursor/mcp.json` (user) or `.cursor/mcp.json` (project).

```jsonc
{
  "mcpServers": {
    "siebel": {
      "command": "/Users/you/Development/mcp-siebel/.venv/bin/mcp-siebel",
      "env": {
        "SIEBEL_BASE_URL": "https://siebel.example.com/siebel/v1.0",
        "SIEBEL_MCP_MODE": "standalone",
        "SIEBEL_TOOLS_SPEC_PATH": "/Users/you/mcp-siebel-test/tool_specs.json",
        "SIEBEL_TOOLS_SPEC_WATCH": "true"
      }
    }
  }
}
```

### VS Code + GitHub Copilot

File: `.vscode/mcp.json` in the workspace (committed). VS Code's native MCP
support (also consumed by Copilot Chat) uses a `servers` map with an explicit
`type`:

```jsonc
{
  "servers": {
    "siebel": {
      "type": "stdio",
      "command": "/Users/you/Development/mcp-siebel/.venv/bin/mcp-siebel",
      "env": {
        "SIEBEL_BASE_URL": "https://siebel.example.com/siebel/v1.0",
        "SIEBEL_MCP_MODE": "standalone",
        "SIEBEL_TOOLS_SPEC_PATH": "/Users/you/mcp-siebel-test/tool_specs.json",
        "SIEBEL_TOOLS_SPEC_WATCH": "true"
      }
    }
  }
}
```

### Cline (VS Code extension)

File: `~/.vscode/extensions/saoudrizwan.claude-dev-*/cline_mcp_settings.json`,
or open it via Cline's "MCP Servers" panel. Same `mcpServers` shape as Claude
Desktop.

```jsonc
{
  "mcpServers": {
    "siebel": {
      "command": "/Users/you/Development/mcp-siebel/.venv/bin/mcp-siebel",
      "env": {
        "SIEBEL_BASE_URL": "https://siebel.example.com/siebel/v1.0",
        "SIEBEL_MCP_MODE": "standalone",
        "SIEBEL_TOOLS_SPEC_PATH": "/Users/you/mcp-siebel-test/tool_specs.json",
        "SIEBEL_TOOLS_SPEC_WATCH": "true"
      }
    }
  }
}
```

### Zed

File: `~/.config/zed/settings.json` under `context_servers`:

```jsonc
{
  "context_servers": {
    "siebel": {
      "command": "/Users/you/Development/mcp-siebel/.venv/bin/mcp-siebel",
      "env": {
        "SIEBEL_BASE_URL": "https://siebel.example.com/siebel/v1.0",
        "SIEBEL_MCP_MODE": "standalone",
        "SIEBEL_TOOLS_SPEC_PATH": "/Users/you/mcp-siebel-test/tool_specs.json",
        "SIEBEL_TOOLS_SPEC_WATCH": "true"
      }
    }
  }
}
```

---

## Remote HTTP/SSE entry (server runs as a process, client connects to a URL)

Run the server (e.g. on `localhost:8201`):

```bash
pip install "git+https://github.com/People-at-Work-Systems/mcp-siebel.git#egg=mcp-siebel[http]"
mcp-siebel-http   # listens on 0.0.0.0:8201; MCP SSE stream at /sse
# Optionally guard it:  MCP_API_TOKEN=secret mcp-siebel-http
```

### Claude Desktop (via `mcp-remote` bridge)

Claude Desktop speaks stdio, so bridge the remote SSE endpoint with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```jsonc
{
  "mcpServers": {
    "siebel-remote": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8201/sse"],
      "env": {
        "MCP_API_TOKEN": "secret"
      }
    }
  }
}
```

### ChatGPT (Connectors)

ChatGPT's custom-connector support points at a **remote** MCP server over HTTP.
Run `mcp-siebel-http` at a reachable URL (e.g. `https://siebel-mcp.your.host`,
terminated by a reverse proxy that forwards to `:8201`), then add a connector
in ChatGPT → **Settings → Connectors** pointing at:

```
https://siebel-mcp.your.host/sse
```

Use the bearer token (`MCP_API_TOKEN`) if you set one. ChatGPT local-stdio MCP
support is environment-dependent and evolving; for a stable setup prefer the
remote SSE form above. (See OpenAI's current Connectors docs for the exact UI
steps, which change between releases.)

---

## After enlisting

- **Claude Desktop / Cursor / Zed:** restart the app so it spawns the server.
- **VS Code:** reload the window; the server appears under Copilot Chat's tool
  list.
- **Verify the tool set** from the server itself: `list_tools` (stdio) or
  `GET http://localhost:8201/tools` (HTTP). With the bundled spec you should see
  19 tools (the 7 write/delete/external tools are `enabled: false`).
- **Spec edits hot-reload** with no restart while the server is running
  (`SIEBEL_TOOLS_SPEC_WATCH=true`, default) — see
  [Hot-reload](tool-specs.md#hot-reload). A handler-module code change still
  needs a restart; only the JSON spec is hot.