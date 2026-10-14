"""MCP-style Siebel tool registry and handlers."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib.resources import files
from typing import Any

import httpx

from .config import AppSettings
from .schema_cache import SchemaCache
from .siebel_client import SiebelClient, SiebelError, SiebelRecord

log = logging.getLogger("mcp-siebel")

# Bundled default tool-spec file (name/description/inputSchema per tool). The
# registry reads this — or an override at SIEBEL_TOOLS_SPEC_PATH — and hot-
# reloads it by mtime. See _load_spec / ToolRegistry._reload_if_changed.


ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]


@dataclass
class SessionContext:
    session_id: str
    username: str | None = None
    client: SiebelClient | None = None
    cache: SchemaCache | None = None
    demo_authenticated: bool = False
    # Monotonic clock reading at creation — used by SessionStore for an
    # absolute expiry (see AppSettings.session_timeout_ms). Monotonic so a
    # wall-clock change can't resurrect or kill a session.
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class ToolContext:
    settings: AppSettings
    session: SessionContext | None


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


class SessionStore:
    """In-memory session map with absolute expiry.

    A session expires ``timeout_s`` seconds after its ``created_at`` (mirrors
    the fixed lifetime of the underlying Siebel REST session, so mcp-siebel
    never hands out an id Siebel has already killed). Expired entries are
    evicted lazily on ``get()`` and reclaimed by ``sweep()``, which also runs
    on every ``create()`` — so each new auth prunes the dead ones and the map
    can't grow unbounded between container restarts.
    """

    def __init__(self, timeout_s: float) -> None:
        self._sessions: dict[str, SessionContext] = {}
        self._timeout_s = timeout_s

    def _expired(self, session: SessionContext) -> bool:
        return (time.monotonic() - session.created_at) >= self._timeout_s

    def create(self) -> SessionContext:
        self.sweep()  # reclaim expired sessions on every new auth
        session = SessionContext(session_id=str(uuid.uuid4()))
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> SessionContext | None:
        key = session_id or ""
        session = self._sessions.get(key)
        if session is None:
            return None
        if self._expired(session):
            # An expired id is treated as not found — callers re-authenticate.
            del self._sessions[key]
            return None
        return session

    def sweep(self) -> int:
        """Delete every expired session; return how many were removed."""
        expired = [sid for sid, s in self._sessions.items() if self._expired(s)]
        for sid in expired:
            del self._sessions[sid]
        return len(expired)

    def count(self) -> int:
        # Keep /health honest: expired sessions don't count.
        self.sweep()
        return len(self._sessions)


def _resolve_spec_path(settings: AppSettings) -> str:
    """Resolve the tool-spec file path: the SIEBEL_TOOLS_SPEC_PATH override if
    set, else the bundled ``mcp_siebel/specs/tool_specs.json`` (located via
    importlib.resources so it works under an installed wheel too)."""
    override = (settings.tools_spec_path or "").strip()
    if override:
        return override
    return str(files("mcp_siebel") / "specs" / "tool_specs.json")


def _load_spec(path: str) -> dict[str, Any] | None:
    """Read and parse the tool-spec JSON at ``path``.

    Returns the parsed object (tool name → entry) on success, or ``None`` on any
    read/parse error so the caller can serve degraded placeholder specs instead
    of crashing the server over a bad spec file.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        log.warning("Tool spec file not readable at %s: %s — serving degraded specs", path, exc)
        return None
    except json.JSONDecodeError as exc:
        log.warning("Tool spec file is not valid JSON at %s: %s — serving degraded specs", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Tool spec file at %s must be a JSON object keyed by tool name — serving degraded specs", path)
        return None
    return data


def _valid_spec_entry(entry: Any) -> bool:
    """Light per-entry validation: an entry must carry a string description and
    an input_schema dict with a type and properties map. Anything failing this
    is ignored for that tool and a degraded placeholder spec is used instead."""
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("description"), str)
        and isinstance(entry.get("input_schema"), dict)
        and isinstance(entry["input_schema"].get("properties"), dict)
        and "type" in entry["input_schema"]
    )


def _degraded_spec(name: str, handler: ToolHandler) -> ToolSpec:
    """Placeholder ToolSpec for when the JSON spec is unavailable or has no valid
    entry for a tool. The tool stays callable (its handler is real) but the LLM
    gets no parameter guidance — operators fix the spec file to restore full
    schemas. Logged at load time so a degraded load is never silent."""
    return ToolSpec(
        name=name,
        description=f"Tool '{name}' — spec unavailable (tool_specs.json missing or invalid).",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )


