# Code Review — `feat/external-tool-specs`

> **Status:** Open — findings not yet addressed. Branch under review:
> `feat/external-tool-specs` @ `d98b4ff`. This review lives on its own branch
> (`docs/review-external-tool-specs`) so it can be read and merged independently of
> the code it critiques.

- **Date:** 2026-10-20
- **Branch:** `feat/external-tool-specs` (39 commits ahead of `main`, tip `d98b4ff`)
- **Scope:** Design and security review of the whole branch against `main`
- **Base:** `main` @ `c6ebb9a`
- **Method:** Every finding below was **reproduced by executing the code**, not inferred from reading it.

> **Reading the references.** All `file:line` citations, and all citations to
> `docs/tool-specs.md` / `docs/mcp-clients.md` / `README.md`, are against
> **`feat/external-tool-specs` @ `d98b4ff`** — the branch under review. This document
> is committed on a separate `docs/` branch based on `main`, where those files differ
> (several, e.g. `src/mcp_siebel/executors/upsert.py`, do not exist on `main` at all).
> Check out `feat/external-tool-specs` to follow along.

---

## Verdict

The architecture of this branch is genuinely good. The immutable `ToolSnapshot` swapped
under a lock, the keep-last-valid reload contract, the per-kind load-time validation,
the renderer-family concept, the framework/handler layering invariant (pinned by a
test), and a 338-test suite all represent careful work.

**The problems are concentrated in one place: the boundary between the spec and the
runtime.** The spec is treated as authoritative — it pins business components,
restricts field writes, declares tool schemas, and gates access. The loader validates
the spec's *shape* carefully, but at call time almost none of the spec's *promises* are
enforced. Six of the thirteen findings below are security findings, and five of those
are control failures where a documented guarantee does not hold.

Nothing here is a reason to discard the branch. The fixes are mostly small and local.
But three of them (`MCP_API_TOKEN` being ignored, the unguarded SSE transport, and
`bind` not pinning) mean the server's documented security posture does not match its
actual posture.

---

## Severity summary

| # | Severity | Finding | Location |
|---|---|---|---|
| 1 | **Critical** | Documented `MCP_API_TOKEN` env var is never read | `config.py:86` |
| 2 | **Critical** | MCP SSE transport bypasses the API-token guard | `http_server.py:298` |
| 3 | **Critical** | Caller args override spec `bind`, defeating the pinned-component rule | `executors/upsert.py:116` |
| 4 | **High** | `enabled` defaults to `true` — fail-open for external data egress | `tools.py:517` |
| 5 | **High** | `call()` does not reload, so tool revocation has a window | `tools.py:672` |
| 6 | **High** | Arguments are never validated against the spec's `input_schema` | `tools.py:684` |
| 7 | **High** | Session hijack — `authenticate` re-points an existing session | `handlers/auth.py:67` |
| 8 | **High** | TLS verification disabled on every Siebel request | `siebel_client.py:172` |
| 9 | **Medium** | `bc_upsert` ignores `scope` for updates, doing a root update | `executors/upsert.py:80` |
| 10 | **Medium** | `bc_upsert` accepts any `upsert`-family renderer, crashing after the write | `executors/upsert.py:70` |
| 11 | **Medium** | `_deref` silently discards sibling keys next to a `$ref` | `tools.py:206` |
| 12 | **Medium** | Deeply nested spec escapes `_SpecError` and crashes the loader | `tools.py:491` |
| 13 | **Low** | `_KindSpec` option lists are dead — never read by the loader | `kinds.py:52` |

**Provenance note.** Findings 2, 7 and 8 predate this branch (they are present on
`main`). Findings 1, 3, 4, 5, 6, 9, 10, 11, 12 and 13 are **introduced or exposed by
this branch**. Finding 1 is pre-existing in code but the branch's documentation actively
steers users toward the non-functional variable, which makes it materially worse here.

---

## Critical

### 1. The documented `MCP_API_TOKEN` env var is never read

**Location:** `src/mcp_siebel/config.py:86`, `src/mcp_siebel/http_server.py:214`

