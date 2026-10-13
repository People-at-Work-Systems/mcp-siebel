# Design Spec: Publish `mcp-siebel` as a standalone open-source package

- **Date:** 2026-10-13
- **Status:** Approved design (pending user review of this spec)
- **Source (read-only):** the UnifiedCRM monorepo at `/Users/uritter/Development/PaW Development/UnifiedCRM`, service dir `services/mcp-siebel/`.
- **Target (this repo):** `/Users/uritter/Development/PaW Development/mcp-siebel/` — the new standalone GitHub repository `mcp-siebel`, to be published to PyPI as `mcp-siebel`. This spec lives at `docs/specs/` inside it.

> **How to use this spec.** It is written to be executable from a fresh context (no prior conversation). Every source path, file mapping, env var, forbidden string, and test is spelled out. **Path convention:** paths beginning with `/Users/…/UnifiedCRM/` are absolute paths into the source monorepo (read-only); all other paths (e.g. `src/mcp_siebel/…`, `tests/…`) are relative to this repo's root. `cd` to this repo root before running the **Lift Procedure** (§12) and stay there. Do not re-derive decisions recorded under §4 "Locked decisions" — they are settled.

---

## 1. Goal

Extract the `mcp-siebel` MCP connector out of the UnifiedCRM monorepo into a standalone, MIT-licensed, pip-installable Python package distributed via (a) its own GitHub repository and (b) PyPI. The package must be **fully self-contained**: it must not require, import, reference, or imply any other UnifiedCRM service, in code, config, or the bundled Siebel system prompt.

### Success criteria

- `pip install mcp-siebel` works with no extras and runs the stdio MCP server via a `mcp-siebel` console script — no FastAPI/uvicorn installed.
- `pip install mcp-siebel[http]` additionally enables `mcp-siebel-http` (uvicorn HTTP/SSE server).
- `import mcp_siebel` and `import mcp_siebel.stdio` succeed without `[http]` installed and pull in no FastAPI/uvicorn/prometheus. `import mcp_siebel.http_server` without `[http]` raises `ImportError` (fastapi missing); the `mcp-siebel-http` console script catches that and prints a friendly "install `mcp-siebel[http]`" message via the `_http_entry.py` shim.
- A permanent automated test asserts no UnifiedCRM reference (from the §7 forbidden list) exists anywhere in the package source or bundled prompt.
- The public GitHub repo has a LICENSE (MIT), README, pyproject, CI, and a clean secret scan (gitleaks) on the initial commit.
- Tagging `v0.1.0` publishes the package to PyPI via OIDC trusted publishing.

---

## 2. Background — verified facts about the current code

These facts were verified by reading the source on 2026-10-13 and are the basis for the design. Re-verify if the monorepo has changed since.

- `services/mcp-siebel/` contains: `app/` (`__init__.py`, `config.py`, `context.py`, `main.py`, `schema_cache.py`, `siebel_client.py`, `tools.py`), `mcp_stdio.py`, `tests/` (12 test files), `requirements.txt`, `Dockerfile`, `app/CLAUDE.md`.
- **The service is already runtime-self-contained.** Its only imports are third-party (`fastapi`, `uvicorn`, `httpx`, `mcp`, `pydantic`, `pydantic-settings`, `prometheus-fastapi-instrumentator`) plus its own `app.*` modules. It imports **no** UnifiedCRM sibling service. No DB, no Redis, no Qdrant — Siebel sessions live in-process in `SessionStore`.
- **`mcp` is pinned `<2`** in `requirements.txt` because `mcp` 2.0 removed the `@McpServer.list_tools()` decorators used by both `app/main.py` and `mcp_stdio.py`. This pin must carry into the package and be documented.
- **The stdio path is already FastAPI-free.** `mcp_stdio.py` imports only `app.config` and `app.tools`. `app/tools.py` imports only `mcp`, `httpx`, `pydantic`, and its own siblings (`siebel_client`, `schema_cache`). Only `app/main.py` imports FastAPI/uvicorn/prometheus. The `[http]`-extra split therefore requires no logic changes — only moving `main.py` to `http_server.py` with guarded imports.
- **All UnifiedCRM references in the service are non-runtime** (comments, docstrings, one path string, two config defaults). Verified by grep on 2026-10-13; see §7 for the full list and §9 for the scrub.
- `app/context.py` (`CorrelationMiddleware`, `session_id_var`, `turn_id_var`) is a self-contained copy of the cross-service correlation pattern. It imports only stdlib. It is used only by `main.py` (the HTTP server), not by `stdio.py` or `tools.py`. It ships in the base package; only `http_server.py` imports it.

---

## 3. Out of scope

