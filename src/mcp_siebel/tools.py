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
kind + executor + enabled/session-rule resolution) and only swaps in the new set
on success; on any failure the last valid set keeps serving and the failure is
logged. If the very first load fails (no last valid set) construction raises
``_SpecError`` so the process exits non-zero with a clear error — the handlers
come from the spec, so without it the set of tools is unknown.
"""

from __future__ import annotations

import hashlib
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

from .common import _error, _error_result
from .config import AppSettings
from .kinds import _COMMON_FIELDS, _KindSpec, _SpecError

if TYPE_CHECKING:
    # Annotation-only (stringified via ``from __future__ import annotations``):
    # importing them at runtime would work but isn't needed here, and keeping
    # them out of the runtime import graph leaves tools.py dependent only on
    # the common helpers it actually calls.
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
    # Name of the spec's session-establishment tool, injected by
    # ``ToolRegistry.call`` so the not-authenticated hint (and any other
    # framework message) can name it without a hard-coded tool literal. Empty
    # only when a context is built outside the registry (tests).
    session_tool_name: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    # The single on/off switch for a tool. ``False`` hides it from
    # ``list_tools()`` and refuses it in ``call()``. The bundled spec sets
    # ``enabled: false`` on its 7 write/delete/external tools so the server is
    # read-only by default; a deployer flips ``enabled: true`` on an entry in
    # an override spec (``SIEBEL_TOOLS_SPEC_PATH``) to turn a tool on — no env
    # var. Authored in the spec, not hard-coded here.
    enabled: bool = True
    # True for the single entry the spec marks ``establishes_session`` — the
    # tool the transports call to create a session and whose session_id they
    # inject into results/schemas. Exactly one such entry is required, and it
    # must be enabled. Authored in the spec, not hard-coded here, so tools.py
    # stays free of tool names.
    establishes_session: bool = False


@dataclass(frozen=True)
class ToolSnapshot:
    """Immutable view of one successfully-loaded spec version.

    A reload builds a complete new ``ToolSnapshot`` and swaps the registry's
    single ``self._snapshot`` reference under ``self._lock``; ``list_tools()``
    and ``call()`` read that reference exactly once per request, so a request
    can never mix the tool map with a stale ``preferred_fields`` mapping (or
    vice versa) from a previous version. Everything a request needs travels
    together on one object.

    ``spec_version`` is a monotonic counter incremented on every successful
    reload whose *exposed* tool list actually changed (by content hash); a
    touched-but-identical file and a failed reload both leave it untouched.
    ``content_hash`` is a stable SHA-256 of the exposed tool list (the filtered
    set clients see via ``list_tools()``), so a spec edit that only touches a
    disabled (``enabled: false``) tool — leaving the exposed list unchanged — is
    not advertised as a change. ``exposed_tools`` is that filtered list,
    precomputed at build time so ``list_tools()`` is a single reference read.
    """

    tools: dict[str, ToolSpec]
    preferred_fields: dict[str, list[str]]
    session_tool_name: str
    spec_version: int
    content_hash: str
    exposed_tools: list[dict[str, Any]]


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
    must carry a string description, a string ``kind``, and an input_schema
    dict with a type and a properties map. ``enabled`` and
    ``establishes_session`` are optional but, if present, must be bools.
    Kind-specific fields (e.g. ``handler`` for python, ``scope``/``result``/
    ``render`` for bc_query) are validated per-kind."""
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("description"), str)
        and isinstance(entry.get("input_schema"), dict)
        and isinstance(entry["input_schema"].get("properties"), dict)
        and "type" in entry["input_schema"]
        and isinstance(entry.get("kind"), str)
        and ("enabled" not in entry or isinstance(entry["enabled"], bool))
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


# Handler references may only name modules inside this package — the resolver
# rejects anything outside it so a spec can't pull in arbitrary code.
_ALLOWED_HANDLER_PKG = "mcp_siebel.handlers"


