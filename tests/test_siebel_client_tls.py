"""Finding 8: TLS verification is on by default (``verify_tls=True``), with an
optional CA-bundle override. Previously ``SiebelClient`` hardcoded
``verify=False`` on every outbound call, silently disabling certificate
verification.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_siebel.siebel_client import SiebelClient


def test_verify_defaults_true() -> None:
    assert SiebelClient(base_url="https://x")._verify is True


def test_verify_tls_false_disables_verification() -> None:
    assert SiebelClient(base_url="https://x", verify_tls=False)._verify is False


def test_ca_bundle_overrides_to_path() -> None:
    # A CA bundle path wins: httpx uses it as the trust store (verification on).
    assert SiebelClient(base_url="https://x", ca_bundle="/path/ca.pem")._verify == "/path/ca.pem"


def test_ca_bundle_overrides_verify_tls_false() -> None:
    # An explicit bundle with verify_tls=False still uses the bundle (verification
    # on via the bundle) — ca_bundle takes precedence, matching the documented
    # "set SIEBEL_CA_BUNDLE to trust a self-signed cert" path.
    assert SiebelClient(base_url="https://x", verify_tls=False, ca_bundle="/ca.pem")._verify == "/ca.pem"


@pytest.mark.asyncio
async def test_async_client_receives_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """The httpx.AsyncClient is constructed with the resolved ``verify`` value."""
    import httpx

    response = MagicMock(status_code=204, headers={})
    cm_client = MagicMock()
    cm_client.request = AsyncMock(return_value=response)
    async_cm = MagicMock()
    async_cm.__aenter__ = AsyncMock(return_value=cm_client)
    async_cm.__aexit__ = AsyncMock(return_value=None)
    mock_ac = MagicMock(return_value=async_cm)
    monkeypatch.setattr(httpx, "AsyncClient", mock_ac)

    client = SiebelClient(base_url="https://x", auth_mode="sso", sso_username="u", ca_bundle="/ca.pem")
    # 204 short-circuits _request before any JSON parsing, so no further mocking.
    await client._request("GET", "/data")

    assert mock_ac.call_args.kwargs["verify"] == "/ca.pem"