class ToolRegistry:
    """Holds the tool map and serves metadata from an external spec file.

    Runtime knows only the handlers (``_build_handlers()`` — name → callable).
    Each tool's description and input_schema come solely from the spec JSON: the
    file is the single source of truth for metadata. The spec is hot-reloaded by
    mtime — ``list_tools()`` stats the file and re-reads it only when it changed,
    so edits go live on the next tools/list with no restart. A missing, corrupt,
    or entry-less spec yields a degraded placeholder (tool still callable, no
    param guidance) and a warning — the server never crashes over a bad spec.

    The Python-authored metadata in ``_build_tool_metadata()`` is NOT used at
    runtime; it is the generator's input (``scripts/dump_tool_specs.py``) that
    bakes the bundled JSON. Edit the JSON (or an override at
    ``SIEBEL_TOOLS_SPEC_PATH``) to change what clients see.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._spec_path = _resolve_spec_path(settings)
        self._spec_mtime: float | None = None
        self._lock = threading.Lock()
        self._handlers = _build_handlers()
        self._tools: dict[str, ToolSpec] = {}
        self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        """Re-read the spec file if its mtime changed since the last load, and
        rebuild ``self._tools`` from the handler map + spec metadata."""
        with self._lock:
            try:
                mtime = os.stat(self._spec_path).st_mtime_ns
            except OSError:
                # File gone missing. Keep serving the last good tools if we have
                # them; otherwise degrade so the server still lists something.
                if self._tools:
                    log.warning("Tool spec file unavailable: %s — keeping current tools", self._spec_path)
                else:
                    log.warning("Tool spec file unavailable: %s — serving degraded specs", self._spec_path)
                    self._tools = {n: _degraded_spec(n, h) for n, h in self._handlers.items()}
                self._spec_mtime = None
                return
            if mtime == self._spec_mtime:
                return
            self._spec_mtime = mtime

            data = _load_spec(self._spec_path)
            if data is None:
                # Load failed. Keep last good tools if any; else degrade.
                if not self._tools:
                    log.warning("Serving degraded tool specs (spec at %s not usable)", self._spec_path)
                    self._tools = {n: _degraded_spec(n, h) for n, h in self._handlers.items()}
                return

            built: dict[str, ToolSpec] = {}
            degraded = 0
            for name, handler in self._handlers.items():
                entry = data.get(name)
                if entry is not None and _valid_spec_entry(entry):
                    built[name] = ToolSpec(
                        name=name,
                        description=entry["description"],
                        input_schema=entry["input_schema"],
                        handler=handler,
                    )
                else:
                    built[name] = _degraded_spec(name, handler)
                    degraded += 1
            # Spec entries that name no real handler can't be called — warn and skip.
            for orphan in (n for n in data if n not in self._handlers):
                log.warning("Tool spec entry %r at %s has no matching handler — ignored", orphan, self._spec_path)
            self._tools = built
            if degraded:
                log.warning(
                    "Loaded %d tool specs from %s (%d degraded — missing/invalid entry)",
                    len(built), self._spec_path, degraded,
                )
            else:
                log.info("Loaded %d tool specs from %s", len(built), self._spec_path)

    def list_tools(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        disabled = {name.lower() for name in self.settings.disabled_tools}
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in self._tools.values()
            if tool.name == "authenticate" or tool.name.lower() not in disabled
        ]

    async def call(self, name: str, arguments: dict[str, Any], session: SessionContext | None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return _error(f'Unknown tool "{name}".')
        if name != "authenticate" and name.lower() in {item.lower() for item in self.settings.disabled_tools}:
            return _error(f'Tool "{name}" is disabled.')
        log.debug("Calling tool %s with args %s", name, list(arguments.keys()))
        try:
            result = await tool.handler(arguments, ToolContext(settings=self.settings, session=session))
        except Exception as exc:  # noqa: BLE001 — convert any handler crash / SiebelError into a structured result
            return _error_result(exc, name)
        if result.get("isError"):
            log.warning("Tool %s returned isError — content: %s", name, result.get("content"))
        return result


def _build_handlers() -> dict[str, ToolHandler]:
    """Runtime handler map (tool name → callable), projected from
    ``_build_tool_metadata()``. The registry uses only this at runtime —
    metadata (description/input_schema) comes from the spec JSON, never from
    Python."""
    return {name: spec.handler for name, spec in _build_tool_metadata().items()}


def _build_tool_metadata() -> dict[str, ToolSpec]:
    """Authoring source for the bundled tool-spec JSON.

    NOT used at runtime — ``ToolRegistry`` reads metadata from the spec file.
    This is the input to ``scripts/dump_tool_specs.py``, which bakes each tool's
    description and base input_schema (built via ``_schema()`` +
    ``FIELD_DESCRIPTIONS``) into ``tool_specs.json``. Re-run the generator after
    editing anything here; the bundled JSON is what clients see.
    """
    specs = [
        ToolSpec(
            "authenticate",
            "Authenticate or register a Siebel user for this session.",
            _schema("username", "password", required=("username", "password")),
            _authenticate,
        ),
        ToolSpec(
            "list_business_objects",
            "List all available Siebel Business Objects.",
            {"type": "object", "properties": {}},
            _list_business_objects,
        ),
        ToolSpec(
            "describe_business_object",
            "Get field schema for a Siebel Business Object/Component.",
            _schema("business_object", "business_component", required=("business_object",)),
            _describe_business_object,
        ),
        ToolSpec(
            "describe_business_object_json",
            "REST data API field list for a BC as JSON — the navigator's result-list columns (reliable; no LOV types).",
            _schema("business_object", "business_component", required=("business_object",)),
            _describe_business_object_json,
        ),
        ToolSpec(
            "describe_repository",
            (
                "Query Siebel repository metadata for the full field list of a Business Component. "
                "Returns richer data than describe_business_object: pre/post default values, LOV "
                "picklist types, calculated field flags, join info, and fields that are valid in "
                "searchspec/sort_spec but not returned by the REST data API. Always call this when "
                "you need fields not listed by describe_business_object, before create/update to "
                "check required field defaults, or when unsure about a field name or picklist values."
            ),
            _schema("business_object", "business_component", required=("business_object",)),
            _describe_repository,
        ),
        ToolSpec(
            "list_business_objects_json",
            "List all available Siebel Business Objects as structured JSON (for the CRM navigator).",
            {"type": "object", "properties": {}},
            _list_business_objects_json,
        ),
        ToolSpec(
            "describe_repository_json",
            "Field metadata for a BC as JSON (name, type, LOV type, flags) — for the navigator's filter chips.",
            _schema("business_object", "business_component", required=("business_object",)),
            _describe_repository_json,
        ),
        ToolSpec(
            "query_data",
            "Query Siebel CRM data using a Siebel searchspec expression.",
            _schema(
                "business_object",
                "business_component",
                "searchspec",
                "fields",
                "sort_spec",
                "page_size",
                "offset",
                required=("business_object",),
            ),
            _query_data,
        ),
        ToolSpec(
            "query_records_json",
            "Query records and return structured JSON.",
            _schema(
                "business_object",
                "business_component",
                "searchspec",
                "fields",
                "sort_spec",
                "page_size",
                "offset",
                "childlinks",
                required=("business_object",),
            ),
            _query_records_json,
        ),
        ToolSpec(
            "count_records",
            "Count records matching an optional searchspec.",
            _schema("business_object", "business_component", "searchspec", required=("business_object",)),
            _count_records,
        ),
        ToolSpec(
            "aggregate_data",
            "Compute sum, avg, min, max, or count across all matching records.",
            _schema(
                "business_object",
                "business_component",
                "field",
                "operation",
                "searchspec",
                required=("business_object", "field", "operation"),
            ),
            _aggregate_data,
        ),
        ToolSpec(
            "get_record",
            "Get a single Siebel record by Id.",
            _schema("business_object", "business_component", "id", "fields", required=("business_object", "id")),
            _get_record,
        ),
        ToolSpec(
            "get_record_with_children",
            "Get one record with embedded child records.",
            _schema("business_object", "business_component", "id", required=("business_object", "id")),
            _get_record_with_children,
        ),
        ToolSpec(
            "create_record",
            "Create a new Siebel record.",
            _schema("business_object", "business_component", "data", required=("business_object", "data")),
            _create_record,
        ),
        ToolSpec(
            "create_record_json",
            "Create a new Siebel record and return it as structured JSON (the new Id is in the result).",
            _schema("business_object", "business_component", "data", required=("business_object", "data")),
            _create_record_json,
        ),
        ToolSpec(
            "update_record",
            "Update an existing Siebel record by Id.",
            _schema("business_object", "business_component", "id", "data", required=("business_object", "id", "data")),
            _update_record,
        ),
        ToolSpec(
            "delete_record",
            "Delete a Siebel record by Id.",
            _schema("business_object", "business_component", "id", required=("business_object", "id")),
            _delete_record,
        ),
        ToolSpec(
            "query_child_records",
            "Query child records under a parent record.",
            _schema(
                "business_object",
                "business_component",
                "parent_id",
                "child_business_component",
                "searchspec",
                "fields",
                "sort_spec",
                "page_size",
                "offset",
                required=("business_object", "parent_id", "child_business_component"),
            ),
            _query_child_records,
        ),
        ToolSpec(
            "query_child_records_json",
            "Query child records and return structured JSON.",
            _schema(
                "business_object",
                "business_component",
                "parent_id",
                "child_business_component",
                "searchspec",
                "fields",
                "sort_spec",
                "page_size",
                "offset",
                required=("business_object", "parent_id", "child_business_component"),
            ),
            _query_child_records_json,
        ),
        ToolSpec(
            "create_child_record",
            "Create child record(s) under a parent record.",
            _schema(
                "business_object",
                "business_component",
                "parent_id",
                "child_business_component",
                "data",
                required=("business_object", "parent_id", "child_business_component", "data"),
            ),
            _create_child_record,
        ),
        ToolSpec(
            "upload_attachment",
            "Upload a base64 file attachment to a Siebel record.",
            _schema(
                "business_object",
                "business_component",
                "parent_id",
                "attachment_bc",
                "file_name",
                "file_ext",
                "file_content_base64",
                "comment",
                required=(
                    "business_object",
                    "parent_id",
                    "attachment_bc",
                    "file_name",
                    "file_ext",
                    "file_content_base64",
                ),
            ),
            _upload_attachment,
        ),
        ToolSpec(
            "get_list_of_values",
            "Fetch Siebel List of Values for LOV type codes.",
            _schema("lov_types", required=("lov_types",)),
            _get_list_of_values,
        ),
        ToolSpec(
            "get_query_syntax",
            "Return Siebel Query Language syntax reference.",
            _schema("user_question", required=("user_question",)),
            _get_query_syntax,
        ),
        ToolSpec(
            "calculate_date",
            "Resolve a natural-language date expression to MM/DD/YYYY.",
            _schema("expression", required=("expression",)),
            _calculate_date,
        ),
        ToolSpec(
            "web_search",
            "Search the web using Brave Search when configured.",
            _schema("query", "count", required=("query",)),
            _web_search,
        ),
    ]
    return {spec.name: spec for spec in specs}


async def _authenticate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session
    if session is None:
        return _error("No session was provided.")
    username = str(args.get("username") or "")
    password = args.get("password")
    if not username:
        return _error("Authentication failed: username is required.")
    if ctx.settings.demo_enabled:
        session.username = username
        session.demo_authenticated = True
        return _text(f"Demo mode active for {username}. All data shown is sample data.")
    if ctx.settings.mcp_mode == "standalone" and not password:
        return _error("Authentication failed: password is required in standalone mode.")

    # The persistent client is always the SSO (header-based) client — every
    # subsequent data call authenticates via the SSO header, not the password —
    # so the SSO path is what actually determines whether data calls will work.
    client = _sso_client(ctx.settings, username)

    if ctx.settings.mcp_mode == "standalone":
        # Probe the SSO identity with a real data query and treat ANY failure as
        # auth failure. The catalogue (/data/describe) is guest-accessible on
        # some Siebel instances and can't validate the identity — a bogus header
        # still gets a 200 there — so we query the configured probe BO/BC: a 200
        # means the SSO header is trusted, any error means it isn't. (The old
        # Basic probe would "proceed anyway" on a 500 and report a false
        # "Authenticated".)
        try:
            await client.query(
                ctx.settings.probe_business_object,
                ctx.settings.probe_business_component,
                page_size=1,
            )
        except SiebelError as exc:
            log.warning(
                "SSO probe failed for '%s' (BO/BC %s/%s): HTTP %s — %s",
                username,
                ctx.settings.probe_business_object,
                ctx.settings.probe_business_component,
                exc.status_code,
                exc.response_body[:200] if exc.response_body else "",
            )
            return _error(f"Authentication failed: could not establish a Siebel session for '{username}'.")
        except Exception as exc:  # noqa: BLE001 — transport errors aren't SiebelError
            log.warning("SSO probe error for '%s': %s", username, exc)
            return _error(f"Authentication failed: could not reach Siebel for '{username}'.")

    session.username = username
    session.client = client
    session.cache = SchemaCache(client)
    return _text(f"Authenticated as {username}. You can now use all Siebel tools.")


async def _list_business_objects(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    objects = await auth.cache.get_business_objects()
    return _text(f"Available Business Objects ({len(objects)}):\n" + "\n".join(f"  - {item}" for item in objects))


async def _describe_business_object(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _text(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)
    try:
        descriptor = await auth.cache.get_descriptor(resolved, bc)
    except SiebelError as exc:
        # The requested BC isn't part of `resolved`'s Integration Object —
        # most often the model described a *child* component, e.g.
        # describe_business_object(Account, Contact). Contact lives in its
        # own Business Object, so retry describing it under its own BO
        # instead of returning a 50257 "not mapped" error. Skip the retry
        # when the BC is the BO itself (nothing to fall back to).
        if "SBL-EAI-50257" not in exc.response_body or bc.lower() == resolved.lower():
            return _error_result(exc, "describe_business_object")
        own_bo = await auth.cache.resolve_business_object(bc)
        if not own_bo or own_bo.lower() == resolved.lower():
            return _error_result(exc, "describe_business_object")
        try:
            descriptor = await auth.cache.get_descriptor(own_bo, bc)
        except SiebelError as retry_exc:
            return _error_result(retry_exc, "describe_business_object")
        # The describe actually ran under the BC's own BO; reflect that in
        # the header so the field list isn't mislabelled as the parent BO's.
        resolved = own_bo
    return _text(
        f"{resolved} / {bc}\n\n{auth.cache.field_summary(descriptor)}\n\n"
        f"Total fields: {len(descriptor.fields)}"
    )


async def _describe_business_object_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Structured-JSON variant of describe_business_object — the REST data API
    field list (the same source the dashboard render uses successfully). More
    reliable than describe_repository on instances where the workspace metadata
    API + Repository BC fallback return nothing. Used by the CRM navigator for
    the result-list columns + the name field. No LOV types here — those come
    from describe_repository_json (best-effort) where available."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _error(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)
    try:
        descriptor = await auth.cache.get_descriptor(resolved, bc)
    except SiebelError as exc:
        return _error_result(exc, "describe_business_object_json")
    return _json_text(
        {
            "business_object": resolved,
            "business_component": bc,
            "fields": [
                {
                    "name": f.name,
                    "type": f.type,
                    "required": f.required,
                    "read_only": f.read_only,
                    "max_length": f.max_length,
                }
                for f in descriptor.fields
            ],
        }
    )


async def _describe_repository(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _text(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)

    fields = await auth.client.describe_repository_bc(resolved, bc)
    if not fields:
        return _text(
            f'No repository metadata found for {resolved}/{bc}. '
            "The workspace metadata API may not be accessible. "
            "Use describe_business_object for the REST field list instead."
        )

    lines = [f"Repository metadata: {resolved} / {bc}", f"Total fields: {len(fields)}", ""]
    for f in sorted(fields, key=lambda x: str(x.get("Name", ""))):
        name = f.get("Name", "")
        ftype = f.get("Type", "")
        length = f.get("Length") or f.get("length") or ""
        flags = []
        if str(f.get("Required", "")).upper() in ("Y", "TRUE", "1"):
            flags.append("Required")
        if str(f.get("ReadOnly", "")).upper() in ("Y", "TRUE", "1"):
            flags.append("ReadOnly")
        if str(f.get("Calculated", "")).upper() in ("Y", "TRUE", "1"):
            flags.append("Calculated")
        pre = f.get("PreDefaultValue") or ""
        post = f.get("PostDefaultValue") or ""
        lov = f.get("LOVType") or f.get("LOV Type") or ""
        join = f.get("JoinName") or f.get("Join") or ""

        detail_parts = []
        if length:
            detail_parts.append(f"len={length}")
        if flags:
            detail_parts.append(", ".join(flags))
        if lov:
            detail_parts.append(f"LOV={lov}")
        if join:
            detail_parts.append(f"Join={join}")
        if pre:
            detail_parts.append(f"PreDefault={pre!r}")
        if post:
            detail_parts.append(f"PostDefault={post!r}")

        detail = f"  [{', '.join(detail_parts)}]" if detail_parts else ""
        lines.append(f"  {name} ({ftype}){detail}")

    return _text("\n".join(lines))


async def _list_business_objects_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Structured-JSON variant of list_business_objects. The prose variant
    renders a text list, which is fine for the LLM but not for a caller that
    needs the array."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    objects = await auth.cache.get_business_objects()
    return _json_text({"business_objects": objects})


async def _describe_repository_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Structured-JSON variant of describe_repository. Returns per-field name,
    type, LOV type (so the caller can fetch LOV values), and flags — not the
    prose field list the LLM-oriented variant renders."""
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    bo = str(args["business_object"])
    resolved = await auth.cache.resolve_business_object(bo)
    if not resolved:
        return _error(f'Business Object "{bo}" not found. Use list_business_objects to see available options.')
    bc = str(args.get("business_component") or resolved)
    fields = await auth.client.describe_repository_bc(resolved, bc)
    return _json_text(
        {
            "business_object": resolved,
            "business_component": bc,
            "fields": [
                {
                    "name": f.get("Name", ""),
                    "type": f.get("Type", ""),
                    "lov_type": f.get("LOVType") or f.get("LOV Type") or "",
                    "required": str(f.get("Required", "")).upper() in ("Y", "TRUE", "1"),
                    "read_only": str(f.get("ReadOnly", "")).upper() in ("Y", "TRUE", "1"),
                    "calculated": str(f.get("Calculated", "")).upper() in ("Y", "TRUE", "1"),
                    "max_length": f.get("Length") or f.get("length"),
                }
                for f in fields
            ],
        }
    )


