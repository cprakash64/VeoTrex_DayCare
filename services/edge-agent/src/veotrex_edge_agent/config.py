import os
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="VEOTREX_EDGE_", extra="ignore", case_sensitive=False
    )

    node_id: UUID
    environment: str = Field(default="local", min_length=1)
    version: str = Field(default="0.1.0", min_length=1)
    # Provenance of the running build, supplied by the release the service was started from
    # (see infra/jetson: release.env). "unknown" when run from a working tree by hand.
    source_commit: str = Field(default="unknown", min_length=1, max_length=64)
    log_level: str = "INFO"
    heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    service_name: str = "veotrex-edge-agent"
    # Brokered Ring WHEP (V1-DEMO-03B). The HTTPS origin of the VeoTrex control plane, and the
    # absolute path of the 0600 file holding this node's machine credential. The credential
    # itself is never configuration: only where to read it from. Both empty = broker disabled.
    control_plane_url: str = Field(default="", max_length=256)
    credential_file: str = Field(default="", max_length=4096, repr=False)

    @model_validator(mode="after")
    def broker_configuration_is_safe(self) -> "EdgeSettings":
        if self.control_plane_url:
            from veotrex_edge_agent.camera_transport.broker_whep import ControlPlaneEndpoint

            # Raises ValueError with a shape-only message; the URL carries no secret.
            ControlPlaneEndpoint.parse(self.control_plane_url, environment=self.environment)
        if self.credential_file and not os.path.isabs(self.credential_file):
            raise ValueError("VEOTREX_EDGE_CREDENTIAL_FILE must be an absolute path")
        return self
