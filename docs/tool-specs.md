# Tool spec reference

mcp-siebel's tool set is defined by a JSON spec file —
[`src/mcp_siebel/specs/tool_specs.json`](../src/mcp_siebel/specs/tool_specs.json)
by default — not by code. The registry reads it at startup, resolves a generic
executor per tool from the entry's `kind`, and hot-reloads it by mtime. This
document is the reference for authoring and overriding that file: the file
format, every kind, the access gates, `bind` / `allowed_fields` /
`searchspec_template`, how to add a tool, and the hot-reload semantics.

A JSON Schema for the file lives at
[`src/mcp_siebel/specs/tool_specs.schema.json`](../src/mcp_siebel/specs/tool_specs.schema.json);
the bundled spec references it via `"$schema"` so editors validate and
autocomplete. The schema is an authoring aid — the loader's runtime validation
is the source of truth (a spec that passes the schema may still be rejected,
e.g. a render from the wrong family for a kind's mode/source/result, a handler
that does not resolve, or a cross-field rule the schema does not express).

## File structure

The top-level object has four sections:

```json
{
  "$schema": "./tool_specs.schema.json",
  "$defs": { "...": "shared parameter definitions, referenced via $ref" },
  "preferred_fields": { "Contact": ["Last Name", "First Name", "..."] },
  "tools": { "tool_name": { "...": "one entry per tool" } }
}
```

| Section | Required | Purpose |
|---|---|---|
| `$schema` | no | Points editors at the JSON Schema. **Ignored by the loader.** |
| `$defs` | no | Shared parameter definitions. Referenced from `input_schema` with `{"$ref": "#/$defs/<name>"}`. The loader inlines every `$ref` before serving a schema, so clients always receive a fully-resolved `inputSchema`. Only `#/$defs/<name>` refs are supported (no external refs); cycles are rejected. |
| `preferred_fields` | no | Business Component name → ordered list of field names. An ordered *preference list* the `bc_read` curated-query fallback intersects with the fields the BC actually reports. Surfaced to executors via `ToolContext.preferred_fields`; hot-reloaded with the tools. |
| `tools` | **yes** | The tool map: tool name → entry. Duplicate tool names are rejected. |

Every tool entry has fields **common to every kind** plus **kind-specific
options**. The common fields are:

