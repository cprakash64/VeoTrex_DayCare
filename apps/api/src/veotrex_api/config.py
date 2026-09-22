from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VEOTREX_",
        extra="ignore",
        case_sensitive=False,
        # Only ``ring_api_proxy_url`` carries an explicit alias; without this, that one field
        # could no longer be set by its own name from application or test code.
        populate_by_name=True,
    )

    environment: str = Field(min_length=1)
    log_level: str = "INFO"
    database_url: SecretStr
    # Alternative to supplying the URL directly: a reference resolved through SecretResolver,
    # so a deployment can mount the DSN as a file instead of exposing it in the process
    # environment, where `docker inspect` and /proc/<pid>/environ would reveal it.
    database_url_ref: str = Field(default="", repr=False)
    # Schema migrations need DDL and object ownership that the API runtime role must never
    # hold. When set, Alembic connects with this DSN instead of ``database_url``; when unset,
    # Alembic falls back to ``database_url`` so a deployment that already supplies the admin
    # DSN to its one-shot migration job is unchanged. The API process never reads it.
    migration_database_url: SecretStr | None = Field(default=None, repr=False)
    migration_database_url_ref: str = Field(default="", repr=False)
    app_version: str = Field(min_length=1)
    service_name: str = "veotrex-api"
    oidc_issuer: str = "https://auth.example.invalid/"
    oidc_audience: str = "https://api.example.invalid"
    oidc_allowed_algorithms: str = "RS256"
    oidc_jwks_cache_ttl_seconds: int = Field(default=300, gt=0, le=3600)
    oidc_jwks_forced_refresh_cooldown_seconds: int = Field(default=10, ge=1, le=300)
    oidc_http_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    oidc_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    ring_oauth_token_url: str = "https://oauth.ring.com/oauth/token"  # noqa: S105 (public URL)
    ring_api_base_url: str = "https://api.amazonvision.com"
    # Optional egress proxy for Ring API (control-plane) requests ONLY - never Ring OAuth token
    # exchange, never Auth0/JWKS, never any other outbound traffic. Temporary unblocker for a
    # deployment whose direct network path to ``ring_api_base_url`` is rejected upstream while
    # the identical request succeeds from another egress. Unset (the default) leaves every
    # request going out directly, exactly as before.
    #
    # Accepted as VEOTREX_RING_API_PROXY_URL (the repository convention) and, because the proxy
    # is provisioned under that name, as a bare RING_API_PROXY_URL. Held ``repr=False``: a proxy
    # URL may carry credentials in other deployments, so it must not reach a log through a
    # settings repr.
    ring_api_proxy_url: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("VEOTREX_RING_API_PROXY_URL", "RING_API_PROXY_URL"),
    )
    ring_client_id: str = "replace-with-ring-client-id"
    ring_client_secret_ref: str = Field(default="env:RING_CLIENT_SECRET", repr=False)
    ring_hmac_signing_key_ref: str = Field(default="env:RING_HMAC_SIGNING_KEY", repr=False)
    ring_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    ring_read_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    ring_write_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    ring_nonce_validation_window_seconds: int = Field(default=600, ge=1, le=600)
    ring_nonce_future_tolerance_seconds: int = Field(default=0, ge=0, le=30)
    ring_access_token_refresh_margin_seconds: int = Field(default=300, ge=30, le=3600)
    ring_pending_candidate_max_age_seconds: int = Field(default=3600, ge=600, le=86400)
    ring_max_access_token_lifetime_seconds: int = Field(default=604800, ge=1, le=2592000)
    ring_max_response_bytes: int = Field(default=65536, ge=1024, le=1048576)
    ring_token_exchange_body_bytes: int = Field(default=1024, ge=128, le=8192)
    ring_token_exchange_rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    ring_inventory_retry_attempts: int = Field(default=3, ge=1, le=5)
    ring_inventory_max_pages: int = Field(default=10, ge=1, le=50)
    ring_inventory_max_devices: int = Field(default=500, ge=1, le=5000)
    ring_inventory_max_component_reads: int = Field(default=2000, ge=1, le=10000)
    ring_inventory_backoff_max_seconds: float = Field(default=10.0, ge=0, le=60)
    ring_webhook_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    # Externally reachable HTTPS origin of the control plane. Ring callback URLs are derived from
    # this configured value and never from an incoming Host/X-Forwarded-Host header. Empty until a
    # real deployment hostname exists; readiness then reports it as missing rather than guessing.
    public_origin: str = ""
    # Reference (never the value) to the vault AEAD master key, resolved through SecretResolver.
    vault_master_key_ref: str = Field(default="env:VEOTREX_VAULT_MASTER_KEY", repr=False)
    # Staff enrollment (V1-02A). Enrollment photos are written under this private directory
    # with opaque server-generated keys; it must be writable by the API process only.
    staff_media_dir: str = Field(default="/var/lib/veotrex/staff-media", min_length=1)
    staff_enrollment_image_bytes: int = Field(default=8_388_608, ge=65_536, le=33_554_432)
    staff_enrollment_upload_rate_limit_per_minute: int = Field(default=60, ge=1, le=600)
    # Face-template backend: "unavailable" (production default until a licence-approved model
    # is adopted; uploads are refused with a bounded category) or "fake" (deterministic,
    # test/local only; never a real biometric). A real backend is a later, explicit decision.
    staff_face_backend: str = Field(default="unavailable", pattern=r"^(unavailable|fake)$")

    @model_validator(mode="after")
    def fake_face_backend_only_outside_production(self) -> "Settings":
        if self.staff_face_backend == "fake" and self.environment not in {
            "test",
            "local",
            "development",
            "ci",
        }:
            raise ValueError("the fake face backend is permitted only in test/local environments")
        return self

    @model_validator(mode="before")
    @classmethod
    def resolve_database_url_reference(cls, data: Any) -> Any:
        """Populate ``database_url`` from ``database_url_ref`` when a reference is configured.

        Runs before field validation so ``database_url`` stays a required ``SecretStr`` and every
        consumer is unchanged. Configuring both is refused rather than silently preferring one,
        and configuring neither still fails as a missing required field.
        """
        if not isinstance(data, dict):
            return data
        for field_name in ("database_url", "migration_database_url"):
            reference = str(data.get(f"{field_name}_ref") or "").strip()
            if not reference:
                continue
            if data.get(field_name):
                raise ValueError(f"configure either {field_name} or {field_name}_ref, never both")
            from veotrex_api.secrets import DefaultSecretResolver, SecretResolutionError

            try:
                resolved = DefaultSecretResolver().resolve(reference)
            except SecretResolutionError as exc:
                # The resolver's messages never contain the secret value.
                raise ValueError(f"{field_name}_ref is unusable: {exc}") from None
            data[field_name] = resolved.get_secret_value()
        return data

    @property
    def effective_migration_database_url(self) -> SecretStr:
        """The DSN Alembic must use: the migration identity when configured, else the default."""
        return self.migration_database_url or self.database_url

    @field_validator("public_origin")
    @classmethod
    def public_origin_must_be_a_bare_https_origin(cls, value: str) -> str:
        if not value:
            return value
        from veotrex_api.public_origin import InvalidPublicOrigin, validate_public_origin

        try:
            return validate_public_origin(value).origin
        except InvalidPublicOrigin as exc:
            raise ValueError(str(exc)) from None

    @field_validator("oidc_issuer")
    @classmethod
    def issuer_must_be_absolute_https_url(cls, value: str) -> str:
        if not value.startswith("https://") or not value.endswith("/"):
            raise ValueError("OIDC issuer must be an absolute HTTPS URL ending in '/'")
        return value

    @field_validator("ring_oauth_token_url", "ring_api_base_url")
    @classmethod
    def ring_urls_must_use_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Ring endpoints must use HTTPS")
        return value

    @field_validator("ring_api_proxy_url", mode="before")
    @classmethod
    def ring_api_proxy_url_must_be_a_supported_proxy(cls, value: Any) -> str | None:
        """Reject an unusable proxy at startup rather than at the first Ring API call.

        An empty or whitespace-only value means "unset", so an env var left blank behaves the
        same as one that was never set. Error messages describe the shape only and never echo
        the configured value, which may carry proxy credentials.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Ring API proxy URL must be a string")
        candidate = value.strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme not in {"socks5", "socks5h", "http", "https"}:
            raise ValueError("Ring API proxy URL must use socks5, socks5h, http or https")
        if not parsed.hostname:
            raise ValueError("Ring API proxy URL must include a host")
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("Ring API proxy URL has an out-of-range port") from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Ring API proxy URL has an out-of-range port")
        return candidate

    @property
    def oidc_algorithms(self) -> tuple[str, ...]:
        algorithms = tuple(value.strip() for value in self.oidc_allowed_algorithms.split(","))
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
        if not algorithms or any(value not in allowed for value in algorithms):
            raise ValueError("OIDC algorithms must be an explicit asymmetric allowlist")
        return algorithms


@lru_cache
def get_settings() -> Settings:
    return Settings()
