"""mcp-siebel — an MCP server for Oracle Siebel CRM 26.2+."""

from importlib.metadata import PackageNotFoundError, version

# Single source of truth is ``[project] version`` in pyproject.toml, read here
# from the installed package metadata so a bump is one edit (pyproject.toml)
# rather than three. Falls back when run from a source tree that was never
# installed (e.g. ``PYTHONPATH=src``) — installed/editable installs always
# resolve the real version.
try:
    __version__ = version("mcp-siebel")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"