`AppSettings` is a `BaseSettings` with `env_prefix="SIEBEL_"` (`config.py:10`) and the
field is `api_token` (`config.py:86`). The only environment variable it can ever
populate that field from is therefore `SIEBEL_API_TOKEN`.

But the token is documented as `MCP_API_TOKEN` in three places a user will read:

- `README.md:49` — "a **bearer token** (`MCP_API_TOKEN`) to guard the HTTP/SSE server"
- `README.md:150` — the config table row
- `docs/mcp-clients.md:36` — the env-var table
- `.env.example:60` — `# MCP_API_TOKEN=`

`MCP_API_TOKEN` is read nowhere in `src/`. It appears once, in a `stdio.py` docstring.

**Reproduction:**

```python
os.environ["MCP_API_TOKEN"] = "secret"
AppSettings().api_token        # -> ''   (not 'secret')

os.environ["SIEBEL_API_TOKEN"] = "secret"
AppSettings().api_token        # -> 'secret'
```

**Impact.** An operator sets `MCP_API_TOKEN` exactly as the README says, believes the
server is protected, and gets **no protection and no warning**. `require_api_token()`
returns immediately on the empty token (`http_server.py:214`), so every REST route is
open. The failure is silent — there is no log line saying "token not configured".

**Fix.** Give the field an explicit alias so both names work, and warn loudly at
startup when neither is set but the HTTP server is running:

```python
api_token: str = Field(default="", alias="MCP_API_TOKEN")
```

Note that `populate_by_name` is not enabled on this `BaseSettings` (`config.py:10`), so
`AppSettings(api_token=...)` in tests would stop working — check `tests/` for that
pattern, or set `populate_by_name=True`.

---

### 2. The MCP SSE transport bypasses the API-token guard entirely

**Location:** `src/mcp_siebel/http_server.py:298` and `:308`

Eight REST routes carry `dependencies=[Depends(require_api_token)]`. The two MCP
endpoints do not:

| Route | Guarded |
|---|---|
| `GET /health` | no (intentional) |
| `GET /sessions/{session_id}` | yes |
| `GET /tools`, `GET /mcp/tools` | yes |
| `GET /tools/events` | yes |
| `POST /sessions` | yes |
| `POST /tools/{tool_name}`, `POST /mcp/call/{tool_name}` | yes |
| **`GET /sse`** | **no** |
| **`POST /messages/`** (mounted, `http_server.py:308`) | **no** |

**Reproduction** — live server, `SIEBEL_API_TOKEN=secret-token` set, no `Authorization`
header sent:

```
GET  /sse       (NO token) -> 200 text/event-stream
                  first SSE line: event: endpoint
                  endpoint: /messages/?session_id=b87a22a8ebcf4c4eab6bcafcf2807dea
POST initialize  (NO token) -> 202
POST tools/list  (NO token) -> 202
SSE responses received by the UNAUTHENTICATED client:
  {"jsonrpc":"2.0","id":1,"result":{"capabilities":{"tools":{"listChanged":true}},...}}
  {"jsonrpc":"2.0","id":2,"result":{"tools":[{"description":"a","inputSchema":{...},"name":"authenticate"},...
```

**Impact.** The SSE transport is *the* MCP client path — it is what `docs/mcp-clients.md`
documents for both Claude Desktop (via `mcp-remote`) and ChatGPT Connectors. An
unauthenticated client can complete a full MCP handshake, enumerate every tool with
descriptions and schemas, and (subject to finding 7) drive tool calls. The documented
token guard covers only the secondary REST surface.

**Fix.** Guard both endpoints. `SseServerTransport` is mounted as a sub-application, so
it needs either a `dependencies=` on the mount, a FastAPI dependency on `sse_endpoint`,
and/or middleware — the `handle_post_message` mount in particular needs an explicit
guard since it bypasses route decorators.

---

### 3. Caller arguments override spec `bind`, defeating the pinned-component rule

**Location:** `src/mcp_siebel/executors/upsert.py:116` (and `query.py:247`,
`read.py:124`, `delete.py:68`, `describe.py:137`)

Every generic executor merges the spec's fixed values with the caller's input as:

```python
effective = {**(options.get("bind") or {}), **args}
```

