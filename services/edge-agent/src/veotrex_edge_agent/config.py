from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="VEOTREX_EDGE_", extra="ignore", case_sensitive=False
    )

    node_id: UUID
    environment: str = Field(default="local", min_length=1)
    version: str = Field(default="0.1.0", min_length=1)
    log_level: str = "INFO"
    heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    service_name: str = "veotrex-edge-agent"
