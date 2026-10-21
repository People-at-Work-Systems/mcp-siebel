"""Runtime configuration for the Siebel MCP service — environment-variable driven."""

from __future__ import annotations

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIEBEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Field names are also accepted as population sources (init kwarg or
        # prefix env var) alongside any explicit alias. Without this, an aliased
        # field could only be set via its alias.
        populate_by_name=True,
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
    # TLS verification for outbound calls to Siebel. Default true (verify the
    # server certificate). Against a self-signed dev Siebel, set SIEBEL_CA_BUNDLE
    # to the CA bundle path (verification stays on, or SIEBEL_VERIFY_TLS=false to
    # disable verification (dev only).
    verify_tls: bool = True
    ca_bundle: str = ""

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
    # that grant guest read access), the probe can't detect an invalid header
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

    # -- Session lifetime -----------------------------------------------------
    # Absolute per-session TTL in ms. A session expires this long after its
    # creation (mirrors the fixed Siebel REST session lifetime). Must stay
    # above any caller's session-cache TTL so the caller re-authenticates
    # before this server's session dies.
    session_timeout_ms: int = Field(default=30 * 60 * 1000, ge=60_000)

    # -- Tools ----------------------------------------------------------------
    # Path to an external tool-spec JSON file (name/description/inputSchema per
    # tool). Empty = use the bundled mcp_siebel/specs/tool_specs.json. The
    # registry hot-reloads this file by mtime, so edits take effect on the next
    # tools/list with no restart. Mirrors SIEBEL_SYSTEM_PROMPT_PATH.
    tools_spec_path: str = Field(default="", alias="SIEBEL_TOOLS_SPEC_PATH")

    # Whether to start a watchfiles file watcher that pushes spec changes to the
    # registry as they happen (instead of waiting for the next tools/list). The
    # watcher watches the spec file's directory and filters to the spec path, so
    # an editor's atomic temp-file + rename replace is detected; bursts are
    # debounced (~300 ms). Default true; set false to disable. The lazy mtime
    # check in list_tools() stays as a fallback either way. Python handler
    # modules are NOT watched — a code change still needs a restart; only the
    # JSON spec is hot-reloaded.
    tools_spec_watch: bool = Field(default=True, alias="SIEBEL_TOOLS_SPEC_WATCH")

    # -- Secrets (prefixed to stay consistent with SIEBEL_ namespace) ---------
    brave_search_api_key: str = ""
    # Bearer token guarding the HTTP/SSE server. Accepts both MCP_API_TOKEN
    # (the documented name) and SIEBEL_API_TOKEN (the prefixed form) so existing
    # deployments keep working. Empty = no auth (server is open; a startup
    # warning is logged).
    api_token: str = Field(
        default="",
        validation_alias=AliasChoices("MCP_API_TOKEN", "SIEBEL_API_TOKEN"),
    )

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