`args` wins. The MCP SDK does **not** validate tool arguments against the tool's
`inputSchema` — mcp 2.3.0's lowlevel server validates request *params* only, and
`get_tool_input_schema` is used solely for `Mcp-Param` header validation on the
streamable-HTTP transport, not for argument checking. So an undeclared argument reaches
the executor untouched.

`bind` and `allowed_fields` are the **only** mechanism by which the spec pins a tool to
a business component. `docs/tool-specs.md:545` documents exactly this:

> The BC is pinned, so the tool can only ever write to `Account`. `allowed_fields`
> constrains the write to `Account Status` and `Main Phone Number`; any other field in
> `data` is refused.

**Reproduction** — spec pins `business_component: Account`, `allowed_fields: [Name]`:

```
1. Legit call (no business_component arg):
   >>> Siebel write: BO='Account' BC='Account'  data={'Name': 'ACME'}
2. Attacker adds business_component (NOT in input_schema):
   >>> Siebel write: BO='Account' BC='SecretBO' data={'Name': 'x'}
```

**Impact.** The write lands in a different business component than the spec author
pinned. A tool the spec describes as "can only ever write to Account" can write
anywhere. The `allowed_fields` allowlist still applies to `data`, so the *fields* are
constrained, but the *target component* is not — and the target is the more sensitive
axis.

**Fix.** Flip the merge order so the spec wins, and drop bound keys the caller tried to
supply:

```python
effective = {**args, **(options.get("bind") or {})}
```

That alone fixes all five executors. Better still, reject the attempt explicitly so the
model gets a clear signal rather than silently getting the pinned value.

---

## High

### 4. `enabled` defaults to `true` — fail-open for external data egress

**Location:** `src/mcp_siebel/tools.py:517`

This branch's own history shows the control being built and then discarded:

- `d2c859b` (2026-10-19 10:44) — *"feat(access): add 'external' access level gated by
  SIEBEL_ALLOW_EXTERNAL"*, with the rationale: *"web_search sends free text to an
  outside service (Brave). In a bank deployment the model could put customer data into
  a search query, so it needs its own gate distinct from read/write/delete."*
- `fecf619` (2026-10-20 07:45) — *"feat(gating): per-tool enabled flag replaces
  access-gate env vars"*, which removed the `access` levels, `SIEBEL_ALLOW_EXTERNAL`,
  `SIEBEL_ALLOW_WRITE`, `SIEBEL_ALLOW_DELETE` and `SIEBEL_DISABLED_TOOLS` entirely.

What replaced it is a single boolean that **defaults to `true`**:

```python
enabled=bool(entry.get("enabled", True)),
```

**Reproduction** — an override spec with `web_search` and no `enabled` key:

```
exposed tools: ['authenticate', 'web_search']
web_search ENABLED by default-absent 'enabled': True
```

**Impact.** The one control that stops customer data leaving the deployment is off
only because the *bundled* spec happens to spell out `enabled: false` on every entry
that needs it. Any operator-authored override spec that omits the key on any entry
silently re-enables it. The "read-only by default" property — which the README,
`docs/tool-specs.md` and `docs/mcp-clients.md` all advertise as a headline guarantee —
now depends on every entry author remembering one line.

This is the classic shape of a control that should fail closed but fails open.

**Fix.** Either restore an explicit `access`/`category` level per entry (write /
delete / external) that the registry enforces, or invert the flag to `disabled: true`
so absence means "off". At minimum, make the loader reject an entry that omits
`enabled` when it carries a kind that can mutate or egress, rather than defaulting it
on.

---

### 5. `call()` does not reload, so tool revocation has a window

**Location:** `src/mcp_siebel/tools.py:672`

The lazy mtime reload is triggered by `list_tools()` and `snapshot()`. `call()` reads
`self._snapshot` directly and never reloads:

```python
async def call(self, name, arguments, session):
    snap = self._snapshot          # no _reload_if_changed() call
    tool = snap.tools.get(name)
    if not tool.enabled:
        return _error(f'Tool "{name}" is disabled.')
```

**Reproduction** — operator sets `enabled: false` on a write tool:

```
1. create_record enabled at startup
2. call() after revocation, no prior list: "Not authenticated. Call the authenticate tool first."
3. after list_tools(): ['authenticate']
4. call() after a list: "Tool \"create_record\" is disabled."
```

Step 2 shows the call **passed the `enabled` gate** and reached the executor.

**Impact.** On the HTTP path, `POST /tools/{tool_name}` (`http_server.py:281`) calls
`registry.call` with no reload, so a revocation does not take effect on the write path
until a client happens to call `GET /tools` first, or until the watchfiles watcher
fires. Revocation is a security operation; it should not depend on an unrelated
request's timing. The same applies to enabling a tool (it stays disabled until a list).

**Fix.** Call `_reload_if_changed()` at the top of `call()`. The watcher already calls
`registry.reload()` on change, so this is belt-and-braces for the case where the
watcher is disabled (`SIEBEL_TOOLS_SPEC_WATCH=false`) or misses an event.

---

### 6. Arguments are never validated against the spec's `input_schema`

**Location:** `src/mcp_siebel/tools.py:684`

The spec declares a typed `input_schema` per tool. It is checked for *shape* at load
time (`_valid_resolved_schema`, `tools.py:249`) and advertised to clients. It is never
checked against incoming arguments at call time.

**Reproduction:**

```python
await reg.call("query_data", {}, session)
# KeyError: 'business_object'  →  caught at tools.py:694
# {'content': [{'type': 'text',
#   'text': '[INTERNAL - do not display verbatim] The Siebel operation could not be completed.'}],
#  'isError': True}
```

Same masking for a non-numeric `page_size` (`query.py:271`):

```
page_size='abc'      -> [INTERNAL - do not display verbatim] The Siebel operation could not be completed.
page_size=0          -> Total: 0 | Page size: 20 | ...   (0 is falsy → silently becomes 20)
page_size=-5         -> Total: 0 | Page size: -5 | ...
page_size=999999     -> Total: 0 | Page size: 999999 | ...
```

**Impact.** Two distinct problems:

1. **The model cannot self-correct.** It is told "the Siebel operation could not be
   completed" for what is a malformed request it could fix by adding a parameter. It
   burns turns and may retry the same wrong call.
2. **Unvalidated values reach the backend.** `page_size: 999999` is forwarded to Siebel
   verbatim (`_query_params` does cap at 100, but `offset` and other numerics are not
   bounded), and `page_size: 0` silently becomes 20 — the caller asked for zero rows
   and got 20, with no indication.

**Fix.** Validate arguments against the resolved `input_schema` in `call()` before
dispatch, and return a *specific*, actionable error (which parameter, what was
expected, what was received) rather than the internal-error mask. This also closes
finding 3 at the same time — an undeclared `business_component` would be rejected as an
unknown property.

---

### 7. Session hijack — `authenticate` re-points an existing session

**Location:** `src/mcp_siebel/handlers/auth.py:67`, reached via `http_server.py:157`
and `stdio.py:132`

Both transports resolve the session from the caller-supplied `session_id` and then hand
it to the `authenticate` handler, which **mutates that session in place**:

```python
session.username = username
session.client = client
session.cache = SchemaCache(client)
```

There is no check that the session is unowned, and no binding between a session and
the credentials that created it.

**Reproduction:**

```
session found for attacker-supplied id: True -> owner: alice
after authenticate, session owner is now: mallory
=> user A's session was re-pointed to user B: mallory
```

**Impact.** Client A authenticates and holds session id S. Client B calls `authenticate`
passing `session_id=S`. A's session is silently rebound to B's Siebel identity, and A's
subsequent calls return B's data — or vice versa. Session ids appear in tool results,
logs, transcripts and error messages, so they are not secret. The `SessionStore` has no
per-session owner binding.

**Fix.** `authenticate` should always create a fresh session rather than mutating a
supplied one, or should refuse to rebind a session that already has a `client`. If
re-binding is a supported flow, it needs an explicit credential check against the
session's current identity.

---

### 8. TLS verification disabled on every Siebel request

**Location:** `src/mcp_siebel/siebel_client.py:93` and `:172`