- **No Docker image.** Distribution is PyPI + GitHub repo only. The `Dockerfile` is not copied. (Users who want a container can write their own `Dockerfile` that `pip install mcp-siebel[http]`s.)
- **No live-Siebel CI.** CI runs the existing mocked-HTTP test suite. A live Siebel smoke test stays a manual pre-release step (§10).
- **No auto-versioning / no release-drafter.** Manual semantic-version tags.
- **No `mcp` 2.0 migration.** The `<2` pin is kept. Migrating to `mcp` 2.x is a separate future task.
- **No changes to the UnifiedCRM monorepo.** The monorepo's `services/mcp-siebel/` is left as-is. The lift copies files out; it does not move them.

---

## 4. Locked decisions (from brainstorming, do not re-litigate)

| Decision | Value |
|---|---|
| Distribution targets | PyPI package **and** a standalone GitHub repo. No Docker. |
| License | MIT |
| Package / repo / import name | `mcp-siebel` / `mcp-siebel` / `mcp_siebel` |
| Repo history | Fresh start — one clean initial commit, no `git filter-repo`. README carries an origin note. |
| Packaging shape | Approach A: stdio in the base install, HTTP/SSE behind an `[http]` extra. One PyPI package. |
| Prompt file | Bundled default (minimal standalone rewrite) **plus** `SIEBEL_SYSTEM_PROMPT_PATH` override. |
| Origin note wording | "This project originated as part of UnifiedCRM, developed by People at Work Systems Software, and was extracted into a standalone package." |
| Release mechanism | GitHub Actions on tag `v*`, OIDC trusted publishing to PyPI, no stored tokens. |
| Copyright holder | People at Work Systems Software (confirm exact legal name with user before the initial commit). |

---

## 5. Repo layout & package structure

New repository `mcp-siebel/` using a **src-layout**:

```
mcp-siebel/
├── LICENSE                                  # MIT, copyright People at Work Systems Software
├── README.md
├── CONTRIBUTING.md                          # short: how to run tests, how to release
├── pyproject.toml
├── .gitignore
├── .env.example
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── release.yml
├── src/
│   └── mcp_siebel/                          # renamed from app/
│       ├── __init__.py                      # minimal: __version__ only, NO heavy imports
│       ├── py.typed                         # empty PEP 561 marker
│       ├── config.py                        ← app/config.py  (with scrubbed defaults, §9)
│       ├── context.py                       ← app/context.py (scrubbed docstring, §9)
│       ├── siebel_client.py                 ← app/siebel_client.py
│       ├── schema_cache.py                  ← app/schema_cache.py
│       ├── tools.py                         ← app/tools.py   (scrubbed comments, §9)
│       ├── stdio.py                         ← mcp_stdio.py   (importlib.resources prompt load, §8)
│       ├── http_server.py                   ← app/main.py    (top-level FastAPI imports — [http] only)
│       ├── _http_entry.py                   # NEW — no-FastAPI shim for the mcp-siebel-http console script (§6)
│       └── prompts/
│           └── siebel-system.md             # bundled standalone prompt (§8)
└── tests/
    ├── __init__.py                          ← tests/__init__.py
    ├── test_authenticate_probe.py
    ├── test_create_record_json.py           # scrubbed comment, §9
    ├── test_get_record_childlinks.py
    ├── test_get_record_fallback.py
    ├── test_health.py
    ├── test_metrics.py
    ├── test_query_pagination.py
    ├── test_schema_and_fields.py
    ├── test_session_expiry.py
    ├── test_tool_error_handling.py
    ├── test_packaging.py                    # NEW — FastAPI isolation guard, §10
    └── test_no_unifiedcrm_refs.py           # NEW — zero-coupling guard, §7
```

**Files NOT copied:** `Dockerfile`, `requirements.txt` (superseded by `pyproject.toml`), `app/CLAUDE.md`, `__pycache__/` everywhere, the `app/` directory name itself (renamed to `mcp_siebel/`).

**Structural rule (load-bearing):** `mcp_siebel/__init__.py`, `stdio.py`, `tools.py`, and `_http_entry.py` must never import `fastapi`, `uvicorn`, or `prometheus_fastapi_instrumentator` at module load. `http_server.py` is the **only** module that imports FastAPI, and it does so at top level (exactly as `app/main.py` does today — no route-registration refactor needed). Because neither `__init__.py`, `stdio.py`, nor `tools.py` imports `http_server`, `import mcp_siebel` and `import mcp_siebel.stdio` never pull in FastAPI. Importing `mcp_siebel.http_server` without `[http]` installed raises `ImportError: No module named 'fastapi'` — that is expected and is **not** an isolation violation; nobody imports it on the stdio path. The `mcp-siebel-http` console script points at the `_http_entry.py` shim (§6), which performs the `http_server` import inside a `try/except` so a missing `[http]` extra produces a friendly message instead of a raw traceback. The `test_packaging.py` guard (§10) enforces the stdio-side property in-process on every test run.

