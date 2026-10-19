"""MCP-style Siebel tool registry.

The registry owns no tool-specific code. Each tool's metadata (description +
input_schema) and its ``kind`` come from ``mcp_siebel/specs/tool_specs.json`` —
the single source of truth, hot-reloaded by mtime. A small set of generic
executors (one per kind) turn a spec entry + the runtime arguments into a tool
result; ``"python"`` is the escape hatch that resolves a handler by importlib
from the entry's ``"handler"`` field (permitting only ``mcp_siebel.handlers``
modules). This file contains no tool names: adding a tool means adding a spec
entry (and, for the python kind, a handler module function), nothing here.

The spec may use a top-level ``$defs`` block with ``$ref`` for shared parameter
definitions; the loader inlines every ``$ref`` before exposing schemas, so
clients always receive a fully-resolved inputSchema. Every reload re-runs the
full pipeline (parse with duplicate detection → deref → validate → per-entry
kind + executor + access/session-rule resolution) and only swaps in the new set
on success; on any failure the last valid set keeps serving and the failure is
logged. If the very first load fails (no last valid set) construction raises
``_SpecError`` so the process exits non-zero with a clear error — the handlers
come from the spec, so without it the set of tools is unknown.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from .config import AppSettings
from .handlers._common import _error, _error_result
from .kinds import _COMMON_FIELDS, _KindSpec, _SpecError

if TYPE_CHECKING:
    # Annotation-only (stringified via ``from __future__ import annotations``):
    # importing them at runtime would work but isn't needed here, and keeping
    # them out of the runtime import graph leaves tools.py dependent only on
    # the handlers._common helpers it actually calls.
    from .schema_cache import SchemaCache
    from .siebel_client import SiebelClient

log = logging.getLogger("mcp-siebel")


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
    # The hot-reloaded ``preferred_fields`` mapping (Business Component name ->
    # ordered field names) from the spec, surfaced to executors and handlers so
    # no module-level global goes stale on reload. Empty when the spec declares
    # no ``preferred_fields`` section. Populated by ``ToolRegistry.call`` from
    # the registry's last-successful-load copy.
    preferred_fields: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    # Access level declared in the spec: "read" tools are always available,
    # "write" tools only when SIEBEL_ALLOW_WRITE is set, "delete" only when
    # SIEBEL_ALLOW_DELETE is set, "external" (tools that send free text to an
    # outside service, e.g. web search) only when SIEBEL_ALLOW_EXTERNAL is set.
    # The registry enforces this gate (both for listing and for call) so adding
    # a write/external tool via JSON can't bypass the read-only default.
    # Authored in the spec, not hard-coded here.
    access: str = "read"
    # True for tools the spec marks ``always_enabled`` — exempt from the
    # disabled-tools filter so a deployer can't brick the server by listing
    # them in SIEBEL_DISABLED_TOOLS (the auth-establishment tool is the one
    # that matters). Authored in the spec, not hard-coded here, so tools.py
    # stays free of tool names. Load-time rule: always_enabled is only allowed
    # with access "read" — it must never bypass the access gate.
    always_enabled: bool = False
    # True for the single entry the spec marks ``establishes_session`` — the
    # tool the transports call to create a session and whose session_id they
    # inject into results/schemas. Exactly one such entry is required, and it
    # must be always_enabled + access "read". Authored in the spec, not
    # hard-coded here, so tools.py stays free of tool names.
    establishes_session: bool = False


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
            # An expired id is treated as not found — callers must establish a new session.
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


def _no_dup_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that rejects duplicate keys anywhere in the spec —
    in particular duplicate tool names under ``tools`` (name uniqueness)."""
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _SpecError(f"duplicate key {key!r}")
        seen.add(key)
    return dict(pairs)


def _deref(node: Any, defs: dict[str, Any], _stack: tuple[str, ...] = ()) -> Any:
    """Inline every local ``$ref`` (``#/$defs/<name>``) in ``node`` against
    ``defs``, returning a plain schema with no ``$ref`` left — MCP clients get a
    fully-resolved inputSchema. External refs are unsupported (they'd need file
    I/O at load time); cycles raise. Definitions are deep-copied so tools never
    share mutable property dicts."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            prefix = "#/$defs/"
            if not isinstance(ref, str) or not ref.startswith(prefix):
                raise _SpecError(f"unsupported $ref {ref!r} (only '#/$defs/<name>' is allowed)")
            name = ref[len(prefix) :]
            if name in _stack:
                raise _SpecError(f"circular $ref: {' -> '.join([*_stack, name])}")
            if name not in defs:
                raise _SpecError(f"unknown $ref target {name!r}")
            return _deref(deepcopy(defs[name]), defs, (*_stack, name))
        return {k: _deref(v, defs, _stack) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(item, defs, _stack) for item in node]
    return node


def _valid_spec_entry(entry: Any) -> bool:
    """Pre-deref structural check of the fields COMMON to every kind: an entry
    must carry a string description, a string ``kind``, a valid ``access``
    level, and an input_schema dict with a type and a properties map.
    ``always_enabled`` and ``establishes_session`` are optional but, if present,
    must be bools. Kind-specific fields (e.g. ``handler`` for python,
    ``scope``/``result``/``render`` for bc_query) are validated per-kind."""
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("description"), str)
        and isinstance(entry.get("input_schema"), dict)
        and isinstance(entry["input_schema"].get("properties"), dict)
        and "type" in entry["input_schema"]
        and isinstance(entry.get("kind"), str)
        and isinstance(entry.get("access"), str)
        and entry.get("access") in _ACCESS_LEVELS
        and ("always_enabled" not in entry or isinstance(entry["always_enabled"], bool))
        and ("establishes_session" not in entry or isinstance(entry["establishes_session"], bool))
    )


def _valid_resolved_schema(schema: Any) -> bool:
    """Post-deref check: the resolved input_schema is an object schema whose
    every property carries a concrete JSON Schema ``type`` (a Claude Desktop
    requirement — a bare ``$ref`` that failed to resolve would leave a property
    without one). Empty properties are fine (some tools take no arguments)."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    props = schema.get("properties")
    if not isinstance(props, dict):
        return False
    return all(isinstance(p, dict) and "type" in p for p in props.values())


