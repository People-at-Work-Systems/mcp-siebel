"""Console-script entry point for the HTTP/SSE server (``mcp-siebel-http``).

This module deliberately imports FastAPI/uvicorn **inside** ``main()``, not at
module level, so that ``import mcp_siebel._http_entry`` (and therefore
``import mcp_siebel``) never pulls in the HTTP dependencies. A base-only
install (``pip install mcp-siebel``) can run ``mcp-siebel`` (stdio) without
``fastapi``; running ``mcp-siebel-http`` without the ``[http]`` extra prints a
clear message instead of a raw traceback.
"""

from __future__ import annotations


def main() -> None:
    try:
        import uvicorn

        from mcp_siebel.http_server import app  # noqa: F401  (imports fastapi)
    except ImportError as e:
        raise SystemExit(
            "mcp-siebel-http requires the [http] extra. "
            "Install it with: pip install mcp-siebel[http]"
        ) from e
    uvicorn.run(app, host="0.0.0.0", port=8201)


if __name__ == "__main__":
    main()
