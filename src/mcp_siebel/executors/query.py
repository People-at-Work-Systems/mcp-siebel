"""The ``bc_query`` executor: query / count over a Business Component.

This is the generic executor behind the spec kind ``bc_query``. It replaces the
per-tool ``_query`` / ``_query_child`` handlers: the field-error retry, the
placeholder guard, the 404 -> empty fallback, and the count behaviour all live
here once, parameterised by the spec entry's options (``scope``, ``result``,
``render``, and the optional ``bind`` / ``searchspec_template``). Argument names
are derived from the previous handlers and from ``siebel_client.py`` — the
external tool interface is unchanged.

No tool names live in this module. The executor is bound to a spec entry at load
time (``_assemble_bc_query`` closes over the entry's options) and the resulting
callable is the single handler ``ToolRegistry.call`` invokes.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ..handlers._common import (
    _empty_result,
    _error,
    _error_result,
    _require_auth,
    _resolve_bo_bc,
    _result_dict,
    _text,
    _validate_fields_arg,
)
from ..kinds import _COMMON_FIELDS, _KindSpec, _SpecError
from ..renderers import RENDERERS
from ..siebel_client import SiebelError
from ._validation import validate_bind_and_required

if TYPE_CHECKING:
    from ..tools import ToolContext

log = logging.getLogger("mcp-siebel")

# Field-error codes that mean "you asked for a field the BC doesn't expose" —
# retried with a known-safe field list (['Id']) instead of falling back to
# Siebel's sometimes-broken default Integration-Object field set (e.g. Contact
# referencing 'Primary Address Integration Id' -> SBL-DAT-00398).
_FIELD_ERROR_CODES = {"SBL-EAI-50258", "SBL-EAI-50297"}


def _is_field_error(exc: SiebelError) -> bool:
    return exc.status_code == 500 and any(code in exc.response_body for code in _FIELD_ERROR_CODES)


_SCOPES = {"root", "child"}
_RESULTS = {"records", "count"}
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# No reliable escaping rule exists for these in Siebel search-spec values, so a
# template placeholder value containing one is rejected rather than guessed.
_FORBIDDEN_TEMPLATE_CHARS = ('"', '[', ']')

_BC_QUERY_ALLOWED_OPTIONS = frozenset({"scope", "result", "render", "bind", "searchspec_template"})
_BC_QUERY_REQUIRED_OPTIONS = frozenset({"scope", "result", "render"})

# Parameters execute_bc_query actually reads from the effective args (the bind
# values merged with the caller's input). A bind key outside this set is a typo
# that would silently do nothing, so it is rejected at load time.
_BC_QUERY_BIND_KEYS = frozenset(
    {
        "business_object",
        "business_component",
        "fields",
        "page_size",
        "offset",
        "sort_spec",
        "childlinks",
        "parent_id",
        "child_business_component",
        "searchspec",
    }
)


# ---------------------------------------------------------------------------
# Load-time validation of kind-specific options
# ---------------------------------------------------------------------------


def _placeholders_outside_quotes(template: str) -> list[str]:
    """Return placeholder names in ``template`` that are NOT inside a single-quoted
    literal. A Siebel searchspec string literal is ``'...'`` with ``''`` an embedded
    quote; we walk the template tracking quote state and report ``{name}`` tokens
    encountered while not inside a literal."""
    outside: list[str] = []
    in_quote = False
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "'":
            if in_quote and i + 1 < n and template[i + 1] == "'":
                i += 2  # doubled quote — escaped, stays inside the literal
                continue
            in_quote = not in_quote
            i += 1
            continue
        if ch == "{":
            j = template.find("}", i + 1)
            if j != -1 and _PLACEHOLDER_RE.fullmatch(template[i : j + 1]):
                if not in_quote:
                    outside.append(template[i + 1 : j])
                i = j + 1
                continue
        i += 1
    return outside


def _validate_bc_query(entry: dict[str, Any], name: str, schema: dict[str, Any]) -> None:
    options = set(entry.keys()) - _COMMON_FIELDS
    extra = options - _BC_QUERY_ALLOWED_OPTIONS
    if extra:
        raise _SpecError(f"unknown option(s) {sorted(extra)} for bc_query entry {name!r}")
    missing = _BC_QUERY_REQUIRED_OPTIONS - entry.keys()
    if missing:
        raise _SpecError(f"bc_query entry {name!r} missing required option(s) {sorted(missing)}")
    scope = entry.get("scope")
    if scope not in _SCOPES:
        raise _SpecError(f"bc_query entry {name!r} has invalid scope {scope!r} (expected 'root' or 'child')")
    result = entry.get("result")
    if result not in _RESULTS:
        raise _SpecError(f"bc_query entry {name!r} has invalid result {result!r} (expected 'records' or 'count')")
    render = entry.get("render")
    if not isinstance(render, str) or render not in RENDERERS:
        raise _SpecError(f"bc_query entry {name!r} has unknown render {render!r} (known: {sorted(RENDERERS)})")

    props = set((schema.get("properties") or {}).keys())
    tmpl = entry.get("searchspec_template")
    if tmpl is not None:
        if not isinstance(tmpl, str):
            raise _SpecError(f"bc_query entry {name!r} 'searchspec_template' must be a string")
        if "searchspec" in props:
            raise _SpecError(
                f"bc_query entry {name!r}: searchspec_template and a 'searchspec' argument are mutually exclusive"
            )
        bind = entry.get("bind")
        if isinstance(bind, dict) and "searchspec" in bind:
            raise _SpecError(
                f"bc_query entry {name!r}: searchspec_template and a bound 'searchspec' are mutually exclusive"
            )
        placeholders = set(_PLACEHOLDER_RE.findall(tmpl))
        unknown = placeholders - props
        if unknown:
            raise _SpecError(
                f"bc_query entry {name!r}: searchspec_template placeholder(s) {sorted(unknown)} not in input_schema"
            )
        outside = _placeholders_outside_quotes(tmpl)
        if outside:
            raise _SpecError(
                f"bc_query entry {name!r}: searchspec_template placeholder(s) {sorted(outside)} "
                f"must sit inside a single-quoted literal"
            )

    # Shared bind (known keys, disjoint from input_schema) + required-parameter
    # coverage. Run after the template checks so a template error is reported
    # before an unrelated missing-business_object error.
    required_params: tuple[str, ...] = ("business_object",)
    if scope == "child":
        required_params = ("business_object", "parent_id", "child_business_component")
    validate_bind_and_required(
        kind="bc_query",
        entry=entry,
        name=name,
        schema=schema,
        known_bind_keys=_BC_QUERY_BIND_KEYS,
        required_params=required_params,
    )


# ---------------------------------------------------------------------------
# searchspec_template rendering
# ---------------------------------------------------------------------------


def _render_searchspec_template(template: str, args: dict[str, Any]) -> tuple[str | None, str | None]:
    """Substitute ``{arg_name}`` placeholders from ``args`` into ``template``,
    escaping each value for Siebel search-spec syntax. Returns ``(rendered, None)``
    on success or ``(None, error_message)`` if a placeholder is missing or a value
    contains a character with no safe escaping rule.

    Siebel search-spec string literals are single-quoted and an embedded ``'`` is
    doubled — that is the established escaping in this codebase (see
    ``siebel_client._record_via_query`` and ``describe_repository_bc``). For ``"``,
    ``[`` and ``]`` no rule exists (``[``/``]`` delimit field references, ``"`` is
    the alternate string delimiter), so values containing them are rejected.
    """
    error: str | None = None

    def replace(m: re.Match[str]) -> str:
        nonlocal error
        key = m.group(1)
        value = args.get(key)
        if value is None:
            error = f"searchspec_template placeholder {{{key}}} was not provided"
            return m.group(0)
        s = str(value)
        if any(ch in s for ch in _FORBIDDEN_TEMPLATE_CHARS):
            error = (
                f"searchspec_template placeholder {{{key}}} contains a character that is not "
                f"allowed in a Siebel searchspec ('\"', '[' and ']' are rejected)"
            )
            return m.group(0)
        return s.replace("'", "''")

    rendered = _PLACEHOLDER_RE.sub(replace, template)
    if error is not None:
        return None, error
    return rendered, None


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


async def execute_bc_query(args: dict[str, Any], ctx: ToolContext, options: dict[str, Any]) -> dict[str, Any]:
    auth = _require_auth(ctx)
    if isinstance(auth, dict):
        return auth
    # bind = fixed values from the spec; args = the caller's input. They are
    # disjoint (a bound key may not also be an input_schema property), so a plain
    # merge gives the effective argument set.
    effective = {**(options.get("bind") or {}), **args}
    resolved, bc = await _resolve_bo_bc(effective, auth)
    if not resolved:
        return _text(f'Business Object "{effective["business_object"]}" not found.')
    bad_fields = _validate_fields_arg(effective.get("fields"))
    if bad_fields:
        return _error(bad_fields)

    template = options.get("searchspec_template")
    if template:
        searchspec, err = _render_searchspec_template(template, args)
        if err is not None:
            return _error(err)
    else:
        searchspec = effective.get("searchspec")

    scope = options["scope"]
    result_kind = options["result"]
    render = options["render"]
    # count reads only totalCount, so it asks for a single row.
    if result_kind == "count":
        page_size = 1
        offset = 0
    else:
        page_size = int(effective.get("page_size") or 20)
        offset = int(effective.get("offset") or 0)
    fields = effective.get("fields")
    sort_spec = effective.get("sort_spec")
    childlinks = effective.get("childlinks")

    try:
        if scope == "child":
            result = await auth.client.query_child_records(
                resolved,
                bc,
                str(effective["parent_id"]),
                str(effective["child_business_component"]),
                search_spec=searchspec,
                fields=fields,
                sort_spec=sort_spec,
                page_size=page_size,
                offset=offset,
            )
        else:
            result = await auth.client.query(
                resolved,
                bc,
                search_spec=searchspec,
                fields=fields,
                sort_spec=sort_spec,
                page_size=page_size,
                offset=offset,
                childlinks=childlinks,
            )
    except SiebelError as exc:
        if exc.status_code == 404:
            # The empty result must reflect the page_size/offset actually used
            # (the count path forces page_size=1); _empty_result reads them
            # from its arg, so override with the computed values.
            empty = _empty_result(effective)
            empty["pageSize"] = page_size
            empty["offset"] = offset
            return RENDERERS[render](empty)
        # The field-error retry is a root-scope behaviour only (the child path
        # never retried), and only when the caller actually named fields.
        if scope == "root" and _is_field_error(exc) and fields:
            log.warning("Field error on query — retrying with minimal field list (Id)")
            try:
                result = await auth.client.query(
                    resolved,
                    bc,
                    search_spec=searchspec,
                    fields=["Id"],
                    sort_spec=sort_spec,
                    page_size=page_size,
                    offset=offset,
                    childlinks=childlinks,
                )
            except SiebelError as retry_exc:
                return _error_result(retry_exc)
            return RENDERERS[render](_result_dict(result))
        return _error_result(exc)
    return RENDERERS[render](_result_dict(result))


def _assemble_bc_query(entry: dict[str, Any], name: str) -> Any:
    options: dict[str, Any] = {
        "scope": entry["scope"],
        "result": entry["result"],
        "render": entry["render"],
    }
    if "bind" in entry:
        options["bind"] = entry["bind"]
    if "searchspec_template" in entry:
        options["searchspec_template"] = entry["searchspec_template"]

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return await execute_bc_query(args, ctx, options)

    return handler


BC_QUERY_KIND: _KindSpec = _KindSpec(
    name="bc_query",
    allowed_access=frozenset({"read"}),
    allowed_options=_BC_QUERY_ALLOWED_OPTIONS,
    required_options=_BC_QUERY_REQUIRED_OPTIONS,
    validate=_validate_bc_query,
    assemble=_assemble_bc_query,
)
