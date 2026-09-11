from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="VEOTREX_", extra="ignore", case_sensitive=False
    )

    environment: str = Field(min_length=1)
    log_level: str = "INFO"
    database_url: SecretStr
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