---

## 6. Packaging — `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mcp-siebel"
version = "0.1.0"
description = "MCP server for Oracle Siebel CRM 26.2+"
readme = "README.md"
license = "MIT"
requires-python = ">=3.10"
authors = [{ name = "People at Work Systems Software" }]
keywords = ["mcp", "siebel", "oracle", "crm", "model-context-protocol"]
classifiers = [
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Software Development :: Libraries",
]
dependencies = [
    "mcp>=1.0,<2",          # 2.0 removed the @McpServer decorators this server uses — hard pin
    "httpx>=0.28",
    "pydantic>=2",
    "pydantic-settings>=2",
]

[project.optional-dependencies]
http = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "prometheus-fastapi-instrumentator>=0.11",
]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "ruff",
    "mypy",
    "build",
]

[project.scripts]
mcp-siebel      = "mcp_siebel.stdio:main"
mcp-siebel-http = "mcp_siebel._http_entry:main"

[tool.hatch.build.targets.wheel]
packages = ["src/mcp_siebel"]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.mypy]
python_version = "3.10"
```

**Notes:**
- `requires-python = ">=3.10"`: the code uses only `from __future__ import annotations`, builtin generics, dataclasses — all 3.10-safe (verified). CI tests 3.10–3.13.
- Hatchling auto-includes everything under `src/mcp_siebel/`, so the bundled `prompts/siebel-system.md` ships in the wheel without a `force-include` directive. **Verify** after the first `python -m build` that the `.md` is present in the wheel (e.g. `python -c "import importlib.resources as r; print(r.files('mcp_siebel.prompts'))"` against the installed package).
- Both `[project.scripts]` entry points are always installed (pyproject scripts are not extra-gated). `mcp-siebel-http` therefore points at a **shim module** `mcp_siebel/_http_entry.py` that imports `http_server` *inside* a `try/except`, so running `mcp-siebel-http` without `[http]` prints a clear message instead of a raw traceback:
  ```python
  # src/mcp_siebel/_http_entry.py — NO fastapi/uvicorn import at module level.
  def main() -> None:
      try:
          import uvicorn
          from mcp_siebel.http_server import app  # this is what pulls fastapi
      except ImportError as e:
          raise SystemExit(
              "mcp-siebel-http requires the [http] extra. Install it with: pip install mcp-siebel[http]"
          ) from e
      uvicorn.run(app, host="0.0.0.0", port=8201)
  ```
  `http_server.py` itself keeps its top-level FastAPI imports and module-level `app = FastAPI(...)` exactly as `app/main.py` has today — **no route-registration refactor**. The shim is the only thing standing between a base-only install and a confusing traceback.
- `mcp_siebel/stdio.py` gains a `main()` wrapping the existing `asyncio.run(_run())`. The `if __name__ == "__main__":` block can stay as a convenience but is no longer the entry path.
- Drop the `sys.path.insert(0, os.path.dirname(__file__))` line from `stdio.py` — installed packages resolve `mcp_siebel.*` imports without it.

---

## 7. The zero-UnifiedCRM-coupling invariant

The package must not require, import, reference, or imply any other UnifiedCRM service — in **code, config, the bundled prompt, comments, docstrings, or paths**. This is enforced at three layers and by one permanent test.

### 7.1 Forbidden reference list (case-insensitive substring match)

The following strings must not appear anywhere in `src/mcp_siebel/**` (including `prompts/siebel-system.md`), `tests/**`, `README.md`, `CONTRIBUTING.md`, `.env.example`, or `pyproject.toml` — **except** the single allowed origin sentence in `README.md` (§8.4):

- `unifiedcrm`, `unifiedcrm.ai`, `unifiedcrm.com`
- `unifiedai` (the Keycloak realm name — appears in the current `keycloak_token_url` default)
- `agent-router`, `backend-api`, `prompt-manager`, `dashboard-service`, `schema-mapper`, `memory-rag`, `session-service`, `llm-engine`, `mcp-pdf`, `mcp-salesforce`
- `account_cross_ref`, `list_account_cross_refs`, `get_service_requests`, `lookup_account_cross_ref`
- `/api/v1/` (the versioned route prefix is a UnifiedCRM backend-api convention; the standalone HTTP server uses `/health`, `/metrics`, `/tools`, …)
- `prompt-manager/prompts` (the old relative path string)

> **Note on `/api/v1/`:** the current `app/main.py` has `"/api/v1/health"` in `_AccessLogFilter._SILENCED_PATHS`. The scrub (§9) removes that entry — the standalone server silences `/health` and `/metrics` only. After that removal, `/api/v1/` is safe to forbid.

### 7.2 Current occurrences (verified 2026-10-13) and their treatment

All current occurrences are non-runtime text. The scrub (§9) rewrites or removes each:

| File | Line | Content (summary) | Treatment |
|---|---|---|---|
| `app/main.py` | 1, 4, 33 | docstring "UnifiedCRM Siebel MCP service … UnifiedCRM backend … backend-api and agent-router" | Rewrite docstring to describe the package generically. |
| `app/main.py` | 60 | `"/api/v1/health"` in `_SILENCED_PATHS` | Remove that entry; keep `/health`, `/metrics`. |
| `app/tools.py` | 556, 567, 769, 963 | comments mentioning `dashboard-service /crm/objects` etc. | Rewrite comments to describe the JSON-returning tool variants generically (no consumer named). |
| `app/context.py` | 4, 5, 12 | docstring mentioning `backend-api`, `agent-router` | Rewrite docstring to describe the correlation-ID pattern generically. |
| `app/config.py` | 58 | comment "above backend-api's 25-min Redis session-cache TTL" | Rewrite comment to describe the TTL rationale generically (no consumer named). |
| `app/config.py` | 21, 75–76 | defaults `base_url="http://siebel:8090/siebel/v1.0"` and `keycloak_token_url="http://keycloak:8080/realms/unifiedai/..."` | **Code change**, see §9.2. |
| `app/config.py` | 85 | `_compose_url` validator compares against the `http://siebel:8090/...` sentinel | **Code change**, see §9.2. |
| `tests/test_create_record_json.py` | 3 | comment mentioning `dashboard-service` | Rewrite comment generically. |
| `mcp_stdio.py` | 56 | `"../../services/prompt-manager/prompts/siebel-system.md"` path | Replaced by `importlib.resources` load (§8). |

### 7.3 Permanent guard — `tests/test_no_unifiedcrm_refs.py`

```python
"""The package must not reference any UnifiedCRM sibling service, in code or prompt."""
from __future__ import annotations

import importlib.resources
import re
import sys

FORBIDDEN = [
    "unifiedcrm", "unifiedcrm.ai", "unifiedcrm.com", "unifiedai",
    "agent-router", "backend-api", "prompt-manager", "dashboard-service",
    "schema-mapper", "memory-rag", "session-service", "llm-engine",
    "mcp-pdf", "mcp-salesforce",
    "account_cross_ref", "list_account_cross_refs",
    "get_service_requests", "lookup_account_cross_ref",
    "/api/v1/", "prompt-manager/prompts",
]
# Scan .py and .md inside the installed package (incl. the bundled prompt).
_SCAN_EXT = (".py", ".md")
# The README origin sentence is the sole allowed mention; tests scan only the
# package, not the repo root, so README is out of scope here by construction.

def _walk(root: importlib.resources.abc.Traversable):
    for child in root.iterdir():
        if child.is_dir():
            yield from _walk(child)
        elif child.name.endswith(_SCAN_EXT):
            yield child

def test_no_unifiedcrm_references_in_package_or_prompt() -> None:
    pkg = importlib.resources.files("mcp_siebel")
    offenders: list[str] = []
    for f in _walk(pkg):
        text = f.read_text(encoding="utf-8").lower()
        for bad in FORBIDDEN:
            if bad in text:
                offenders.append(f"{f.name}: contains {bad!r}")
    assert not offenders, "UnifiedCRM references found in package:\n" + "\n".join(offenders)
```

This runs on every `pytest` invocation and in CI. If a future edit reintroduces a reference, it fails locally before CI.

---

## 8. The bundled Siebel system prompt

### 8.1 Storage and loading

- The prompt ships at `src/mcp_siebel/prompts/siebel-system.md` (package data, auto-included by hatchling).
- `mcp_siebel/stdio.py` loads it via `importlib.resources`:
  ```python
  def _load_instructions() -> str:
      path = os.environ.get("SIEBEL_SYSTEM_PROMPT_PATH")
      if path:
          text = open(path, encoding="utf-8").read()
      else:
          from importlib.resources import files
          text = (files("mcp_siebel") / "prompts" / "siebel-system.md").read_text(encoding="utf-8")
      # Keep the existing auth-section rewrite for the standalone session-id flow.
      text = text.replace(
          "## Authentication\n\nThe user is already authenticated. Never ask for credentials. "
          "If someone types a username/password, ignore it and answer their underlying question.",
          _AUTH_INSTRUCTIONS.strip(),
      )
      return text
  ```
  `_AUTH_INSTRUCTIONS` (the session-id auth flow text) is already UnifiedCRM-free — keep it as-is.
- The `os.path.normpath(os.path.join(os.path.dirname(__file__), "../../services/prompt-manager/..."))` logic and the `except OSError` fallback are removed. The fallback, if desired, becomes: if `SIEBEL_SYSTEM_PROMPT_PATH` is set but unreadable, raise; otherwise the bundled default always exists.

### 8.2 The standalone prompt content

The monorepo's `services/prompt-manager/prompts/siebel-system.md` is **agent-router's composed Siebel prompt** — it carries dashboard-catalog and service-request/cross-reference blocks specific to UnifiedCRM's orchestration. It is **not** copied. A new, minimal standalone prompt is authored in its place. It must:

- Describe **only**: the Siebel tools, the `session_id` auth flow (the `## Authentication` section that `stdio.py` rewrites at load time — keep that exact anchor string so the replace still works), LOV/pagination conventions, and the `childlinks=none` default.
- Contain **none** of the §7.1 forbidden strings.
- Not mention "the system", "the platform", dashboards, service requests, cross-references, or any orchestrator.
- Keep the `## Authentication\n\nThe user is already authenticated.…` anchor verbatim (it is the target of the runtime replace; even though it gets replaced, keeping it keeps the file self-describing and the replace deterministic).

### 8.3 `SIEBEL_SYSTEM_PROMPT_PATH` is a new env var

Added to the env-var table (§9.3) and README. Empty/unset ⇒ bundled default.

### 8.4 README origin note (the sole allowed UnifiedCRM mention)

In `README.md`, a short "Acknowledgements" or "Origin" section contains exactly:

> This project originated as part of UnifiedCRM, developed by People at Work Systems Software, and was extracted into a standalone package.

This sentence is the only place `UnifiedCRM` / `People at Work Systems Software` appears. The `test_no_unifiedcrm_refs.py` guard scans only the installed package (`src/mcp_siebel/`), not the repo root `README.md`, so this note is allowed by construction.

---

## 9. Scrub & docs

### 9.1 Comment/docstring scrub

Rewrite or remove every line listed in §7.2 so no §7.1 forbidden string remains. The replacements describe behavior generically (e.g. "the JSON variant returns the new record's Id so programmatic callers can read it" — no consumer named).

### 9.2 Config defaults — code change in `config.py`

Two defaults are UnifiedCRM-specific and must change. This is a real code edit, not a comment scrub:

1. **`base_url`** — change default from `"http://siebel:8090/siebel/v1.0"` to `""` (empty). The user must set `SIEBEL_BASE_URL` or `SIEBEL_HOST`.
2. **`_compose_url` validator** — currently checks `if self.base_url != "http://siebel:8090/siebel/v1.0" or not self.host`. Rewrite to compose from host when `base_url` is empty **and** host is set:
   ```python
   @model_validator(mode="after")
   def _compose_url(self) -> "AppSettings":
       if self.base_url or not self.host:
           return self
       host = self.host.rstrip("/")
       if "://" in host:
           url = host if host.endswith("/siebel/v1.0") else f"{host}/siebel/v1.0"
       else:
           suffix = "" if host.endswith("/siebel/v1.0") else "/siebel/v1.0"
           url = f"{self.scheme}://{host}{suffix}"
       self.base_url = url
       return self
   ```
3. **`keycloak_token_url`** — change default from `"http://keycloak:8080/realms/unifiedai/protocol/openid-connect/token"` to `""` (empty). Required only when `SIEBEL_OAUTH_CLIENT_ID`/`SIEBEL_OAUTH_CLIENT_SECRET` are set.

After these changes, re-run the existing `tests/` to confirm no test depended on the old sentinel default. (If a test does, update it to set `base_url` explicitly — do **not** restore the compose-internal default.)

### 9.3 `.env.example`

A new file at the repo root with only the Siebel env vars, all placeholder values:

```sh
# Siebel connection (required)
SIEBEL_BASE_URL=https://siebel.example.com/siebel/v1.0
# SIEBEL_HOST=             # alternative to BASE_URL: host (+ scheme) to build the URL from
SIEBEL_SCHEME=https

# Mode / behavior
SIEBEL_MCP_MODE=standalone
SIEBEL_SSO_HEADER=SBL_USER
SIEBEL_LANGUAGE_CODE=ENU
SIEBEL_DEFAULT_PAGE_SIZE=20
SIEBEL_REQUEST_TIMEOUT_MS=30000
SIEBEL_MAX_CONCURRENT_REQUESTS=5
SIEBEL_MAX_QUEUE_DEPTH=50
SIEBEL_AGGREGATE_PAGE_SIZE=100
SIEBEL_AGGREGATE_MAX_PAGES=500
SIEBEL_SESSION_TIMEOUT_MS=1800000

# Auth probe (standalone mode) — a REST-enabled BO/BC a bogus SSO header cannot read
SIEBEL_PROBE_BUSINESS_OBJECT=Account
SIEBEL_PROBE_BUSINESS_COMPONENT=Account

# Tools — comma-separated list to disable
SIEBEL_DISABLED_TOOLS=delete_record

# Optional bundled-prompt override
# SIEBEL_SYSTEM_PROMPT_PATH=/path/to/your-siebel-system.md

# OAuth (optional — for Siebel OAuth/Keycloak auth mode)
# SIEBEL_OAUTH_CLIENT_ID=
# SIEBEL_OAUTH_CLIENT_SECRET=
# SIEBEL_KEYCLOAK_TOKEN_URL=https://keycloak.example.com/realms/yourrealm/protocol/openid-connect/token

# Optional bearer-token guard on the HTTP server (unset = no auth)
# MCP_API_TOKEN=

# Optional extras
# SIEBEL_BRAVE_SEARCH_API_KEY=
# SIEBEL_API_TOKEN=
# SIEBEL_DEMO_ENABLED=false
```

> **Verify against `app/config.py`** before finalizing — every `AppSettings` field maps to a `SIEBEL_*` env var (prefix `SIEBEL_`, except `disabled_tools_raw` which uses explicit alias `SIEBEL_DISABLED_TOOLS`). `MCP_API_TOKEN` is read outside `AppSettings` (in `main.py`/`mcp_stdio.py`); keep it. The list above was derived from the `config.py` read on 2026-10-13; if fields were added since, add them here.

### 9.4 Secret scan

Before the initial commit, run `gitleaks` (or `trufflehog`) over the entire new repo tree. No env-var *values*, tokens, IPs, or hostnames may appear — only the placeholder `siebel.example.com` / `keycloak.example.com`. Fix any hit before committing.

### 9.5 README.md structure

1. **Title + tagline:** `mcp-siebel — MCP server for Oracle Siebel CRM 26.2+`.
2. **What it is** (2–3 sentences): an MCP server exposing Siebel REST as MCP tools, with stdio and HTTP/SSE transports.
3. **Install:** `pip install mcp-siebel` (stdio) and `pip install mcp-siebel[http]` (HTTP/SSE). A callout: *"Requires `mcp>=1.0,<2`. The `mcp` 2.x line is not yet supported."*
4. **Quick start — stdio (Claude Desktop):** a `claude_desktop_config.json` snippet using `"command": "mcp-siebel"` (the console script) + the `env` block with `SIEBEL_BASE_URL` / `SIEBEL_SCHEME`.
5. **HTTP/SSE mode:** `mcp-siebel-http` (uvicorn on :8201) + the `mcp-remote` bridge config for Claude Desktop (`npx -y mcp-remote http://localhost:8201/sse`).
6. **Configuration:** the env-var table (same set as `.env.example`), noting `SIEBEL_SYSTEM_PROMPT_PATH` override and the bundled default.
7. **Tools:** a pointer to the server's own `list_tools` / `--help` rather than a hand-maintained list (the tool set is `SIEBEL_DISABLED_TOOLS`-driven and changes).
8. **Development:** `pip install -e .[dev]`, `pytest`, `ruff check`, `mypy`.
9. **Origin** (§8.4 sentence).
10. **License:** MIT.

### 9.6 `.gitignore`

Standard Python: `__pycache__/`, `*.py[cod]`, `*.egg-info/`, `build/`, `dist/`, `.venv/`, `venv/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.env`.

---

## 10. Testing

- **Existing tests come along** (`tests/*.py`, 10 files). Their `from app.…` / `import app.…` imports are rewritten to `mcp_siebel` (§11 step 3). They mock the Siebel HTTP layer — no live Siebel needed. CI runs them on the matrix.
- **New `tests/test_packaging.py`** — the FastAPI-isolation guard, runs on every `pytest`. It asserts only that the **stdio path** does not pull in the HTTP deps (importing `http_server` without `[http]` is *expected* to raise `ImportError` and is not tested here):
  ```python
  """The stdio path must not pull in FastAPI/uvicorn/prometheus."""
  import sys

  def test_stdio_import_does_not_pull_http_deps() -> None:
      for mod in ("fastapi", "uvicorn", "prometheus_fastapi_instrumentator"):
          sys.modules.pop(mod, None)
      import mcp_siebel.stdio  # noqa: F401
      for mod in ("fastapi", "uvicorn", "prometheus_fastapi_instrumentator"):
          assert mod not in sys.modules, f"stdio import pulled in {mod}"
  ```
- **New `tests/test_no_unifiedcrm_refs.py`** — see §7.3.
- **No live-Siebel tests in CI.** A manual pre-release smoke test: `pip install -e .[dev]`, `pytest`, then `mcp-siebel` against a real Siebel endpoint — confirm `initialize` + `tools/list` + one `authenticate` round-trip work.

---

## 11. CI & release

### 11.1 `ci.yml` (on push + PR)

- **Lint/type job:** `ruff check`, `mypy` (Python 3.12).
- **Test matrix job:** Python 3.10 / 3.11 / 3.12 / 3.13 on `ubuntu-latest`; plus one `macos-latest` run on 3.12 (Claude Desktop users are mostly macOS). Steps: checkout, `setup-python`, `pip install -e .[dev]`, `pytest`.
- **Build job:** `python -m build` — confirms sdist + wheel build cleanly.
- **Extra-isolation job (the key packaging gate):** install with **no extras** — `pip install .` — then:
  ```bash
  python -c "import mcp_siebel.stdio"                                            # must succeed
  python -c "import sys; import mcp_siebel.stdio; assert 'fastapi' not in sys.modules"   # stdio must not pull fastapi
  ! python -c "import mcp_siebel.http_server"                                    # must FAIL (ImportError) without [http]
  pip install .[http] && python -c "import mcp_siebel.http_server"               # now must succeed
  ```
  The job fails the build if the stdio/base path ever pulls a FastAPI dep, or if `http_server` import *succeeds* without `[http]` (which would mean a stray dependency leak). It is the CI twin of `test_packaging.py`.

### 11.2 `release.yml` (on tag `v*`)

- **Trusted publishing (OIDC)** to PyPI — no stored tokens. Configure the PyPI publisher (repo `mcp-siebel`, workflow `release.yml`, environment `release`) once via the PyPI web UI.
- Steps: checkout, `setup-python`, `pip install build`, `python -m build`, `pypa/gh-action-pypi-publish@release/v1` with `permissions: id-token: write` and `environment: release`, then a GitHub Release created from the tag.
- **Versioning:** Semantic Versioning, starting `0.1.0`. Single source of truth: `[project] version` in `pyproject.toml`, bumped manually per release.
- **Pre-release checklist** (in `CONTRIBUTING.md`): run gitleaks; confirm `mcp<2` pin still required (check `mcp` changelog for a 2.x release and the migration status); bump `pyproject.toml` version; update changelog; tag `vX.Y.Z`; push tag.

---

## 12. Lift procedure (ordered, executable from fresh context)

> Run these steps in order. Paths beginning with `/Users/…/UnifiedCRM/` are absolute into the source monorepo (read-only); all other paths are relative to **this repo's root** (`/Users/uritter/Development/PaW Development/mcp-siebel/`), which already exists as an empty directory. `cd` there first and stay there.

1. **Create the repo skeleton** (the root dir already exists; create the subdirs and init git):
   ```bash
   cd "/Users/uritter/Development/PaW Development/mcp-siebel"
   mkdir -p src/mcp_siebel/prompts tests .github/workflows docs/specs
   git init
   ```
2. **Copy files per the §5 mapping** from the monorepo source `/Users/uritter/Development/PaW Development/UnifiedCRM/services/mcp-siebel/` into this repo (left side is relative to the source service dir, right side is relative to this repo root):
   - `app/__init__.py` → `src/mcp_siebel/__init__.py` (then strip to `__version__ = "0.1.0"` only — remove any re-exports that pull heavy deps).
   - `app/config.py` → `src/mcp_siebel/config.py`
   - `app/context.py` → `src/mcp_siebel/context.py`
   - `app/siebel_client.py` → `src/mcp_siebel/siebel_client.py`
   - `app/schema_cache.py` → `src/mcp_siebel/schema_cache.py`
   - `app/tools.py` → `src/mcp_siebel/tools.py`
   - `app/main.py` → `src/mcp_siebel/http_server.py`
   - `mcp_stdio.py` → `src/mcp_siebel/stdio.py`
   - `tests/*.py` → `tests/*.py` (all 10 test files + `__init__.py`).
   - Do **not** copy `Dockerfile`, `requirements.txt`, `app/CLAUDE.md`, `__pycache__/`.
3. **Rewrite imports** across all `.py` files in `src/` and `tests/`:
   - `from app.` → `from mcp_siebel.`
   - `from app import` → `from mcp_siebel import`
   - `import app.` → `import mcp_siebel.`
   - In `stdio.py`: remove the `sys.path.insert(0, os.path.dirname(__file__))` line.
   - Use a project-wide find-and-replace (e.g. `grep -rl "from app\." src tests | xargs sed -i '' 's/from app\./from mcp_siebel./g'` on macOS — adjust for GNU sed on CI).
4. **Extract `main()` entry points.**
   - `stdio.py`: add `def main() -> None: asyncio.run(_run())` and keep `if __name__ == "__main__": main()`.
   - `http_server.py`: **no `main()` needed** — it keeps its top-level `app = FastAPI(...)` and route decorators as-is. The console-script entry is the shim (next).
   - Create `src/mcp_siebel/_http_entry.py` with the `main()` from §6 (no FastAPI import at module level; the `http_server` import lives inside `try/except`).
5. **Apply the §9 scrub** (comments/docstrings) and **§9.2 config-defaults code change**. Re-grep to confirm no §7.1 forbidden string remains in `src/` or `tests/`:
   ```bash
   grep -rinE "unifiedcrm|unifiedai|agent-router|backend-api|prompt-manager|dashboard-service|schema-mapper|memory-rag|session-service|llm-engine|mcp-pdf|mcp-salesforce|account_cross_ref|list_account_cross_refs|get_service_requests|lookup_account_cross_ref|/api/v1/|prompt-manager/prompts" src tests || echo "clean"
   ```
6. **Author the standalone prompt** at `src/mcp_siebel/prompts/siebel-system.md` per §8.2. Confirm it contains the `## Authentication\n\nThe user is already authenticated.…` anchor and no §7.1 forbidden string.
7. **Update `stdio.py` prompt loading** to `importlib.resources` + `SIEBEL_SYSTEM_PROMPT_PATH` per §8.1.
8. **Add packaging & root files:** `pyproject.toml` (§6), `LICENSE` (MIT, copyright "People at Work Systems Software" — confirm exact legal name with user), `README.md` (§9.5), `CONTRIBUTING.md` (short), `.gitignore` (§9.6), `.env.example` (§9.3).
9. **Add `src/mcp_siebel/py.typed`** (empty file).
10. **Add the two new tests:** `tests/test_packaging.py` (§10) and `tests/test_no_unifiedcrm_refs.py` (§7.3).
11. **Add CI:** `.github/workflows/ci.yml` and `release.yml` (§11).
12. **Local verification:**
    ```bash
    pip install -e .[dev]
    ruff check . && mypy . && pytest          # all green
    pip install .                              # no extras
    python -c "import mcp_siebel.stdio; print('stdio ok')"
    python -c "import mcp_siebel.http_server"  # EXPECTED to fail with ImportError (fastapi) — good
    pip install .[http]
    mcp-siebel-http &  sleep 2; curl -s http://localhost:8201/health; kill %1
    mcp-siebel --help 2>&1 | head              # or a manual initialize/tools-list smoke
    ```
13. **Secret scan:** `gitleaks detect --source . --no-git` (or `trufflehog filesystem .`). Fix any hit.
14. **Initial commit:**
    ```bash
    git add -A
    git commit -m "Initial commit: mcp-siebel 0.1.0 extracted from UnifiedCRM (People at Work Systems Software)

    Co-Authored-By: Claude Code <noreply@anthropic.com>"
    ```
15. **Create the GitHub repo** `mcp-siebel` (empty — no README/license/.gitignore generated by GitHub), then:
    ```bash
    git remote add origin git@github.com:<owner>/mcp-siebel.git
    git push -u origin main
    ```
16. **Configure PyPI trusted publisher** (PyPI web UI → Account → Publishing → add pending publisher: GitHub repo `mcp-siebel`, workflow filename `release.yml`, environment `release`).
17. **First release:**
    ```bash
    git tag -a v0.1.0 -m "v0.1.0"
    git push origin v0.1.0
    ```
    `release.yml` builds and publishes to PyPI. Verify `pip install mcp-siebel` works in a clean venv.

---

## 13. Risks & open items

- **`mcp` 2.x release.** If `mcp` 2.0 lands and the `<2` pin blocks security fixes, a follow-up migration task is needed (out of scope here). The README and CONTRIBUTING pre-release checklist call this out.
- **Copyright holder name.** "People at Work Systems Software" is used per the user's instruction; confirm the exact legal entity name before the initial commit (LICENSE + pyproject `authors`).
- **Hatch wheel inclusion of `prompts/*.md`.** Hatchling auto-includes package-dir contents, but **verify** after the first build that `siebel-system.md` is actually in the wheel — if not, add an explicit `[tool.hatch.build.targets.wheel.force-include]` mapping.
- **Tests depending on old `base_url` default.** The §9.2 default change may break a test that relied on `http://siebel:8090/...`. Fix by setting `base_url` explicitly in that test — do not restore the compose-internal default.
- **Brave search key / `brave_search_api_key`.** `AppSettings` has a `brave_search_api_key` field. If `tools.py` exposes a Brave-search tool, that's part of the package's behavior — keep it, but confirm no Brave credential leaks into the repo. (Not investigated in depth during this design; the executor should check whether a Brave-search tool exists and is intended to ship.)