| Field | Required | Type | Notes |
|---|---|---|---|
| `description` | yes | string | The tool description shown to the model. |
| `kind` | yes | string | Selects the executor. One of `python`, `bc_query`, `bc_upsert`, `bc_delete`, `bc_describe`, `bc_read`, `static_text`. |
| `access` | yes | string | Access level: `read`, `write`, `delete`, `external`. See [Access levels](#access-levels). |
| `always_enabled` | no | bool | Exempts the tool from `SIEBEL_DISABLED_TOOLS`. Only allowed with `access: "read"`. |
| `establishes_session` | no | bool | True for the one tool the transports call to create a session. Exactly one entry must set it; it must also be `always_enabled` and `access: "read"`. |
| `input_schema` | yes | object | The MCP `inputSchema` (a JSON Schema). Every property must carry a concrete `type` after `$ref` inlining. |

Kind-specific options follow, alphabetically; `input_schema` is conventionally
last. (The bundled file is normalized to that key order.)

## Access levels

The `access` field is the single source of truth for gating. The registry hides
a tool from `list_tools` and refuses it at `call` when its gate is off —
regardless of `always_enabled`, which only exempts the `SIEBEL_DISABLED_TOOLS`
list.

| `access` | Gate | Default | Meaning |
|---|---|---|---|
| `read` | — | always on | Pure Siebel reads. Always available. |
| `write` | `SIEBEL_ALLOW_WRITE` | off | Create / update / upload — mutates Siebel data. |
| `delete` | `SIEBEL_ALLOW_DELETE` | off | Delete — removes Siebel data. |
| `external` | `SIEBEL_ALLOW_EXTERNAL` | off | Sends free text to an outside service (e.g. `web_search` → Brave). Off by default so customer data can't leak into a search query without an explicit opt-in. |

Which access level a kind permits is fixed in code (and mirrored in the schema):

| Kind | Allowed `access` |
|---|---|
| `python` | `read`, `write`, `delete`, `external` |
| `bc_query` | `read` |
| `bc_upsert` | `write` |
| `bc_delete` | `delete` |
| `bc_describe` | `read` |
| `bc_read` | `read` |
| `static_text` | `read` |

The registry logs once at startup which access levels are enabled.

### `always_enabled`

Exempts a tool from `SIEBEL_DISABLED_TOOLS` so a deployer can't brick the server
by listing it there (the session-establishment tool is the one that matters).
Load-time rule: **only allowed with `access: "read"`** — it must never bypass
the access gate. The registry logs a warning if `SIEBEL_DISABLED_TOOLS` lists an
always-enabled tool (the entry is ignored, not silently dropped).

### `establishes_session`

Exactly one entry must mark `establishes_session: true`. It must also be
`always_enabled` and `access: "read"`. The transports read its name from the
registry (`session_tool_name`) so they need not hard-code a tool name.

## Kinds

Each kind is a small, generic executor parameterised by the spec entry's
options. Argument names come from the entry's `input_schema`; the executor
binds to the entry at load time and the resulting callable is the single
handler the registry invokes.

### `python` — the escape hatch

For tools with real custom logic that no generic executor covers (a business
service call, a paginated aggregation loop, an external HTTP call).

| Option | Required | Type | Notes |
|---|---|---|---|
| `handler` | yes | string | `mcp_siebel.handlers.<module>:<func>`. The resolver permits **only** modules under `mcp_siebel.handlers`; anything outside is rejected. |

Allowed `access`: `read`, `write`, `delete`, `external`. No `render`.

Example (the bundled `aggregate_data`):

```json
"aggregate_data": {
  "description": "Compute sum, avg, min, max, or count across all matching records.",
  "kind": "python",
  "access": "read",
  "handler": "mcp_siebel.handlers.records:_aggregate_data",
  "input_schema": {
    "properties": {
      "business_object": { "$ref": "#/$defs/business_object" },
      "field": { "type": "string" },
      "operation": { "type": "string" },
      "searchspec": { "$ref": "#/$defs/searchspec" }
    },
    "required": ["business_object", "field", "operation"],
    "type": "object"
  }
}
```

### `bc_query` — query / count over a Business Component

| Option | Required | Type | Notes |
|---|---|---|---|
| `scope` | yes | `root` \| `child` | Query the BC directly (`root`) or under a parent record (`child`). |
| `result` | yes | `records` \| `count` | Return matching records, or just a count. |
| `render` | yes | string | `records` → a `query`-family renderer; `count` → a `count`-family renderer. |
| `bind` | no | object | See [bind](#bind). |
| `searchspec_template` | no | string | See [searchspec_template](#searchspec_template). Mutually exclusive with a `searchspec` argument or a bound `searchspec`. |

Allowed `access`: `read`. Renderer families: `query` (`query_prose`,
`child_query_prose`, `query_json`) for `result: "records"`; `count`
(`count_prose`, `count_json`) for `result: "count"`. Required params:
`business_object` (always); `parent_id` + `child_business_component` for
`scope: "child"`.

Example (`query_data`):

```json
"query_data": {
  "description": "Query Siebel CRM data using a Siebel searchspec expression.",
  "kind": "bc_query",
  "access": "read",
  "render": "query_prose",
  "result": "records",
  "scope": "root",
  "input_schema": {
    "properties": {
      "business_object": { "$ref": "#/$defs/business_object" },
      "searchspec": { "$ref": "#/$defs/searchspec" },
      "fields": { "$ref": "#/$defs/fields" }
    },
    "required": ["business_object"],
    "type": "object"
  }
}
```

### `bc_upsert` — create or update a record

| Option | Required | Type | Notes |
|---|---|---|---|
| `operation` | yes | `create` \| `update` | |
| `scope` | yes | `root` \| `child` | Create under the BC (`root`) or under a parent record (`child`). |
| `render` | yes | string | An `upsert`-family renderer. |
| `bind` | no | object | See [bind](#bind). |
| `allowed_fields` | no | array of strings | See [allowed_fields](#allowed_fields). **Required (non-empty) when `business_component` is bound.** |

Allowed `access`: `write`. Renderer family: `upsert` (`create_prose`,
`create_json`, `update_prose`, `child_create_prose`). Required params:
`business_object` (always); `id` for `operation: "update"`; `parent_id` +
`child_business_component` for `scope: "child"`.

Example (`create_record`):

```json
"create_record": {
  "description": "Create a new Siebel record.",
  "kind": "bc_upsert",
  "access": "write",
  "operation": "create",
  "render": "create_prose",
  "scope": "root",
  "input_schema": {
    "properties": {
      "business_object": { "$ref": "#/$defs/business_object" },
      "data": { "$ref": "#/$defs/data" }
    },
    "required": ["business_object", "data"],
    "type": "object"
  }
}
```

### `bc_delete` — delete a record by Id

| Option | Required | Type | Notes |
|---|---|---|---|
| `render` | yes | string | A `delete`-family renderer. |
| `bind` | no | object | See [bind](#bind). |

Allowed `access`: `delete`. Renderer family: `delete` (`delete_prose`).
Required params: `business_object`, `id`.

Example (`delete_record`):

```json
"delete_record": {
  "description": "Delete a Siebel record by Id.",
  "kind": "bc_delete",
  "access": "delete",
  "render": "delete_prose",
  "input_schema": {
    "properties": {
      "business_object": { "$ref": "#/$defs/business_object" },
      "id": { "$ref": "#/$defs/id" }
    },
    "required": ["business_object", "id"],
    "type": "object"
  }
}
```

### `bc_describe` — catalogue / describe a Business Component

| Option | Required | Type | Notes |
|---|---|---|---|
| `source` | yes | `business_objects` \| `bc_descriptor` \| `repository` | `business_objects` lists all BOs; `bc_descriptor` is the REST data-API field list (with the SBL-EAI-50257 child-BC fallback); `repository` is the richer workspace-repository field list. |
| `render` | yes | string | A renderer matching the source's family. |
| `bind` | no | object | See [bind](#bind). |

Allowed `access`: `read`. Renderer families: `list` (`list_prose`, `list_json`)
for `business_objects`; `describe` (`describe_bc_prose`, `describe_bc_json`) for
`bc_descriptor`; `repository` (`repository_prose`, `repository_json`) for
`repository`. Required params: `business_object` (except for
`source: "business_objects"`, which takes no BO argument).

Example (`describe_business_object`):

```json
"describe_business_object": {
  "description": "Get field schema for a Siebel Business Object/Component.",
  "kind": "bc_describe",
  "access": "read",
  "render": "describe_bc_prose",
  "source": "bc_descriptor",
  "input_schema": {
    "properties": {
      "business_object": { "$ref": "#/$defs/business_object" }
    },
    "required": ["business_object"],
    "type": "object"
  }
}
```

### `bc_read` — read one record (with or without children)

| Option | Required | Type | Notes |
|---|---|---|---|
| `mode` | yes | `single` \| `with_children` | Read one record, or one record with embedded child records. |
| `render` | yes | string | `single` → a `record`-family renderer; `with_children` → a `hierarchical_record`-family renderer. |
| `fallback` | no | `curated_query` | Only valid with `mode: "single"`. On a direct-read failure, retry as a scoped query that names fields (see below). |
| `bind` | no | object | See [bind](#bind). |

Allowed `access`: `read`. Renderer families: `record` (`record_prose`) for
`single`; `hierarchical_record` (`record_hierarchical_prose`) for
`with_children`. Required params: `business_object`, `id`.

The `curated_query` fallback: a direct `/data/{BO}/{BC}/{id}` read asks Siebel
for the whole record. On an instance whose integration component names a field
the BC doesn't define, Siebel 500s serialising it (e.g. Contact referencing
'Primary Address Integration Id', SBL-DAT-00398). The fallback retries as a
scoped `[Id]='...'` query with an explicit field list — the curated
`preferred_fields` list intersected with what the BC reports, or a ranked
heuristic for an unknown BC — so a misconfigured integration component doesn't
make a record unreadable. If the scoped query also fails (or matches no items),
the original error propagates and the registry masks it for the caller.

Example (`get_record`):

```json
"get_record": {
  "description": "Get a single Siebel record by Id.",
  "kind": "bc_read",
  "access": "read",
  "fallback": "curated_query",
  "mode": "single",
  "render": "record_prose",
  "input_schema": {
    "properties": {
      "business_object": { "$ref": "#/$defs/business_object" },
      "id": { "$ref": "#/$defs/id" },
      "fields": { "$ref": "#/$defs/fields" }
    },
    "required": ["business_object", "id"],
    "type": "object"
  }
}
```

### `static_text` — return fixed text

No auth, no Siebel call — the tool works without a session. Arguments are
ignored (the model need not pass any).

| Option | Required | Type | Notes |
|---|---|---|---|
| `text` | yes | string \| array of strings | A list is joined with `\n` so long texts stay readable in JSON. |

Allowed `access`: `read`. No `render`.

Example (`get_query_syntax`):

```json
"get_query_syntax": {
  "description": "Return Siebel Query Language syntax reference.",
  "kind": "static_text",
  "access": "read",
  "text": [
    "Use <> for not equal; never use !=.",
    "Field names go in square brackets: [Status] = 'Open'."
  ],
  "input_schema": { "properties": {}, "type": "object" }
}
```

## `bind`

`bind` fixes values in the spec that are merged with the caller's input to form
the effective arguments. It pins a tool to a fixed Business Object / Component,
a fixed field list, a fixed search spec — so a customer-specific tool can expose
one shaped query without letting the model name the BC or the fields.

Rules (enforced at load time):

- **Disjoint from `input_schema.properties`** — a bound key may not also be a
  caller-supplied argument (the two would be ambiguous). Add a bind key only
  for parameters you want to fix; keep the rest in `input_schema`.
- **Known to the executor** — a bind key must name a parameter the kind's
  executor actually reads (a typo that would silently do nothing is rejected).
  The known keys per kind are documented above with each kind's options.
- **Type-checked per kind** — BO/BC/id/searchspec are strings, `page_size`/
  `offset` are non-negative ints, `fields`/`childlinks` are lists of strings,
  `data` is an object.

```json
"bind": { "business_object": "Account", "business_component": "Account" }
```

## `allowed_fields`

`bc_upsert` only. An allowlist of field names a caller may write into `data`;
any other field is refused at call time. This is how a bound write tool stays
safe: pinning `business_component` locks the BC, and `allowed_fields` constrains
the columns. **Binding `business_component` requires a non-empty
`allowed_fields` list** — otherwise a bound-BC tool would accept arbitrary field
names, defeating the point of pinning the BC.

```json
"allowed_fields": ["Name", "Account Status", "Main Phone Number"]
```

## `searchspec_template`

`bc_query` only. A Siebel search-spec template with `{placeholder}` tokens
filled from the caller's matching `input_schema` properties. It lets a
customer-specific tool expose one shaped query (e.g. "active accounts of a given
type") without handing the model raw searchspec syntax.

Rules (enforced at load time, plus call time):

- Each `{placeholder}` must name an `input_schema` property. Unknown placeholders
  are rejected at load.
- **Every placeholder must sit inside a single-quoted literal** in the template.
  A placeholder outside quotes is rejected at load (the value would be
  interpolated into the searchspec's structure, not into a string operand).
- **Mutually exclusive** with a `searchspec` argument or a bound `searchspec`.

### Quoting and escaping

Siebel search-spec string literals are single-quoted, and an embedded single
quote is doubled (`'` → `''`). That is the only escaping the template applies.

```json
"searchspec_template": "[Account Status] = 'Active' AND [Type] = '{account_type}'"
```

When the caller passes `account_type = "Reseller"`, this renders to
`[Account Status] = 'Active' AND [Type] = 'Reseller'`.

For `"`, `[` and `]` **no safe escaping rule exists** — `[`/`]` delimit field
references and `"` is the alternate string delimiter — so a placeholder value
containing any of them is **rejected at call time** rather than guessed. A
placeholder value's embedded `'` is doubled. There is no other transformation;
design templates so a value can only land inside a single-quoted literal.

## How to add a tool

### JSON-only (a generic kind)

If one of the generic kinds fits, adding a tool is a spec edit only — no code,
no restart of the registry's import graph (the spec is hot-reloaded):

1. Add an entry under `tools` with the right `kind`, `access`, the kind's
   required options, and an `input_schema`.
2. Save the file. The next `tools/list` serves the new tool (see
   [Hot-reload](#hot-reload)).

A `bc_query` / `bc_upsert` / `bc_read` / `bc_describe` tool with `bind` can be
shaped for a specific deployment with no Python at all (see the
[worked example](#worked-example-a-customer-specific-bound-tool)).

### `python` escape hatch

If no generic executor covers the logic (a business service call, a paginated
loop, an external HTTP call):

1. Write the handler as an `async def` in a module under
   `mcp_siebel/handlers/` (e.g. `handlers/mydomain.py`). It takes
   `(args: dict, ctx: ToolContext)` and returns a tool-result dict. Import the
   shared helpers from `mcp_siebel.common` (`_text`, `_error`, `_require_auth`,
   …).
2. Point the entry's `handler` at `"mcp_siebel.handlers.mydomain:_my_tool"`.
3. **A code change requires a restart** — only the JSON spec is hot-reloaded;
   handler modules are imported once. (Adding a brand-new handler module also
   requires a restart; the resolver imports it by name.)

Framework code (executors, renderers, `tools`, `kinds`) must not import from
`mcp_siebel.handlers` — handlers are the escape hatch, not a framework
dependency. This invariant is pinned by
`tests/test_framework_no_handler_imports.py`.

## Hot-reload

The registry stats the spec file on every `list_tools` call and re-reads it only
when its mtime changed. Every reload re-runs the full pipeline (parse with
duplicate-key detection → deref `$ref` → validate → per-entry kind + executor +
access/session-rule resolution) and only swaps in the new set on success.

- **What is reloaded:** the tool entries and the `preferred_fields` mapping
  (both served from the last successful load).
- **Fail-fast vs keep-last-valid:** a failure on the very first load (no last
  valid set) aborts startup — the process exits non-zero with a clear error
  naming the offending entry and rule. A failure on a later reload is non-fatal:
  the last valid set keeps serving and the failure is logged. A broken file is
  not re-read on every call (its mtime is recorded) — it is retried when the
  file changes again.
- **Connected clients:** the server does **not** push
  `notifications/tools/list_changed`; it has no file watcher. A spec edit goes
  live on the next `tools/list` (stdio) or `GET /tools` (HTTP) the client issues
  — that call triggers the mtime check. A client that never re-requests the tool
  list sees the change only after reconnecting.

## Worked example: a customer-specific bound tool

A neutral example (no real customer names): a read tool that lists active
accounts of a given type, and a bound write tool that updates an account's
status and phone — both shaped by the spec with no Python.

### Bound read with a `searchspec_template`

```json
"active_accounts_by_type": {
  "description": "List active accounts of a given type (Account BC).",
  "kind": "bc_query",
  "access": "read",
  "bind": {
    "business_object": "Account",
    "business_component": "Account",
    "fields": ["Name", "Account Status", "Type", "Main Phone Number"]
  },
  "render": "query_prose",
  "result": "records",
  "scope": "root",
  "searchspec_template": "[Account Status] = 'Active' AND [Type] = '{account_type}'",
  "input_schema": {
    "properties": {
      "account_type": {
        "description": "Account type, e.g. 'Reseller' or 'Customer'.",
        "type": "string"
      }
    },
    "required": ["account_type"],
    "type": "object"
  }
}
```

The model supplies only `account_type`; the BO/BC/fields and the
`[Account Status] = 'Active'` predicate are fixed. The `{account_type}`
placeholder sits inside a single-quoted literal, so the model cannot inject
search-spec structure — only a string operand, with `'` doubled and `"`/`[`/`]`
rejected.

### Bound write with `allowed_fields`

```json
"update_account_contact_info": {
  "description": "Update an account's status and main phone number only.",
  "kind": "bc_upsert",
  "access": "write",
  "allowed_fields": ["Account Status", "Main Phone Number"],
  "bind": {
    "business_object": "Account",
    "business_component": "Account"
  },
  "operation": "update",
  "render": "update_prose",
  "scope": "root",
  "input_schema": {
    "properties": {
      "id": { "$ref": "#/$defs/id" },
      "data": { "$ref": "#/$defs/data" }
    },
    "required": ["id", "data"],
    "type": "object"
  }
}
```

The BC is pinned, so the tool can only ever write to `Account`. `allowed_fields`
constrains the write to `Account Status` and `Main Phone Number`; any other
field in `data` is refused. (Binding `business_component` requires a non-empty
`allowed_fields` — enforced at load.) Enable with `SIEBEL_ALLOW_WRITE=true`; the
tool is hidden and refused while the gate is off.