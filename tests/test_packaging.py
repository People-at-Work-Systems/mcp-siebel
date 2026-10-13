"""The stdio path must not pull in the [http]-gated dependencies.

Guards the ``[http]``-extra split: ``import mcp_siebel.stdio`` (and therefore
``import mcp_siebel``) must not drag ``fastapi`` or
``prometheus_fastapi_instrumentator`` into ``sys.modules`` — those are imported
only by ``http_server.py``, which the stdio path never touches.

Note: ``uvicorn`` and ``starlette`` are NOT asserted here, even though they are
web-server deps, because the ``mcp`` SDK itself imports them at module load
(they are core deps of ``mcp``, not extras). So ``import mcp_siebel.stdio``
transitively pulls ``uvicorn``/``starlette`` via ``mcp`` regardless of the
``[http]`` extra, and that is unavoidable while ``mcp>=2.0,<3`` is a dependency.
The property this test enforces is the one that genuinely holds and that the
``[http]`` extra gates: ``fastapi`` and ``prometheus-fastapi-instrumentator``
are absent on a stdio-only import.
"""

from __future__ import annotations

import sys

# Deps that are genuinely [http]-gated (imported only by http_server.py).
# uvicorn/starlette are excluded — mcp itself imports them transitively.
_HTTP_DEPS = ("fastapi", "prometheus_fastapi_instrumentator")


def test_stdio_import_does_not_pull_http_deps() -> None:
    for mod in _HTTP_DEPS:
        sys.modules.pop(mod, None)
    import mcp_siebel.stdio  # noqa: F401

    pulled = [mod for mod in _HTTP_DEPS if mod in sys.modules]
    assert not pulled, f"stdio import pulled in [http]-gated deps: {pulled}"
