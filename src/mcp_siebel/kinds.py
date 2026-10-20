"""Tool-kind registry primitives.

A tool's ``kind`` (declared in the spec) selects an executor: a small, generic
piece of framework code that turns the spec entry + the runtime arguments into a
tool result. Each kind declares which spec options it requires/allows, so the
registry can validate kind-specific options at load time. This module holds only
the ``_KindSpec`` dataclass and the set of spec fields common to every kind; the
kinds themselves are registered in
``tools.py`` (the ``python`` escape hatch) and in ``executors/`` (the generic
executors).

Kept free of any import from ``tools`` or ``executors`` so there is no runtime
cycle: ``tools`` imports this module, and the executor modules import it too.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class _SpecError(Exception):
    """Raised when a tool-spec file is unreadable, unparseable, has bad $refs,
    fails validation, or names an unknown kind / unresolvable handler / invalid
    kind-specific option. The registry treats any _SpecError as a failed reload
    and keeps the last valid tool set (or, on the very first load, aborts startup
    so the process exits non-zero with a clear error)."""


# Spec fields every entry may carry, regardless of kind. Kind-specific options
# (e.g. ``handler`` for python, ``scope``/``result``/``render`` for bc_query) are
# everything else on the entry and are validated per-kind.
_COMMON_FIELDS: frozenset[str] = frozenset(
    {"description", "input_schema", "kind", "enabled", "establishes_session"}
)

# (entry, tool_name, resolved input_schema) -> None; raise on an invalid
# kind-specific option. Called once per entry at load time.
ValidateFn = Callable[[dict[str, Any], str, dict[str, Any]], None]

# (entry, tool_name) -> the ToolHandler ToolRegistry.call() will invoke. The
# executor is resolved/bound once at load time; the returned callable closes over
# the spec entry's kind-specific options.
AssembleFn = Callable[[dict[str, Any], str], Any]


@dataclass(frozen=True)
class _KindSpec:
    name: str
    # Kind-specific option keys the entry may carry.
    allowed_options: frozenset[str]
    # Kind-specific option keys the entry must carry.
    required_options: frozenset[str]
    validate: ValidateFn
    assemble: AssembleFn
