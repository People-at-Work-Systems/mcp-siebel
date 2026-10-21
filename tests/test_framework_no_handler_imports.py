"""Framework code must not import from ``mcp_siebel.handlers``.

The layering invariant (spec Task C): the framework layer — ``tools.py``,
``kinds.py``, ``renderers.py``, and everything under ``executors/`` (including
``_fallback.py`` and ``_validation.py``) — must depend only on framework
modules and ``common.py``/``siebel_client``. The ``python``-kind handlers under
``mcp_siebel.handlers`` are the escape hatch for custom tools; framework code
reaching into them couples a generic executor to a specific tool's
implementation and is a regression.

This is a static check over the source files (parsed with ``ast``, so string
literals and docstrings that merely mention ``handlers`` don't false-positive).
The only module permitted to import from ``mcp_siebel.handlers`` is
``mcp_siebel.tools`` — and only for the ``_ALLOWED_HANDLER_PKG`` string and the
``importlib.import_module`` call in ``_resolve_handler``, which are NOT import
statements and so don't trip this check.
"""

from __future__ import annotations

import ast
import importlib.resources

# Modules that make up the framework layer (no ``mcp_siebel.handlers`` imports
# allowed). ``mcp_siebel.handlers`` itself is the one package that may import
# from its own submodules, so it is excluded from the scan.
_EXCLUDED_DIRS = {"handlers"}


def _is_handlers_import(node: ast.AST) -> bool:
    """True if ``node`` is an import that names ``mcp_siebel.handlers`` (absolute)
    or a relative ``from <.handlers...>`` / ``from . import handlers``."""
    if isinstance(node, ast.Import):
        return any(
            alias.name == "mcp_siebel.handlers" or alias.name.startswith("mcp_siebel.handlers.")
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        # Absolute: from mcp_siebel.handlers[.x] import ...
        if node.module and (node.module == "mcp_siebel.handlers" or node.module.startswith("mcp_siebel.handlers.")):
            return True
        # Relative: from .handlers[.x] import ... (level >= 1, module starts with 'handlers')
        if node.level and node.module and (node.module == "handlers" or node.module.startswith("handlers.")):
            return True
        # Relative: from . import handlers (module is None, 'handlers' is an imported name)
        if node.level and node.module is None and any(alias.name == "handlers" for alias in node.names):
            return True
    return False


def _walk(root):
    for child in root.iterdir():
        if child.is_dir():
            if child.name in _EXCLUDED_DIRS:
                continue
            yield from _walk(child)
        elif child.name.endswith(".py"):
            yield child


def test_framework_modules_do_not_import_handlers() -> None:
    pkg = importlib.resources.files("mcp_siebel")
    offenders: list[str] = []
    for f in _walk(pkg):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if _is_handlers_import(node):
                offenders.append(f"{f}: imports mcp_siebel.handlers ({ast.dump(node)})")
    assert not offenders, "framework code imports from mcp_siebel.handlers:\n" + "\n".join(offenders)
