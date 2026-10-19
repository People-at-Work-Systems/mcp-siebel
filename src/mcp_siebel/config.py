"""Runtime configuration for the Siebel MCP service — environment-variable driven."""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # -- Access gates (default off = read-only) -------------------------------
    # When False, tools whose spec entry has access "write" / "delete" /
    # "external" are hidden by ToolRegistry.list_tools() and refused by call().
    # The per-tool access level lives in the spec; these flags are the on/off
    # switch. ``external`` covers tools that send free text to an outside
    # service (e.g. web search) — off by default so a bank deployment can't
    # leak customer data into a search query without an explicit opt-in.
    allow_write: bool = False
    allow_delete: bool = False
    allow_external: bool = False

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

    @property
    def disabled_tools(self) -> list[str]:
        """The explicit SIEBEL_DISABLED_TOOLS list only.

        Access gating (write/delete) is enforced by ToolRegistry from each
        spec entry's ``access`` field against allow_write/allow_delete — it no
        longer lives here, so a write tool added via JSON can't bypass the
        read-only default. always_enabled tools are exempt from this list.
        """
        return [t.strip() for t in self.disabled_tools_raw.split(",") if t.strip()]

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