# --- Kind registry ---------------------------------------------------------
#
# Each kind declares its executor (assembled once at load time into the
# ToolHandler ToolRegistry.call invokes) and the spec options it
# requires/allows. Kind-specific options are validated at load time; any
# violation is a _SpecError (fatal on first load, keep-last-valid on reload).
# The ``python`` kind is the escape hatch for tools with real custom logic;
# generic executors live under ``executors/`` and register here.


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
    by mtime — ``list_tools()``/``snapshot()`` stat the file and re-read it only
    when it changed, so edits go live on the next tools/list with no restart (a
    file watcher supplements this with a push notification; see ``watcher.py``).
    Every reload re-runs the full pipeline (parse with duplicate detection →
    deref → validate → per-entry kind + executor resolution) and, on success,
    builds a fresh immutable ``ToolSnapshot`` and swaps the single
    ``self._snapshot`` reference under ``self._lock``; ``list_tools()`` and
    ``call()`` read that reference exactly once per request, so a request never
    mixes data from two spec versions. On any failure the last valid snapshot
    keeps serving and the failure is logged. If the very first load fails there
    is no last valid set, so construction raises ``_SpecError`` (the process
    exits non-zero with a clear error) rather than serving an unknown tool set.

    A monotonic ``spec_version`` (and a content hash of the exposed tool list)
    travel on the snapshot and are surfaced via ``health_status()``; both are
    bumped only on a successful reload whose exposed list actually changed.
    ``subscribe()`` registers a change-hook fired after such a reload — a
    touched-but-identical file and a failed reload both fire nothing.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._spec_path = _resolve_spec_path(settings)
        self._spec_mtime: float | None = None
        self._lock = threading.Lock()
        # The single immutable view of the loaded spec. Swapped under
        # ``self._lock`` by ``_reload_if_changed``; read exactly once per
        # request by ``list_tools()``/``call()`` so a request never mixes data
        # from two spec versions. None only before the first successful load
        # (construction raises on a first-load failure, so a constructed
        # registry always has a snapshot).
        self._snapshot: ToolSnapshot | None = None
        # Change-hook subscribers, notified after a successful reload whose
        # exposed tool list actually changed. Guarded by ``self._lock``.
        self._subscribers: list[Callable[[ToolSnapshot], None]] = []
        # True until the first successful load. A failure before then is fatal:
        # without a valid spec the set of tools is unknown, so construction
        # raises and the process exits non-zero with a clear error. After the
        # first good load a reload failure is non-fatal (log + keep last valid).
        self._first_load: bool = True
        self._reload_if_changed()

    @property
    def session_tool_name(self) -> str:
        """Name of the spec's single establishes_session tool — the one the
        transports call to create a session and whose session_id they inject.
        Set on every successful load; a successful load always has exactly one."""
        assert self._snapshot is not None, "no tool spec loaded"
        return self._snapshot.session_tool_name

    @property
    def spec_path(self) -> str:
        """The resolved spec file path the registry loads from (override or
        bundled). Read-only — used by the watcher and surfaced in /health."""
        return self._spec_path

    @property
    def preferred_fields(self) -> dict[str, list[str]]:
        """The hot-reloaded ``preferred_fields`` mapping (BC name -> ordered
        field names) from the current snapshot. Empty when the spec declares no
        section. Read-only view for callers outside the registry; ``call()``
        reads it off the snapshot directly."""
        assert self._snapshot is not None, "no tool spec loaded"
        return self._snapshot.preferred_fields

    @property
    def _tools(self) -> dict[str, ToolSpec]:
        """Read-only view of the current snapshot's tool map (test/inspection
        helper). The storage is the snapshot; this property exists so existing
        callers that read ``registry._tools`` keep working against the single
        source of truth."""
        assert self._snapshot is not None, "no tool spec loaded"
        return self._snapshot.tools

    def health_status(self) -> dict[str, Any]:
        """Read-only snapshot for /health: how many tools are loaded, from which
        spec file (path + mtime), and the current ``spec_version`` / content
        hash. Does not trigger a reload."""
        snap = self._snapshot
        return {
            "tools": len(snap.tools) if snap else 0,
            "spec_path": self._spec_path,
            "spec_mtime": self._spec_mtime,
            "spec_version": snap.spec_version if snap else 0,
            "content_hash": snap.content_hash if snap else "",
        }

    def _load_valid_tools(self) -> tuple[dict[str, ToolSpec], dict[str, list[str]], str]:
        """Read, parse, deref, validate, and resolve executors for the spec file.
        Returns a fresh ``name → ToolSpec`` map, the loaded ``preferred_fields``
        mapping, and the session-tool name on success; raises ``_SpecError`` on
        any failure so the caller can keep the last valid set. Every entry must
        declare a known ``kind``; the kind's validator checks its kind-specific
        options and the kind's assembler binds the executor — a single bad entry
        rejects the whole reload so a half-edited spec never partially replaces a
        good one. This method touches no instance state: the caller builds the
        immutable snapshot from the return value under the lock."""
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
            # Kind-specific option validation (raises _SpecError naming the rule).
            kind_spec.validate(entry, name, schema)
            # Bind the executor once; the returned callable is the single handler
            # ToolRegistry.call invokes.
            try:
                handler = kind_spec.assemble(entry, name)
            except _SpecError as exc:
                raise _SpecError(f"{kind} error in {name!r}: {exc}") from exc
            built[name] = ToolSpec(
                name=name,
                description=entry["description"],
                input_schema=schema,
                handler=handler,
                enabled=bool(entry.get("enabled", True)),
                establishes_session=bool(entry.get("establishes_session", False)),
            )
        # Exactly one entry establishes a session; it must be enabled. The
        # transports read its name from session_tool_name, so this invariant is
        # what lets them avoid hard-coding a tool name.
        session_tools = [n for n, t in built.items() if t.establishes_session]
        if len(session_tools) != 1:
            raise _SpecError(
                f"exactly one tool must mark establishes_session (found {len(session_tools)}: {session_tools!r})"
            )
        session_name = session_tools[0]
        session_spec = built[session_name]
        if not session_spec.enabled:
            raise _SpecError(f"establishes_session tool {session_name!r} must be enabled")
        return built, preferred_fields, session_name

    def _exposed_tools(self, built: dict[str, ToolSpec]) -> list[dict[str, Any]]:
        """The filtered tool list clients see via ``list_tools()``: disabled
        (``enabled: false``) tools dropped. Computed at snapshot build time so
        ``list_tools()`` is a single reference read and the content hash
        reflects exactly what is exposed."""
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in built.values()
            if tool.enabled
        ]

    @staticmethod
    def _content_hash(exposed: list[dict[str, Any]]) -> str:
        """Stable SHA-256 of the exposed tool list (canonical JSON). Changes only
        when a client-visible tool's name/description/input_schema changes — so a
        spec edit that only touches a disabled tool is not advertised as a change."""
        return hashlib.sha256(json.dumps(exposed, sort_keys=True).encode("utf-8")).hexdigest()

    def _reload_if_changed(self) -> None:
        """Re-read the spec file if its mtime changed since the last load. On a
        successful reload, build a fresh immutable ``ToolSnapshot`` and swap the
        single ``self._snapshot`` reference under the lock. On a reload failure
        after the first good load, keep the last valid snapshot and log. On a
        failure during the very first load, raise ``_SpecError`` so construction
        aborts and the process exits non-zero with a clear error. After a
        successful swap whose exposed tool list actually changed (by content
        hash), change-hook subscribers are notified outside the lock — a
        touched-but-identical file and a failed reload both notify nothing."""
        changed_snapshot: ToolSnapshot | None = None
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
                built, preferred_fields, session_name = self._load_valid_tools()
            except _SpecError as exc:
                # Record this mtime so a broken file isn't re-read on every list
                # call; it'll be retried when the file changes again.
                self._spec_mtime = mtime
                if self._first_load:
                    raise
                log.warning("Tool spec reload failed — keeping current tools (%s): %s", exc, self._spec_path)
                return

            exposed = self._exposed_tools(built)
            new_hash = self._content_hash(exposed)
            prev = self._snapshot
            # The snapshot is rebuilt on every successful reload so a
            # preferred_fields-only change (which leaves the exposed tool list
            # — and thus the hash — unchanged) still reaches handlers on their
            # next call. The spec_version bump and the change-hook are gated on
            # the exposed list actually changing; a touched-but-identical file
            # and a preferred_fields-only edit bump neither and fire nothing.
            content_changed = prev is None or prev.content_hash != new_hash
            if prev is None:
                new_version = 1
            elif content_changed:
                new_version = prev.spec_version + 1
            else:
                new_version = prev.spec_version
            snapshot = ToolSnapshot(
                tools=built,
                preferred_fields=preferred_fields,
                session_tool_name=session_name,
                spec_version=new_version,
                content_hash=new_hash,
                exposed_tools=exposed,
            )
            self._snapshot = snapshot
            self._spec_mtime = mtime
            self._first_load = False
            if content_changed:
                changed_snapshot = snapshot
            log.info("Loaded %d tool specs from %s (version %d)", len(built), self._spec_path, new_version)

        # Notify subscribers outside the lock so a slow callback can't block a
        # list/call and the lock never re-enters via callback code.
        if changed_snapshot is not None:
            self._notify_subscribers(changed_snapshot)

    def _notify_subscribers(self, snapshot: ToolSnapshot) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(snapshot)
            except Exception:  # noqa: BLE001 — a subscriber fault must not break others or the reload
                log.exception("change-hook subscriber raised; continuing")

    def reload(self) -> None:
        """Public reload entry point (used by the file watcher). Idempotent:
        stats the spec and rebuilds the snapshot only if its mtime changed.
        Fires change-hook subscribers iff the exposed tool list changed."""
        self._reload_if_changed()

    def snapshot(self) -> ToolSnapshot:
        """Return the current immutable snapshot, triggering a lazy mtime reload
        first (the fallback path the watcher supplements). Transports read
        ``spec_version`` / ``content_hash`` from this for ETag and SSE events."""
        self._reload_if_changed()
        assert self._snapshot is not None, "no tool spec loaded"
        return self._snapshot

    def subscribe(self, callback: Callable[[ToolSnapshot], None]) -> Callable[[], None]:
        """Register a callback fired after a successful reload whose exposed tool
        list actually changed (the new snapshot is passed). Returns an
        unsubscribe function. Callbacks run synchronously on the reloading
        thread; transports bridge to their event loop inside the callback."""

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        with self._lock:
            self._subscribers.append(callback)
        return _unsubscribe

    def list_tools(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        # Read the snapshot reference exactly once; the exposed list is
        # precomputed on it, so the result is a consistent view of one version
        # (never half v1 / half v2). A shallow copy keeps callers from mutating
        # the cached list; element dicts are shared, as before.
        snap = self._snapshot
        assert snap is not None, "no tool spec loaded"
        return list(snap.exposed_tools)

    async def call(self, name: str, arguments: dict[str, Any], session: SessionContext | None) -> dict[str, Any]:
        # Read the snapshot reference exactly once: the tool lookup and the
        # preferred_fields / session_tool_name used below all come from the same
        # version, so a concurrent reload can never split them.
        snap = self._snapshot
        assert snap is not None, "no tool spec loaded"
        tool = snap.tools.get(name)
        if tool is None:
            return _error(f'Unknown tool "{name}".')
        if not tool.enabled:
            return _error(f'Tool "{name}" is disabled.')
        log.debug("Calling tool %s with args %s", name, list(arguments.keys()))
        try:
            result = await tool.handler(
                arguments,
                ToolContext(
                    settings=self.settings,
                    session=session,
                    preferred_fields=snap.preferred_fields,
                    session_tool_name=snap.session_tool_name,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — convert any handler crash / SiebelError into a structured result
            return _error_result(exc, name)
        if result.get("isError"):
            log.warning("Tool %s returned isError — content: %s", name, result.get("content"))
        return result