def _validate_preferred_fields(raw: Any) -> dict[str, list[str]]:
    """Validate the optional top-level ``preferred_fields`` section: an object
    of Business Component name -> non-empty list of unique strings. Returns the
    coerced mapping (preserving order); raises ``_SpecError`` on any violation.
    A missing section (``None``) is valid and yields an empty mapping."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _SpecError("'preferred_fields' must be an object of Business Component name -> list of field names")
    result: dict[str, list[str]] = {}
    for bc, fields in raw.items():
        if not isinstance(fields, list) or not fields:
            raise _SpecError(f"'preferred_fields' entry for {bc!r} must be a non-empty list of field names")
        if not all(isinstance(f, str) for f in fields):
            raise _SpecError(f"'preferred_fields' entry for {bc!r} must contain only strings")
        if len(set(fields)) != len(fields):
            raise _SpecError(f"'preferred_fields' entry for {bc!r} has duplicate field names")
        result[bc] = list(fields)
    return result


# Access levels a spec entry may declare. ``read`` is always available;
# ``write`` / ``delete`` are gated by SIEBEL_ALLOW_WRITE / SIEBEL_ALLOW_DELETE;
# ``external`` (tools that send free text to an outside service, e.g. web
# search) is gated by SIEBEL_ALLOW_EXTERNAL. All three gates default off.
_ACCESS_LEVELS: set[str] = {"read", "write", "delete", "external"}

# Handler references may only name modules inside this package — the resolver
# rejects anything outside it so a spec can't pull in arbitrary code.
_ALLOWED_HANDLER_PKG = "mcp_siebel.handlers"


# --- Kind registry ---------------------------------------------------------
#
# Each kind declares its executor (assembled once at load time into the
# ToolHandler ToolRegistry.call invokes), the spec options it requires/allows,
# and the access levels it permits. Kind-specific options are validated at load
# time; any violation is a _SpecError (fatal on first load, keep-last-valid on
# reload). The ``python`` kind is the escape hatch for tools with real custom
# logic; generic executors live under ``executors/`` and register here.


def _validate_python(entry: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    options = set(entry.keys()) - _COMMON_FIELDS
    extra = options - {"handler"}
    if extra:
        raise _SpecError(f"unknown option(s) {sorted(extra)} for python entry {name!r}")
    if not isinstance(entry.get("handler"), str) or not entry["handler"]:
        raise _SpecError(f"kind 'python' requires a 'handler' string for {name!r}")


def _assemble_python(entry: dict[str, Any], name: str) -> ToolHandler:
    try:
        return _resolve_handler(entry["handler"])
    except _SpecError as exc:
        raise _SpecError(f"handler error in {name!r}: {exc}") from exc


_KINDS: dict[str, _KindSpec] = {
    "python": _KindSpec(
        name="python",
        allowed_access=frozenset({"read", "write", "delete", "external"}),
        allowed_options=frozenset({"handler"}),
        required_options=frozenset({"handler"}),
        validate=_validate_python,
        assemble=_assemble_python,
    ),
}

# Register the generic executors (imported for their side effect of adding to
# _KINDS; the import is deferred to the bottom of the module to keep the runtime
# import graph acyclic — the executor modules import only kinds/renderers/
# handlers at runtime, not tools).
from .executors.delete import DELETE_KIND  # noqa: E402
from .executors.describe import DESCRIBE_KIND  # noqa: E402
from .executors.query import BC_QUERY_KIND  # noqa: E402
from .executors.read import BC_READ_KIND  # noqa: E402
from .executors.static_text import STATIC_TEXT_KIND  # noqa: E402
from .executors.upsert import UPSERT_KIND  # noqa: E402

_KINDS[BC_QUERY_KIND.name] = BC_QUERY_KIND
_KINDS[UPSERT_KIND.name] = UPSERT_KIND
_KINDS[DELETE_KIND.name] = DELETE_KIND
_KINDS[DESCRIBE_KIND.name] = DESCRIBE_KIND
_KINDS[STATIC_TEXT_KIND.name] = STATIC_TEXT_KIND
_KINDS[BC_READ_KIND.name] = BC_READ_KIND


def _resolve_handler(handler_ref: Any) -> ToolHandler:
    """Resolve a ``"module:func"`` reference to a callable, allowing only
    modules under ``mcp_siebel.handlers``. Raises ``_SpecError`` on anything
    malformed, outside the allowed package, unimportable, or not callable — so
    the registry treats a bad reference as a failed reload and keeps the last
    valid tool set."""
    if not isinstance(handler_ref, str) or ":" not in handler_ref:
        raise _SpecError(f"bad handler reference {handler_ref!r} (expected 'module:func')")
    module_path, _, attr = handler_ref.partition(":")
    if not module_path or not attr:
        raise _SpecError(f"bad handler reference {handler_ref!r}")
    if not (module_path == _ALLOWED_HANDLER_PKG or module_path.startswith(_ALLOWED_HANDLER_PKG + ".")):
        raise _SpecError(f"handler module {module_path!r} is not under {_ALLOWED_HANDLER_PKG}")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise _SpecError(f"could not import handler module {module_path!r}: {exc}") from exc
    try:
        handler = getattr(module, attr)
    except AttributeError as exc:
        raise _SpecError(f"module {module_path!r} has no attribute {attr!r}") from exc
    if not callable(handler):
        raise _SpecError(f"handler {handler_ref!r} is not callable")
    return handler


class ToolRegistry:
    """Holds the tool map and serves metadata + handlers from an external spec.

    The spec is the single source of truth: each entry declares a ``kind`` and a
    ``handler`` reference, and the registry resolves the handler by importlib
    (permitting only ``mcp_siebel.handlers`` modules). The file is hot-reloaded
    by mtime — ``list_tools()`` stats the file and re-reads it only when it
    changed, so edits go live on the next tools/list with no restart. Every
    reload re-runs the full pipeline (parse with duplicate detection → deref →
    validate → per-entry kind + executor resolution) and only swaps in the new
    set on success; on any failure the last valid set keeps serving and the
    failure is logged. If the very first load fails there is no last valid set,
    so construction raises ``_SpecError`` (the process exits non-zero with a
    clear error) rather than serving an unknown tool set.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._spec_path = _resolve_spec_path(settings)
        self._spec_mtime: float | None = None
        self._lock = threading.Lock()
        self._tools: dict[str, ToolSpec] = {}
        # The hot-reloaded preferred_fields mapping (BC name -> ordered field
        # names); swapped in only on a successful reload so a broken file never
        # replaces a good mapping. Surfaced to executors/handlers via ToolContext.
        self._preferred_fields: dict[str, list[str]] = {}
        # Name of the spec's establishes_session tool; set on each successful
        # load. None only when no spec has loaded yet.
        self._session_tool_name: str | None = None
        # True until the first successful load. A failure before then is fatal:
        # without a valid spec the set of tools is unknown, so construction
        # raises and the process exits non-zero with a clear error. After the
        # first good load a reload failure is non-fatal (log + keep last valid).
        self._first_load: bool = True
        self._reload_if_changed()
        # Log once at startup which access levels are enabled — a deployer who
        # forgets to flip a gate hears it here, not from a hidden/refused tool
        # later. ``read`` is always on; the other three follow their flags.
        log.info(
            "Access gates: read=on, write=%s, delete=%s, external=%s",
            "on" if self.settings.allow_write else "off",
            "on" if self.settings.allow_delete else "off",
            "on" if self.settings.allow_external else "off",
        )

    @property
    def session_tool_name(self) -> str:
        """Name of the spec's single establishes_session tool — the one the
        transports call to create a session and whose session_id they inject.
        Set on every successful load; a successful load always has exactly one."""
        assert self._session_tool_name is not None, "no tool spec loaded"
        return self._session_tool_name

    @property
    def preferred_fields(self) -> dict[str, list[str]]:
        """The hot-reloaded ``preferred_fields`` mapping (BC name -> ordered
        field names) from the spec. Empty when the spec declares no section."""
        return self._preferred_fields

    def health_status(self) -> dict[str, Any]:
        """Read-only snapshot for /health: how many tools are loaded and from
        which spec file (path + mtime). Does not trigger a reload."""
        return {
            "tools": len(self._tools),
            "spec_path": self._spec_path,
            "spec_mtime": self._spec_mtime,
        }

    def _load_valid_tools(self) -> tuple[dict[str, ToolSpec], dict[str, list[str]]]:
        """Read, parse, deref, validate, and resolve executors for the spec file.
        Returns a fresh ``name → ToolSpec`` map and the loaded ``preferred_fields``
        mapping on success; raises ``_SpecError`` on any failure so the caller can
        keep the last valid set. Every entry must declare a known ``kind``; the
        kind's validator checks its kind-specific options and the kind's assembler
        binds the executor — a single bad entry rejects the whole reload so a
        half-edited spec never partially replaces a good one."""
        try:
            with open(self._spec_path, encoding="utf-8") as fh:
                data = json.loads(fh.read(), object_pairs_hook=_no_dup_pairs)
        except OSError as exc:
            raise _SpecError(f"not readable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise _SpecError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
            raise _SpecError("missing 'tools' object")
        defs = data.get("$defs", {})
        if not isinstance(defs, dict):
            raise _SpecError("'$defs' must be an object")
        # Optional top-level preferred_fields (BC name -> ordered field names).
        # Validated here so a bad section fails the load atomically with the
        # tools; the mapping is surfaced to executors/handlers via ToolContext.
        preferred_fields = _validate_preferred_fields(data.get("preferred_fields"))
        tools_data: dict[str, Any] = data["tools"]

        built: dict[str, ToolSpec] = {}
        for name, entry in tools_data.items():
            if not _valid_spec_entry(entry):
                raise _SpecError(f"invalid entry for {name!r}")
            kind = entry["kind"]
            kind_spec = _KINDS.get(kind)
            if kind_spec is None:
                raise _SpecError(f"unknown kind {kind!r} for {name!r} (known: {sorted(_KINDS)})")
            try:
                schema = _deref(entry["input_schema"], defs)
            except _SpecError as exc:
                raise _SpecError(f"$ref error in {name!r}: {exc}") from exc
            if not _valid_resolved_schema(schema):
                raise _SpecError(f"invalid resolved schema for {name!r}")
            access = entry["access"]
            if access not in kind_spec.allowed_access:
                raise _SpecError(
                    f"kind {kind!r} permits access {sorted(kind_spec.allowed_access)} "
                    f"(entry {name!r} has access {access!r})"
                )
            # Kind-specific option validation (raises _SpecError naming the rule).
            kind_spec.validate(entry, name, schema)
            # Bind the executor once; the returned callable is the single handler
            # ToolRegistry.call invokes.
            try:
                handler = kind_spec.assemble(entry, name)
            except _SpecError as exc:
                raise _SpecError(f"{kind} error in {name!r}: {exc}") from exc
            always_enabled = bool(entry.get("always_enabled", False))
            # always_enabled exempts a tool from SIEBEL_DISABLED_TOOLS only — it
            # must never bypass the access gate, so it is only permitted on a
            # read tool (the auth-establishment tool is the one that matters).
            if always_enabled and access != "read":
                raise _SpecError(
                    f"always_enabled is only allowed with access 'read' "
                    f"(entry {name!r} has access {access!r})"
                )
            built[name] = ToolSpec(
                name=name,
                description=entry["description"],
                input_schema=schema,
                handler=handler,
                access=access,
                always_enabled=always_enabled,
                establishes_session=bool(entry.get("establishes_session", False)),
            )
        # Exactly one entry establishes a session; it must be always_enabled and
        # access "read". The transports read its name from session_tool_name, so
        # this invariant is what lets them avoid hard-coding a tool name.
        session_tools = [n for n, t in built.items() if t.establishes_session]
        if len(session_tools) != 1:
            raise _SpecError(
                f"exactly one tool must mark establishes_session (found {len(session_tools)}: {session_tools!r})"
            )
        session_name = session_tools[0]
        session_spec = built[session_name]
        if not session_spec.always_enabled:
            raise _SpecError(f"establishes_session tool {session_name!r} must be always_enabled")
        if session_spec.access != "read":
            raise _SpecError(f"establishes_session tool {session_name!r} must be access 'read'")
        self._session_tool_name = session_name
        return built, preferred_fields

    def _reload_if_changed(self) -> None:
        """Re-read the spec file if its mtime changed since the last load. On a
        successful reload, swap in the new tool set. On a reload failure after
        the first good load, keep the last valid set and log. On a failure
        during the very first load, raise ``_SpecError`` so construction aborts
        and the process exits non-zero with a clear error (which entry, which
        rule) — without a valid spec the set of tools is unknown."""
        with self._lock:
            try:
                mtime = os.stat(self._spec_path).st_mtime_ns
            except OSError:
                if self._first_load:
                    raise _SpecError(f"tool spec file unavailable: {self._spec_path}") from None
                log.warning("Tool spec file unavailable: %s — keeping current tools", self._spec_path)
                self._spec_mtime = None
                return
            if mtime == self._spec_mtime:
                return

            try:
                built, preferred_fields = self._load_valid_tools()
            except _SpecError as exc:
                # Record this mtime so a broken file isn't re-read on every list
                # call; it'll be retried when the file changes again.
                self._spec_mtime = mtime
                if self._first_load:
                    raise
                log.warning("Tool spec reload failed — keeping current tools (%s): %s", exc, self._spec_path)
                return

            self._tools = built
            self._preferred_fields = preferred_fields
            self._spec_mtime = mtime
            self._first_load = False
            log.info("Loaded %d tool specs from %s", len(built), self._spec_path)
            self._warn_disabled_always_enabled(built)

    def _warn_disabled_always_enabled(self, built: dict[str, ToolSpec]) -> None:
        """Log a clear warning for every always-enabled tool that
        SIEBEL_DISABLED_TOOLS tried to disable — the filter ignores those
        entries, so the deployer hears about the misconfiguration instead of
        silently getting the tool back."""
        disabled = {name.lower() for name in self.settings.disabled_tools}
        for name, spec in built.items():
            if spec.always_enabled and name.lower() in disabled:
                log.warning(
                    "SIEBEL_DISABLED_TOOLS lists %r, but the spec marks it always_enabled — "
                    "ignored (always_enabled tools cannot be disabled).",
                    name,
                )

    def _access_enabled(self, tool: ToolSpec) -> bool:
        """Whether ``tool``'s access level is currently permitted by the
        SIEBEL_ALLOW_WRITE / SIEBEL_ALLOW_DELETE / SIEBEL_ALLOW_EXTERNAL flags.
        ``read`` is always on; ``write`` / ``delete`` / ``external`` are off by
        default (read-only server, and no data leaves for an outside service
        without an explicit opt-in)."""
        if tool.access == "write":
            return bool(self.settings.allow_write)
        if tool.access == "delete":
            return bool(self.settings.allow_delete)
        if tool.access == "external":
            return bool(self.settings.allow_external)
        return True

    def list_tools(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        disabled = {name.lower() for name in self.settings.disabled_tools}
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in self._tools.values()
            if self._access_enabled(tool)
            and (tool.always_enabled or tool.name.lower() not in disabled)
        ]

    async def call(self, name: str, arguments: dict[str, Any], session: SessionContext | None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return _error(f'Unknown tool "{name}".')
        # The access gate takes precedence: a write/delete/external tool is
        # refused when its flag is off, regardless of always_enabled (which only
        # exempts the SIEBEL_DISABLED_TOOLS list, never the access gate).
        if not self._access_enabled(tool):
            flag = {
                "write": "allow_write",
                "delete": "allow_delete",
                "external": "allow_external",
            }.get(tool.access, "allow_write/allow_delete/allow_external")
            return _error(f'Tool "{name}" is disabled (requires {flag}).')
        if not tool.always_enabled and name.lower() in {item.lower() for item in self.settings.disabled_tools}:
            return _error(f'Tool "{name}" is disabled.')
        log.debug("Calling tool %s with args %s", name, list(arguments.keys()))
        try:
            result = await tool.handler(
                arguments, ToolContext(settings=self.settings, session=session, preferred_fields=self._preferred_fields)
            )
        except Exception as exc:  # noqa: BLE001 — convert any handler crash / SiebelError into a structured result
            return _error_result(exc, name)
        if result.get("isError"):
            log.warning("Tool %s returned isError — content: %s", name, result.get("content"))
        return result