async def _query_data(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await _query(args, ctx)
    if isinstance(result, dict) and "items" not in result:
        return result
    rows = [
        f"Record {result['offset'] + idx + 1}:\n{_format_record(item)}"
        for idx, item in enumerate(result["items"])
    ]
    total = result.get("totalCount")
    count_info = f"Total: {total}" if total is not None else f"Returned: {len(result['items'])}"
    more = (
        f"Has more: YES - call again with offset={result.get('nextStartRow')}"
        if result.get("hasMore")
        else "Has more: NO (end of results)"
    )
    return _text(
        f"{count_info} | Page size: {result['pageSize']} | {more}\n\n"
        + ("\n\n".join(rows) or "(no records found)")
    )


async def _query_records_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await _query(args, ctx)
    return result if "items" not in result else _json_text(result)


async def _count_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = {**args, "page_size": 1}
    result = await _query(args, ctx)
    if "items" not in result:
        return result
    total = result.get("totalCount")
    return _text(f"Total records: **{total}**" if total is not None else "Count not available.")


async def _aggregate_data(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved = await auth.cache.resolve_business_object(str(args["business_object"]))
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    bc = str(args.get("business_component") or resolved)
    field = str(args["field"])
    operation = str(args["operation"])
    if operation not in {"sum", "avg", "min", "max", "count"}:
        return _error("operation must be one of: sum, avg, min, max, count.")
    offset = 0
    record_count = 0
    values: list[float] = []
    truncated = False
    for page in range(ctx.settings.aggregate_max_pages):
        try:
            result = await auth.client.query(
                resolved,
                bc,
                search_spec=args.get("searchspec"),
                fields=[field] if operation != "count" else None,
                page_size=ctx.settings.aggregate_page_size,
                offset=offset,
            )
        except SiebelError as exc:
            if exc.status_code == 404:
                break
            return _error_result(exc)
        record_count += len(result.items)
        if operation != "count":
            for item in result.items:
                try:
                    values.append(float(item[field]))
                except (KeyError, TypeError, ValueError):
                    pass
        if not result.has_more or result.next_start_row is None:
            break
        offset = result.next_start_row
    else:
        truncated = True
    if operation == "count":
        value: float = float(record_count)
    elif not values:
        return _text(f'No numeric values found in field "{field}" across {record_count} records.')
    elif operation == "sum":
        value = sum(values)
    elif operation == "avg":
        value = sum(values) / len(values)
    elif operation == "min":
        value = min(values)
    else:
        value = max(values)
    suffix = f"\nResult truncated after {ctx.settings.aggregate_max_pages} pages." if truncated else ""
    rendered = f"{int(value):,}" if value.is_integer() else f"{value:,.2f}"
    return _text(f"{resolved}/{bc}.{field} {operation}: **{rendered}**\nRecords processed: {record_count}{suffix}")


# Cap on the fallback's field list. Asking for every described field can itself
# be rejected, so take a workable subset — chosen by usefulness, not by
# descriptor order (see _rank_fallback_fields).
_MAX_FALLBACK_FIELDS = 40

# Who/what the record is: without these it is unidentifiable.
_IDENTIFYING_FIELD_HINTS = ("name", "number", "abstract", "subject", "title")
# How to reach the person. Kept as its own tier above the qualifying fields —
# lumped together, broad hints like "account" and "date" matched filler
# (Account Currency Code, Birth Date) and crowded out the email and phone.
_CONTACT_FIELD_HINTS = ("email", "phone", "mobile", "cellular")
# Where the record is. Its own tier because an address is only useful whole:
# with "city"/"country" as mere qualifiers, street, state and postal code sat
# unhinted and dropped out, and a contact came back with no address at all.
# Checked after the contact hints so "Email Address" stays a contact detail.
_ADDRESS_FIELD_HINTS = ("street", "address", "city", "state", "postal", "zip", "province", "country")
# Useful qualifiers once the record is identified, reachable and located.
_QUALIFYING_FIELD_HINTS = (
    "status",
    "type",
    "stage",
    "priority",
    "owner",
    "revenue",
    "close date",
    "account",
)
# Yes/no flags and foreign keys. Siebel names them like the real thing —
# "Suppress All Emails" and "Email SR Updates Flag" both read as contact
# details to a keyword match — but nobody asks for them, and the prompt is
# forbidden from displaying Ids at all. The DTYPE says what a name cannot.
_LOW_VALUE_FIELD_TYPES = ("DTYPE_BOOL", "DTYPE_ID")


def _rank_fallback_fields(
    names: list[str], limit: int = _MAX_FALLBACK_FIELDS, types: dict[str, str] | None = None
) -> list[str]:
    """Pick the most useful `limit` field names.

    Siebel describes fields alphabetically, so taking a plain prefix returns
    whatever sorts early — for Contact that was Accomplishments, Account
    Currency Code and Active Status, with Last Name and Email Address falling
    outside the cap entirely. Rank by what identifies a record instead, keeping
    descriptor order within a tier so results stay predictable.

    `types` maps field name → Siebel DTYPE where known; it demotes flags and
    foreign keys that a name-only ranking mistakes for content.
    """
    types = types or {}

    def tier(name: str) -> int:
        n = name.lower()
        if n == "id":
            return 0
        if types.get(name, "").upper() in _LOW_VALUE_FIELD_TYPES:
            return 6
        if any(h in n for h in _IDENTIFYING_FIELD_HINTS):
            return 1
        if any(h in n for h in _CONTACT_FIELD_HINTS):
            return 2
        if any(h in n for h in _ADDRESS_FIELD_HINTS):
            return 3
        if any(h in n for h in _QUALIFYING_FIELD_HINTS):
            return 4
        return 5

    # sorted() is stable, so equal tiers keep their original order.
    return sorted(names, key=tier)[:limit]


# Read fields per Business Component, ordered the way someone reads a record:
# who it is, then how to reach them, where they are, and the qualifying detail.
# For the objects we know, naming the fields beats ranking 190 keyword-scored
# names — the ranking is a heuristic and the demo instance kept proving it
# (a Contact came back with two opt-out flags and no address).
#
# Mirrors a caller-side preferred-fields list, which does
# the same job for the navigator. Treated as an ordered *preference list*,
# intersected with the fields the BC actually reports: naming a field this
# deployment lacks must cost one field, never the whole read (Siebel answers
# HTTP 400 for an unknown field). Contact's names are confirmed against the
# demo instance's describe output; the rest are standard Siebel names.
_PREFERRED_FIELDS: dict[str, tuple[str, ...]] = {
    "Contact": (
        "Last Name",
        "First Name",
        "Middle Name",
        "Job Title",
        "Primary Account Name",
        "Work Phone #",
        "Cellular Phone #",
        "Home Phone #",
        "Fax Phone #",
        "Email Address",
        "Primary Street Address",
        "Primary City",
        "Primary State",
        "Primary Postal Code",
        "Primary Country",
        "Status",
        "Comment",
    ),
    "Account": (
        "Name",
        "Account Status",
        "Type",
        "Main Phone Number",
        "Main Fax Number",
        "Home Page",
        "Primary Account Street Address",
        "Primary Account City",
        "Primary Account State",
        "Primary Account Postal Code",
        "Primary Account Country",
        "Primary Organization",
        "Annual Revenue",
        "Employees",
    ),
    "Opportunity": (
        "Name",
        "Account",
        "Sales Stage",
        "Primary Revenue Amount",
        "Primary Revenue Close Date",
        "Win Probability",
        "Primary Organization",
        "Sales Method",
        "Description",
    ),
    "Service Request": (
        "SR Number",
        "Abstract",
        "Status",
        "Substatus",
        "Priority",
        "Severity",
        "Account",
        "Contact Last Name",
        "Contact First Name",
        "Owner",
        "Description",
    ),
}


def _read_fields(business_component: str, described: list[Any]) -> list[str]:
    """The field list to read a whole record with.

    Curated order where the object is known, ranked heuristics otherwise — and
    ranked too when a curated list matches nothing, so a deployment that
    renamed its fields still gets a usable record instead of an empty read.
    """
    available = {f.name for f in described if f.name}
    curated = [f for f in _PREFERRED_FIELDS.get(business_component, ()) if f in available]
    if curated:
        # Follow-up tool calls need the Id even though the prompt never shows it.
        return [*curated, "Id"] if "Id" in available else curated
    return _rank_fallback_fields(
        [f.name for f in described if f.name], types={f.name: f.type for f in described if f.name}
    )


def _ordered_record(record: SiebelRecord, fields: Any) -> SiebelRecord:
    """Reorder a record to the field order it was requested in.

    Siebel returns fields in its own order — the read that prompted this put
    "Suppress All Emails" first and the name fourth. The system prompt tells
    the model to report fields in the order they came back, so that order has
    to be worth following. Anything Siebel returned but we didn't ask for
    keeps its place at the end rather than being dropped.
    """
    if not fields:
        return record
    wanted = [f.strip() for f in (fields.split(",") if isinstance(fields, str) else fields)]
    ordered = {name: record[name] for name in wanted if name in record}
    ordered.update({k: v for k, v in record.items() if k not in ordered})
    return ordered


async def _record_via_query(
    auth: Any, business_object: str, business_component: str, record_id: str, fields: Any
) -> Any:
    """Fetch a single record by Id with an explicit field list.

    The direct `/data/{BO}/{BC}/{id}` read asks Siebel for the whole record. On
    an instance whose integration component names a field the business
    component doesn't define, Siebel 500s while serialising it — e.g. Contact
    referencing 'Primary Address Integration Id' (SBL-DAT-00398). Naming the
    fields sidesteps the broken one, which is why the navigator can display a
    record the direct read cannot return.
    """
    if not fields:
        descriptor = await auth.client.describe_bc(business_object, business_component)
        usable = [f for f in descriptor.fields if f.name and not _is_audit_field(f.name)]
        fields = _read_fields(business_component, usable)
    safe_id = str(record_id).replace("'", "''")
    result = await auth.client.query(
        business_object,
        business_component,
        search_spec=f"[Id]='{safe_id}'",
        fields=fields,
        page_size=1,
    )
    items = getattr(result, "items", None) or []
    return _ordered_record(items[0], fields) if items else None


def _is_audit_field(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in ("created", "updated", "last upd", "conflict", "integration id"))


async def _get_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    bad_fields = _validate_fields_arg(args.get("fields"))
    if bad_fields:
        return _error(bad_fields)
    fields = args.get("fields")
    try:
        record = await auth.client.get_record(resolved, bc, str(args["id"]), fields)
    except SiebelError as exc:
        # A 404 already comes back as None, so an exception here means the read
        # itself failed. Retry naming the fields before giving up: a
        # misconfigured integration component shouldn't make a record
        # unreadable when a scoped query can read it. If that fails too, the
        # original Siebel error propagates — it names the offending field,
        # which is what an administrator needs.
        log.warning("get_record direct read failed (%s); retrying as a scoped query", exc)
        record = await _record_via_query(auth, resolved, bc, str(args["id"]), fields)
        if record is None:
            raise
    if not record:
        return _text("No record found.")
    return _text(f"{resolved}/{bc} record {args['id']}:\n{_format_record(record)}")


async def _get_record_with_children(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    record = await auth.client.get_record_with_children(resolved, bc, str(args["id"]))
    if not record:
        return _text("No record found.")
    return _text(f"{resolved}/{bc} record {args['id']}:\n{_format_hierarchical_record(record)}")


async def _create_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    data = args.get("data")
    if not isinstance(data, dict) or not data:
        return _error('The "data" parameter is required and must be a non-empty object.')
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    result = await auth.client.create_record(resolved, bc, data)
    return _text(f"Created record in {resolved}/{bc}:\n{_format_record(result)}")


async def _create_record_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The JSON-returning twin of _create_record — same create, but returns
    _json_text(result) so a caller can read the new record's Id
    programmatically (mirrors query_records_json vs query_data)."""
    data = args.get("data")
    if not isinstance(data, dict) or not data:
        return _error('The "data" parameter is required and must be a non-empty object.')
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    result = await auth.client.create_record(resolved, bc, data)
    return _json_text(result)


async def _update_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    result = await auth.client.update_record(resolved, bc, str(args["id"]), args["data"])
    return _text(f"Updated record {args['id']} in {resolved}/{bc}:\n{_format_record(result)}")


async def _delete_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    await auth.client.delete_record(resolved, bc, str(args["id"]))
    return _text(f"Deleted record {args['id']} from {resolved}/{bc}.")


async def _query_child_records(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await _query_child(args, ctx)
    if "items" not in result:
        return result
    rows = [f"Record {result['offset'] + idx + 1}:\n{_format_record(item)}" for idx, item in enumerate(result["items"])]
    return _text("\n\n".join(rows) or "(no child records found)")


async def _query_child_records_json(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = await _query_child(args, ctx)
    return result if "items" not in result else _json_text(result)


async def _create_child_record(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    result = await auth.client.create_child_record(
        resolved, bc, str(args["parent_id"]), str(args["child_business_component"]), args["data"]
    )
    return _text(f"Created child record(s) under {resolved}/{bc}/{args['parent_id']}:\n{_format_record(result)}")


async def _upload_attachment(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    descriptor = await auth.cache.get_descriptor(resolved, str(args["attachment_bc"]))
    names = [field.name for field in descriptor.fields]
    file_name_field = next((name for name in names if name.endswith("FileName")), None)
    file_ext_field = next((name for name in names if name.endswith("FileExt")), None)
    if not file_name_field or not file_ext_field:
        return _error(f"Could not find FileName/FileExt fields in {args['attachment_bc']}.")
    await auth.client.upload_attachment(
        resolved,
        bc,
        str(args["parent_id"]),
        str(args["attachment_bc"]),
        file_name_field,
        file_ext_field,
        str(args["file_name"]),
        str(args["file_ext"]),
        str(args["file_content_base64"]),
        args.get("comment"),
    )
    return _text(f"Attachment uploaded successfully: {args['file_name']}.{args['file_ext']}")


async def _get_list_of_values(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    lov_types = args.get("lov_types") or []
    inputs = {
        "body": {
            "ListsTopElementQuery": {
                "MessageId": "",
                "MessageType": "Integration Object",
                "IntObjectName": "List Query",
                "IntObjectFormat": "Siebel Hierarchical",
                "ListOfList Query": {
                    "List": [
                        {"Active": "Y", "Language Code": ctx.settings.language_code, "Type": lov_type}
                        for lov_type in lov_types
                    ]
                },
            }
        }
    }
    response = await auth.client.call_business_service("EAILOVService", "GetListOfValues", inputs)
    entries = (((response or {}).get("ListsTopElementResult") or {}).get("List") or [])
    result: dict[str, list[str]] = {lov_type: [] for lov_type in lov_types}
    for entry in entries:
        values = (entry.get("List Value") or []) if isinstance(entry, dict) else []
        result[str(entry.get("Type"))] = [
            str(value.get("Display Value", "")) for value in values if isinstance(value, dict)
        ]
    return _json_text(result)


async def _get_query_syntax(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return _text(
        "Use <> for not equal; never use !=.\n"
        "Field names go in square brackets: [Status] = 'Open'.\n"
        "Use LIKE with * wildcards: [Name] LIKE 'Acme*'.\n"
        "Combine with AND/OR and parenthesize compound expressions.\n"
        "Dates may use Today(), Timestamp(), and arithmetic such as Today()-30.\n"
        "Siebel may return 404 when a query has no records."
    )


async def _calculate_date(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    expr = str(args.get("expression", "")).strip().lower()
    today = date.today()
    weekdays = {"sunday": 6, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5}
    result: date | None = None
    if expr == "today":
        result = today
    elif expr == "tomorrow":
        result = today + timedelta(days=1)
    elif expr == "yesterday":
        result = today - timedelta(days=1)
    elif expr.startswith("next "):
        target = weekdays.get(expr.removeprefix("next "))
        if target is not None:
            result = today + timedelta(days=((target - today.weekday()) % 7) or 7)
    elif expr.startswith("in ") and expr.endswith(" days"):
        result = today + timedelta(days=int(expr.split()[1]))
    elif expr == "end of month":
        result = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if result is None:
        return _error(f'Could not parse date expression: "{args.get("expression")}".')
    return _text(result.strftime("%m/%d/%Y (%A)"))


async def _web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not ctx.settings.brave_search_api_key:
        return _error("Web search is not configured. Set BRAVE_SEARCH_API_KEY.")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": args["query"], "count": int(args.get("count") or 5)},
            headers={"Accept": "application/json", "X-Subscription-Token": ctx.settings.brave_search_api_key},
        )
    if response.status_code >= 400:
        return _error(f"Brave Search API error: {response.status_code}")
    results = ((response.json().get("web") or {}).get("results") or [])
    lines = []
    for item in results:
        lines.extend([f"**{item.get('title')}**", f"URL: {item.get('url')}", str(item.get("description") or ""), ""])
    return _text("\n".join(lines) or f'No results found for: "{args["query"]}"')


_FIELD_ERROR_CODES = {"SBL-EAI-50258", "SBL-EAI-50297"}


def _is_field_error(exc: SiebelError) -> bool:
    return exc.status_code == 500 and any(code in exc.response_body for code in _FIELD_ERROR_CODES)


async def _query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    # Reject placeholder field tokens (e.g. '<?>') the LLM emits when it
    # doesn't know real field names — sending them to Siebel wastes a 50258
    # round-trip and the subsequent retry hits Siebel's (sometimes broken)
    # default field set. Tell the model to describe the BC first instead.
    bad_fields = _validate_fields_arg(args.get("fields"))
    if bad_fields:
        return _error(bad_fields)
    try:
        result = await auth.client.query(
            resolved,
            bc,
            search_spec=args.get("searchspec"),
            fields=args.get("fields"),
            sort_spec=args.get("sort_spec"),
            page_size=int(args.get("page_size") or 20),
            offset=int(args.get("offset") or 0),
            childlinks=args.get("childlinks"),
        )
    except SiebelError as exc:
        if exc.status_code == 404:
            return _empty_result(args)
        if _is_field_error(exc) and args.get("fields"):
            # Retry with a known-safe field list instead of `None`. Omitting
            # `fields` makes Siebel return its *default* Integration-Object
            # field set, which can reference fields that don't exist on the
            # BC (e.g. 'Primary Address Integration Id' on Contact →
            # SBL-DAT-00398). 'Id' is universal, so the query still succeeds
            # with a minimal column instead of 500-ing on bad defaults.
            log.warning("Field error on query — retrying with minimal field list (Id)")
            try:
                result = await auth.client.query(
                    resolved, bc,
                    search_spec=args.get("searchspec"),
                    fields=["Id"],
                    sort_spec=args.get("sort_spec"),
                    page_size=int(args.get("page_size") or 20),
                    offset=int(args.get("offset") or 0),
                    childlinks=args.get("childlinks"),
                )
                return _result_dict(result)
            except SiebelError as retry_exc:
                return _error_result(retry_exc)
        return _error_result(exc)
    return _result_dict(result)


async def _query_child(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    resolved, bc = await _resolve_bo_bc(args, auth)
    if not resolved:
        return _text(f'Business Object "{args["business_object"]}" not found.')
    # Same placeholder guard as _query — see there for rationale.
    bad_fields = _validate_fields_arg(args.get("fields"))
    if bad_fields:
        return _error(bad_fields)
    try:
        result = await auth.client.query_child_records(
            resolved,
            bc,
            str(args["parent_id"]),
            str(args["child_business_component"]),
            search_spec=args.get("searchspec"),
            fields=args.get("fields"),
            sort_spec=args.get("sort_spec"),
            page_size=int(args.get("page_size") or 20),
            offset=int(args.get("offset") or 0),
        )
    except SiebelError as exc:
        if exc.status_code == 404:
            return _empty_result(args)
        return _error_result(exc)
    return _result_dict(result)


async def _resolve_bo_bc(args: dict[str, Any], auth: Any) -> tuple[str | None, str]:
    resolved = await auth.cache.resolve_business_object(str(args["business_object"]))
    return resolved, str(args.get("business_component") or resolved or args["business_object"])


async def _authenticated_client(
    settings: AppSettings, username: str, password: str
) -> SiebelClient | None:
    """Return a ready-to-use SiebelClient for standalone mode.

    Tries token auth (POST /auth) first — required by Siebel 26.x instances that
    don't accept raw Basic Auth on data endpoints. Falls back to Basic Auth for
    older or differently-configured instances. Returns None if credentials are
    invalid (401/403).
    """
    # Attempt 1: token auth
    token_client = SiebelClient(
        base_url=settings.base_url,
        auth_mode="token",
        username=username,
        password=password,
        page_size=settings.default_page_size,
        request_timeout_ms=settings.request_timeout_ms,
        max_concurrent_requests=settings.max_concurrent_requests,
    )
    try:
        await token_client.login()
        log.info("Siebel authentication succeeded via token auth")
        return token_client
    except SiebelError as exc:
        if exc.status_code in {401, 403}:
            return None  # Bad credentials — no point trying basic
        log.info(
            "Token auth endpoint returned HTTP %s — falling back to Basic Auth",
            exc.status_code,
        )

    # Attempt 2: basic auth — validate with a lightweight probe
    basic_client = _basic_client(settings, username, password)
    try:
        await basic_client.list_business_objects()
        basic_client.page_size = settings.default_page_size
        log.info("Siebel authentication succeeded via Basic Auth")
        return basic_client
    except SiebelError as exc:
        if exc.status_code in {401, 403}:
            return None
        # 500 / other — Siebel may restrict the catalogue endpoint but credentials
        # could still be valid; proceed and let the real tool call surface any error.
        log.warning(
            "Basic Auth probe returned HTTP %s — proceeding anyway: %s",
            exc.status_code,
            exc.response_body[:200] if exc.response_body else "",
        )
        basic_client.page_size = settings.default_page_size
        return basic_client


def _basic_client(settings: AppSettings, username: str, password: str) -> SiebelClient:
    return SiebelClient(
        base_url=settings.base_url,
        auth_mode="basic",
        username=username,
        password=password,
        page_size=1,
        request_timeout_ms=settings.request_timeout_ms,
        max_concurrent_requests=settings.max_concurrent_requests,
    )


def _sso_client(settings: AppSettings, username: str) -> SiebelClient:
    return SiebelClient(
        base_url=settings.base_url,
        auth_mode="sso",
        sso_header_name=settings.sso_header,
        sso_username=username,
        page_size=settings.default_page_size,
        request_timeout_ms=settings.request_timeout_ms,
        max_concurrent_requests=settings.max_concurrent_requests,
    )


def _require_auth(ctx: ToolContext) -> Any:
    if ctx.session and ctx.session.client and ctx.session.cache:
        return ctx.session
    return _error("Not authenticated. Call the authenticate tool first.")


def _result_dict(result: Any) -> dict[str, Any]:
    return {
        "items": result.items,
        "totalCount": result.total_count,
        "hasMore": result.has_more,
        "nextStartRow": result.next_start_row,
        "offset": result.offset,
        "pageSize": result.page_size,
    }


def _empty_result(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": [],
        "totalCount": 0,
        "hasMore": False,
        "offset": int(args.get("offset") or 0),
        "pageSize": int(args.get("page_size") or 20),
    }


def _format_record(record: SiebelRecord) -> str:
    lines = []
    for key, value in record.items():
        if value in (None, ""):
            continue
        if key == "Link" and isinstance(value, list):
            children = [item.get("name") for item in value if isinstance(item, dict) and item.get("rel") == "child"]
            if children:
                lines.append(f"  Children: {', '.join(str(c) for c in children)}")
            continue
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _format_hierarchical_record(record: SiebelRecord) -> str:
    visible = {key: value for key, value in record.items() if not key.startswith("__")}
    return _format_record(visible)


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _json_text(data: Any) -> dict[str, Any]:
    import json

    return _text(json.dumps(data, default=str))


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _error_result(exc: Exception, tool_name: str = "") -> dict[str, Any]:
    if isinstance(exc, SiebelError):
        log.error(
            "Siebel error in tool %s: HTTP %s — %s",
            tool_name,
            exc.status_code,
            exc.response_body[:500] if exc.response_body else "(no body)",
        )
        if exc.status_code == 401:
            return _error("The Siebel session has expired. Refresh the page or log in again.")
    else:
        log.error("Unexpected error in tool %s: %s", tool_name, exc, exc_info=True)
    # isError (not _text) so the agent loop knows the tool call failed and can
    # surface a user-friendly message instead of treating the masked text as a
    # successful result.
    return _error("[INTERNAL - do not display verbatim] The Siebel operation could not be completed.")


# Per-parameter descriptions surfaced to the LLM via the tool inputSchema.
# Every Siebel tool argument is typed as JSON string (required by Claude
# Desktop — see CLAUDE.md), but a bare `{"type":"string"}` gives the model no
# clue what to put in `fields` / `child_business_component`, which is why it
# emitted placeholder tokens like '<?>'. These descriptions guide it, and
# `_schema(required=...)` marks the args a call is meaningless without so the
# model is told up front instead of the tool crashing on `args["..."]`.
FIELD_DESCRIPTIONS: dict[str, str] = {
    "username": "Siebel user name.",
    "password": "Siebel password.",
    "business_object": (
        "Siebel Business Object name, e.g. 'Account', 'Contact', 'Opportunity'. "
        "Call list_business_objects to see all available names."
    ),
    "business_component": (
        "Business Component within the Business Object. Defaults to the Business "
        "Object name itself when omitted."
    ),
    "child_business_component": (
        "Name of a Business Component that is linked as a CHILD of the parent "
        "record under this Business Object — e.g. 'Contact' or 'Opportunity' "
        "under 'Account'. Must be a real child relationship of this Business "
        "Object, not an arbitrary Business Component."
    ),
    "parent_id": "Siebel row Id of the parent record, e.g. '1-5GZE'.",
    "id": "Siebel row Id of the record, e.g. '1-5GZE'.",
    "searchspec": (
        "Optional Siebel search expression, e.g. \"[Name] LIKE 'A*'\" or "
        "'[Id] = \"1-5GZE\"'. Omit to return all rows (paged)."
    ),
    "fields": (
        "Comma-separated list of REAL Siebel field names to return, e.g. "
        "'Name,Status'. Look the valid names up with describe_business_object "
        "first. Omit to return the default field set. NEVER pass placeholders "
        "such as '<?>'; if you are unsure of the field names, call "
        "describe_business_object before querying."
    ),
    "sort_spec": "Optional sort expression, e.g. 'Name' or 'Created DESC'.",
    "page_size": "Page size 1-100 as a string number, e.g. '20'. Default 20.",
    "offset": "Zero-based row offset to start paging from, as a string number, e.g. '100'.",
    "data": 'JSON object of field names to values for create/update, e.g. {"Name": "Acme"}.',
    "operation": "Aggregation operation: one of 'sum', 'avg', 'min', 'max', 'count'.",
    "field": "A real Siebel field name to aggregate over (from describe_business_object).",
    "childlinks": "Comma-separated child Business Component names to expand inline in the response.",
    "attachment_bc": "Business Component for the attachment, e.g. 'Account Attachment'.",
    "file_name": "Attachment file name without extension.",
    "file_ext": "Attachment file extension, e.g. 'pdf'.",
    "file_content_base64": "Attachment file contents, base64-encoded.",
    "comment": "Optional attachment comment.",
    "lov_types": "Comma-separated Siebel LOV type codes, e.g. 'ACCOUNT_STATUS'.",
    "user_question": "Natural-language question about Siebel query syntax.",
    "expression": "Natural-language date expression, e.g. 'today', 'next Monday', 'end of month'.",
    "query": "Web search query string.",
    "count": "Number of web results to return, as a string number, e.g. '5'.",
}

# Tokens the LLM emits as placeholders when it doesn't know a real value.
# Rejecting them up front (instead of forwarding to Siebel) avoids the
# SBL-EAI-50258 "Invalid fields: '<?>'" round-trip and the broken-default
# retry that follows (see _query). Compared case-insensitively.
_PLACEHOLDER_FIELD_TOKENS = {
    "<?>", "?", "??", "unknown", "n/a", "<field>", "<fields>",
    "<name>", "<value>", "<placeholder>", "...",
}


def _validate_fields_arg(raw: Any) -> str | None:
    """Return a guidance error if `fields` contains placeholder tokens the
    LLM emits when it doesn't know real field names; None when the arg is
    absent or looks like real field names."""
    if raw is None:
        return None
    parts = [p.strip() for p in (raw.split(",") if isinstance(raw, str) else raw)]
    bad = [p for p in parts if p and p.lower() in _PLACEHOLDER_FIELD_TOKENS]
    if not bad:
        return None
    return (
        f"'fields' must be real Siebel field names, but got placeholder(s) {bad!r}. "
        "Call describe_business_object first to get valid field names, then pass them "
        "comma-separated — or omit 'fields' to use the default set. Do not guess and "
        "do not pass placeholders like '<?>'."
    )


def _schema(*fields: str, required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build a tool inputSchema. Every property keeps a concrete `type` (a
    Claude-Desktop requirement), now paired with a model-facing description
    and an optional `required` list so the LLM knows what it must supply."""
    properties: dict[str, Any] = {}
    for f in fields:
        prop: dict[str, Any] = {"type": "string"}
        desc = FIELD_DESCRIPTIONS.get(f)
        if desc:
            prop["description"] = desc
        properties[f] = prop
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema
