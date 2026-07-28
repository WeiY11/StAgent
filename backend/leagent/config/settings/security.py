"""Security settings sub-module."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecuritySettings(BaseSettings):
    """HTTP security hardening knobs.

    ``enforce_auth`` is tri-state: ``None`` (auto — enforce on non-loopback
    binds), ``True`` (always), ``False`` (never). Rate limiting auto-enables
    when auth is effectively enforced unless explicitly disabled with
    ``rate_limit_auto_with_auth=false``.
    """

    model_config = SettingsConfigDict(env_prefix="LEAGENT_SECURITY_")

    #: Comma-separated list of allowed CORS origins. ``*`` allows any origin
    #: (credentials are then automatically disabled per the CORS spec).
    cors_allow_origins: str = "*"
    cors_allow_credentials: bool = True
    #: Comma-separated allowed Host header values for ``TrustedHostMiddleware``.
    #: ``*`` disables host checking.
    trusted_hosts: str = "*"
    security_headers_enabled: bool = True
    #: Send HSTS (only meaningful behind HTTPS). Off by default for local http.
    hsts_enabled: bool = False
    rate_limit_enabled: bool | None = None
    rate_limit_auto_with_auth: bool = True
    rate_limit_per_minute: int = Field(default=300, ge=1)
    rate_limit_burst: int = Field(default=60, ge=1)
    #: ``None`` = auto (enforce when bind host is not loopback / not desktop).
    enforce_auth: bool | None = None
    #: Gate ``/api/v1/meta`` and ``/api/v1/metrics`` behind auth when enforced.
    gate_diagnostics: bool = True
    #: Allow OpenAPI docs only when authenticated (when auth enforced + debug).
    gate_openapi: bool = True
    #: Hard cap on streamed request bodies (defends against header-less / chunked
    #: uploads that bypass a ``Content-Length`` check). 0 disables the streaming cap.
    max_streaming_body_bytes: int = Field(default=0, ge=0)
    #: Soft per-user concurrent agent/sandbox quota (0 = unlimited beyond global).
    max_concurrent_per_user: int = Field(default=5, ge=0)

    def cors_origins_list(self) -> list[str]:
        raw = (self.cors_allow_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    def trusted_hosts_list(self) -> list[str]:
        raw = (self.trusted_hosts or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [h.strip() for h in raw.split(",") if h.strip()]
