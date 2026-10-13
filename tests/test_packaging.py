"""The stdio path must not pull in FastAPI/uvicorn/prometheus.

Guards the ``[http]``-extra split: ``import mcp_siebel.stdio`` (and therefore
``import mcp_siebel``) must work on a base-only install and must not drag the
HTTP dependencies into ``sys.modules``.
"""

from __future__ import annotations

import sys

_HTTP_DEPS = ("fastapi", "uvicorn", "prometheus_fastapi_instrumentator")


def test_stdio_import_does_not_pull_http_deps() -> None:
    for mod in _HTTP_DEPS:
        sys.modules.pop(mod, None)
    import mcp_siebel.stdio  # noqa: F401

    pulled = [mod for mod in _HTTP_DEPS if mod in sys.modules]
    assert not pulled, f"stdio import pulled in HTTP deps: {pulled}"
