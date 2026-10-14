"""Shared load-time validation for the generic executors.

The bind / required-parameter checks common to ``bc_query``, ``bc_upsert`` and
``bc_delete`` live here so each executor does not re-implement them:

* every ``bind`` key must name a parameter the executor actually reads (an
  unknown key is a typo that would silently do nothing at runtime);
* each required parameter must be either bound (a fixed value in the spec) or
  listed in the tool's ``input_schema.required`` — otherwise the executor could
  be called with no value for a parameter it dereferences;
* ``bind`` keys must be disjoint from the input_schema properties (a bound value
  and a caller-supplied value for the same key would be ambiguous).

No tool names live here. Each executor passes its own ``known_bind_keys`` and
``required_params``; the helper is parameterised by kind so its errors name the
kind that failed.
"""

from __future__ import annotations

from typing import Any

from ..kinds import _SpecError


def validate_bind_and_required(
    *,
    kind: str,
    entry: dict[str, Any],
    name: str,
    schema: dict[str, Any],
    known_bind_keys: frozenset[str],
    required_params: tuple[str, ...],
) -> None:
    """Raise ``_SpecError`` if ``bind`` is malformed / has unknown keys, or if a
    required parameter is neither bound nor declared in ``input_schema.required``.

    ``bind`` keys must also be disjoint from the input_schema properties. The
    caller has already validated the executor-specific options (scope, render,
    templates, …); this is the shared tail of every generic executor's validator.
    """
    props = set((schema.get("properties") or {}).keys())
    bind = entry.get("bind")
    if bind is None:
        bind_keys: set[str] = set()
    elif isinstance(bind, dict):
        bind_keys = set(bind.keys())
    else:
        raise _SpecError(f"{kind} entry {name!r} 'bind' must be an object")
    overlap = bind_keys & props
    if overlap:
        raise _SpecError(f"{kind} entry {name!r}: bind key(s) {sorted(overlap)} also in input_schema")
    unknown = bind_keys - known_bind_keys
    if unknown:
        raise _SpecError(
            f"{kind} entry {name!r}: bind key(s) {sorted(unknown)} are not known executor "
            f"parameters (known: {sorted(known_bind_keys)})"
        )
    required_in_schema = set(schema.get("required") or [])
    missing = [p for p in required_params if p not in bind_keys and p not in required_in_schema]
    if missing:
        raise _SpecError(
            f"{kind} entry {name!r}: parameter(s) {missing} must be bound or listed in input_schema.required"
        )
