"""Demo runtime configuration.

Separate from ``EdgeSettings`` (``VEOTREX_EDGE_``) because this configures a demonstration
runtime rather than the edge agent itself, and the operator-facing variable names are the
ones written down in the demo runbook.

No path is baked in. ``VEOTREX_DEMO_VIDEO_PATH`` has no default: the runtime refuses to start
rather than reaching for somebody's home directory.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from veotrex_edge_agent.frame_source.recorded_video import VideoDecoder


class DemoRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="VEOTREX_DEMO_", extra="ignore", case_sensitive=False
    )

    video_path: Path
    video_loop: bool = True
    cadence_fps: int = Field(default=5, ge=1, le=15)
    decoder: VideoDecoder = VideoDecoder.SOFTWARE
    jpeg_quality: int = Field(default=85, ge=40, le=95)

    area_label: str = Field(default="Demo Classroom", min_length=1, max_length=80)
    camera_label: str = Field(default="Demo Camera", min_length=1, max_length=80)

    # Operator-declared, never inferred from imagery. Leaving staff_on_duty at 0 disables
    # the threshold card entirely instead of showing a made-up compliance state.
    staff_on_duty: int = Field(default=0, ge=0, le=50)
    people_per_staff: int = Field(default=4, ge=1, le=50)

    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8878, ge=1024, le=65535)
    stale_after_seconds: float = Field(default=3.0, gt=0, le=60)

    @field_validator("area_label", "camera_label")
    @classmethod
    def labels_are_plain_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("label must not be blank")
        if any(character in cleaned for character in "<>"):
            raise ValueError("label must not contain markup characters")
        return cleaned
