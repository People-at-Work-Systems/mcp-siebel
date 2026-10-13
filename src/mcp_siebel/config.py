"""Runtime configuration for the Siebel MCP service — environment-variable driven."""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Tools gated behind SIEBEL_ALLOW_WRITE / SIEBEL_ALLOW_DELETE (both default off,
# so the server is read-only by default). See AppSettings.disabled_tools.
WRITE_TOOLS: tuple[str, ...] = (
    "create_record",
    "create_record_json",
    "update_record",
    "create_child_record",
    "upload_attachment",
)
DELETE_TOOLS: tuple[str, ...] = ("delete_record",)


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIEBEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- Mode -----------------------------------------------------------------
    mcp_mode: str = "standalone"

    # -- Siebel connection ----------------------------------------------------
    base_url: str = ""
    # Alternative to base_url: build from host + scheme
    host: str = ""
    scheme: str = "http"
    sso_header: str = "SBL_USER"
    language_code: str = "ENU"

    # -- Auth probe (standalone) ---------------------------------------------
    # In standalone mode _authenticate probes the SSO identity with a real data
    # query against this business object/component (page_size=1): a 200 means
    # the SSO header is trusted; any error means it isn't. The catalogue
    # (/data/describe) is guest-accessible on some Siebel instances and can't
    # validate the identity, so a data query is required.
    #
    # The probe BO/BC must be (a) REST-enabled and (b) NOT accessible to an
    # unauthenticated/guest identity — a bogus SSO header must yield an error
    # (>=400), not a 200. If it returns 200 for guest (e.g. Account on instances
    # that grant guest read access), authenticate can't detect an invalid header
    # and will report success. Override per instance; Account/Account is a
    # reasonable default for standard Siebel sample DBs.
    probe_business_object: str = "Account"
    probe_business_component: str = "Account"

    # -- Siebel tuning --------------------------------------------------------
    default_page_size: int = Field(default=20, ge=1, le=100)
    request_timeout_ms: int = Field(default=30_000, ge=1_000)
    max_concurrent_requests: int = Field(default=5, ge=1)
    max_queue_depth: int = Field(default=50, ge=1)
    aggregate_page_size: int = Field(default=100, ge=1, le=100)
    aggregate_max_pages: int = Field(default=500, ge=1)

    # -- Demo mode ------------------------------------------------------------
    demo_enabled: bool = False

    # -- Access gates (default off = read-only) -------------------------------
    # When False, the corresponding tools are auto-added to disabled_tools and
    # therefore unlisted by ToolRegistry.list_tools() and refused by call().
    allow_write: bool = False
    allow_delete: bool = False

    # -- Session lifetime -----------------------------------------------------
    # Absolute per-session TTL in ms. A session expires this long after its
    # creation (mirrors the fixed Siebel REST session lifetime). Must stay
    # above any caller's session-cache TTL so the caller re-authenticates
    # before this server's session dies.
    session_timeout_ms: int = Field(default=30 * 60 * 1000, ge=60_000)

    # -- Tools ----------------------------------------------------------------
    # Kept as str so pydantic-settings never attempts JSON-decoding.
    # Use the disabled_tools property to get the parsed list.
    disabled_tools_raw: str = Field(default="", alias="SIEBEL_DISABLED_TOOLS")

    @property
    def disabled_tools(self) -> list[str]:
        """Explicit SIEBEL_DISABLED_TOOLS list plus the access-gate defaults.

        When allow_write / allow_delete are False (the default, read-only mode),
        the write tools and delete_record are appended here so the existing
        ToolRegistry filter (which reads this property) hides and refuses them.
        """
        explicit = [t.strip() for t in self.disabled_tools_raw.split(",") if t.strip()]
        gated: list[str] = []
        if not self.allow_write:
            gated.extend(WRITE_TOOLS)
        if not self.allow_delete:
            gated.extend(DELETE_TOOLS)
        return explicit + gated

    # -- OAuth ----------------------------------------------------------------
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    keycloak_token_url: str = ""

    # -- Secrets (prefixed to stay consistent with SIEBEL_ namespace) ---------
    brave_search_api_key: str = ""
    api_token: str = ""

    @model_validator(mode="after")
    def _compose_url(self) -> AppSettings:
        """If base_url is empty and SIEBEL_HOST is set, build the URL from host+scheme."""
        if self.base_url or not self.host:
            return self
        host = self.host.rstrip("/")
        if "://" in host:
            url = host if host.endswith("/siebel/v1.0") else f"{host}/siebel/v1.0"
        else:
            suffix = "" if host.endswith("/siebel/v1.0") else "/siebel/v1.0"
            url = f"{self.scheme}://{host}{suffix}"
        self.base_url = url
        return self


def load_settings() -> AppSettings:
    return AppSettings()