```python
async with httpx.AsyncClient(verify=False, timeout=self.timeout) as client:
```

Both the login call (`:93`) and every data call (`:172`) skip certificate validation.

**Impact.** The server's TLS certificate is never checked, so a network-positioned
attacker can present any certificate and terminate, read and rewrite the entire CRM
session — including the SSO header that carries the authenticated user's identity
(`siebel_client.py:119`) and every record returned. The target deployment (README:
"Siebel vanilla demo database", typically behind a reverse proxy with a self-signed or
internal-CA certificate) is exactly the case where `verify=False` gets added silently
and never revisited.

**Fix.** Make verification configurable and default it on:
`SIEBEL_VERIFY_TLS` / `SIEBEL_CA_BUNDLE`. For self-signed deployments, point
`SIEBEL_CA_BUNDLE` at the internal CA rather than disabling validation outright.

---

## Medium

### 9. `bc_upsert` ignores `scope` for updates, performing a root-scope update

**Location:** `src/mcp_siebel/executors/upsert.py:80`

The required-parameter check computes `required_params` in an if/elif chain where the
`update` branch precedes the `scope == "child"` branch:

```python
if operation == "update":
    required_params = ("business_object", "id")
elif scope == "child":
    required_params = ("business_object", "parent_id", "child_business_component")
```

So `operation: update, scope: child` never reaches the child branch: `parent_id` and
`child_business_component` are never required, and the entry loads without error.

**Reproduction:**

```
A) operation=update scope=child -> which path runs?
   >>> update_record BO='Account' BC='Account' id='1-1'
```

The `scope: child` option is silently dead for updates; the record is updated as a root
record, which is not what the spec says.

**Fix.** Reject the combination at load time (`operation == "update" and scope ==
"child"` → `_SpecError`), or implement the child-update path.

---

### 10. `bc_upsert` accepts any `upsert`-family renderer, crashing after the write succeeds

**Location:** `src/mcp_siebel/executors/upsert.py:70`, `renderers.py:296`

`validate_render` is called with `accepted_families={"upsert"}`, and all three of
`create_prose`, `update_prose` and `child_create_prose` share the `"upsert"` family
(`renderers.py:294-297`). But they read **different context keys** — `_update_prose`
does `ctx['id']` (`renderers.py:104`), while the create path builds a context with no
`id` (`upsert.py:151`).

**Reproduction:**

```
B) operation=create render=update_prose -> runtime error?
   >>> create_record BO='Account' BC='Account'
   result: [{'type': 'text', 'text': '[INTERNAL - do not display verbatim] The Siebel operation could not be completed.'}]
   KeyError: 'id'
```

**Impact.** The most damaging detail: the `create_record` call **had already
succeeded** (the client logged it) before the renderer failed. The model is shown a
generic failure for a mutation that was actually applied — it will likely retry, and
the retry creates a **duplicate record**. The same applies to `operation: update` with
`render: create_prose`.

**Fix.** Split the `upsert` family into `create` / `update` / `child_create`
sub-families, or validate the renderer against the operation explicitly.

---

### 11. `_deref` silently discards sibling keys next to a `$ref`

**Location:** `src/mcp_siebel/tools.py:206`

```python
if "$ref" in node:
    ...
    return _deref(deepcopy(defs[name]), defs, (*_stack, name))
```

The whole node is replaced by the referenced definition. Any sibling key — a per-use
`description`, `default`, `examples` — is discarded.

**Reproduction:**

```python
_deref({"$ref": "#/$defs/id", "description": "CUSTOM per-use"}, defs)
# -> {'type': 'string', 'description': 'base desc'}     # custom text gone
```

**Impact.** A spec author documenting a business rule on one tool's parameter gets the
generic shared text instead, with no warning. `_valid_resolved_schema` only checks that
a `type` is present, and the published JSON Schema does not express the restriction, so
the loss is undetectable at every layer.

**Fix.** Either reject `$ref` nodes that carry siblings (explicit, no silent loss) or
merge siblings over the resolved definition, per JSON Schema 2020-12 semantics.

---

### 12. A deeply nested spec escapes `_SpecError` and crashes the loader

