"""The package must not reference any UnifiedCRM sibling service, in code or prompt.

Guards the zero-coupling invariant (spec §7). Scans every ``.py`` and ``.md``
file inside the installed ``mcp_siebel`` package — including the bundled
``prompts/siebel-system.md`` — for forbidden references. If a future edit
reintroduces one, this test fails locally before CI.
"""

from __future__ import annotations

import importlib.resources

FORBIDDEN = [
    "unifiedcrm",
    "unifiedcrm.ai",
    "unifiedcrm.com",
    "unifiedai",
    "agent-router",
    "backend-api",
    "prompt-manager",
    "dashboard-service",
    "schema-mapper",
    "memory-rag",
    "session-service",
    "llm-engine",
    "mcp-pdf",
    "mcp-salesforce",
    "account_cross_ref",
    "list_account_cross_refs",
    "get_service_requests",
    "lookup_account_cross_ref",
    "/api/v1/",
    "prompt-manager/prompts",
]
_SCAN_EXT = (".py", ".md")


def _walk(root):
    for child in root.iterdir():
        if child.is_dir():
            yield from _walk(child)
        elif child.name.endswith(_SCAN_EXT):
            yield child


def test_no_unifiedcrm_references_in_package_or_prompt() -> None:
    pkg = importlib.resources.files("mcp_siebel")
    offenders: list[str] = []
    for f in _walk(pkg):
        text = f.read_text(encoding="utf-8").lower()
        for bad in FORBIDDEN:
            if bad in text:
                offenders.append(f"{f}: contains {bad!r}")
    assert not offenders, "UnifiedCRM references found in package:\n" + "\n".join(offenders)