**Location:** `src/mcp_siebel/tools.py:491`

`_deref` recurses once per `$ref` link with no depth limit, and `RecursionError` is not
a `_SpecError`, so it is not caught by the loader's error handling.

**Reproduction** — a spec with 2000 chained `$defs` entries:

```
RAISED: RecursionError maximum recursion depth exceeded
```

**Impact.** Two distinct failures:

- **On reload:** the `RecursionError` escapes the keep-last-valid path
  (`tools.py:577` catches only `_SpecError`) and takes down the request that triggered
  it, rather than keeping the last valid tool set.
- **On first load:** it escapes the constructor's `_SpecError` contract, so the process
  dies with a stack trace instead of the intended *"exit non-zero with a clear error"*.

**Fix.** Add a depth limit to `_deref` that raises `_SpecError`, and broaden the
catch in `_reload_if_changed` to also catch `RecursionError`.

---

## Low

### 13. `_KindSpec` option lists are dead — never read by the loader

**Location:** `src/mcp_siebel/kinds.py:52`

Every kind declares the options it allows and requires on the `_KindSpec` dataclass:

```python
allowed_options: frozenset[str]
required_options: frozenset[str]
```

But grepping `src/` shows the only occurrences are the declarations themselves —
`tools.py:317`, `kinds.py:52`, and one pair per executor. **Nothing reads them.** Each
executor instead hard-codes its own `_X_ALLOWED_OPTIONS` constant and its own
`extra`/`missing` checks.

**Impact.** Two sources of truth per kind, only one of which is enforced. A
contributor who adds an option to a kind's `allowed_options` frozenset changes
nothing. A contributor who trusts that field to be the enforcement point ships a spec
option that is rejected at load time with a confusing message. The field is
unverifiable by any reader, and no test covers it.

**Fix.** Either have the registry enforce `allowed_options`/`required_options`
centrally (and delete the per-executor duplicates), or delete the fields.

---

## What the branch gets right

Worth stating explicitly, because these are the parts a fix should not regress:

- **The immutable snapshot swap** (`ToolSnapshot`, `tools.py:108`) — a request can never
  mix two spec versions. The `spec_version` / `content_hash` discipline and the
  `preferred_fields`-only-edit handling are carefully reasoned.
- **The keep-last-valid reload contract** — a bad spec edit never replaces a good tool
  set, and a first-load failure aborts with a clear error rather than serving an
  unknown tool set. This is exactly the right shape, and it is what makes finding 12
  worth fixing rather than merely noting.
- **Per-kind load-time validation** — unknown options, missing required options,
  render families, bind-key typos and type errors are all caught at load. The *design*
  of this validation is right; findings 9 and 10 are gaps in it, not a rejection of it.
- **The framework/handler layering invariant**, pinned by
  `tests/test_framework_no_handler_imports.py` — the "no tool names in framework code"
  discipline is genuinely well enforced and well documented.
- **Characterization tests before migration** — the commit sequence (characterization
  tests → executor → migration) is the right way to do this kind of refactor.
- **The `searchspec_template` escaping** (`query.py:199`) is *more* careful than the
  pre-existing paths: it rejects `"`, `[` and `]` outright because no safe escaping
  rule exists for them. The older paths (`_fallback.py:193`, `siebel_client.py:424`)
  escape only `'` and would let a `]` terminate the literal. Worth aligning while
  fixing the other findings.

---

## Suggested remediation order

1. **Finding 1** — alias `api_token` to `MCP_API_TOKEN`; warn at startup if the HTTP
   server runs without a token. (Smallest diff, largest security delta.)
2. **Finding 2** — guard `/sse` and `/messages/`.
3. **Finding 3** — flip the merge order so `bind` wins. One line per executor.
4. **Finding 6** — validate arguments against `input_schema`; return specific errors.
   This also closes the undeclared-property half of finding 3.
5. **Finding 4** — decide the fail-closed shape for access control and implement it
   deliberately rather than by default.
6. **Findings 5, 7, 8** — reload on call, stop rebinding live sessions, make TLS
   verification configurable and default it on.
7. **Findings 9-13** — the correctness and hygiene items; each is small and
   independently testable.
