import asyncio
from uuid import UUID

import pytest
from pydantic import SecretStr

from veotrex_edge_agent.qualification.backend import QualificationEnvironmentError
from veotrex_edge_agent.qualification.gstreamer import (
    GStreamerQualificationBackend,
    codec_route,
    ring_rtsps_url,
)
from veotrex_edge_agent.qualification.models import (
    CameraTarget,
    QualificationMode,
    SessionClass,
    SessionRequest,
)


def test_h264_and_h265_codec_routes_are_dynamic() -> None:
    assert codec_route("H264").depayloader == "rtph264depay"
    assert codec_route("h265").parser == "h265parse"
    assert codec_route("HEVC").codec == "H265"


def test_unknown_codec_is_rejected_safely() -> None:
    with pytest.raises(QualificationEnvironmentError, match="unsupported"):
        codec_route("VP9")


def test_ring_url_is_rtsps_tcp_port_and_has_no_credential() -> None:
    url = ring_rtsps_url("ava1.device/opaque", "lens 1")
    assert url.startswith("rtsps://video.rtsp.amazonvision.com:322/")
    assert "ava1.device%2Fopaque" in url
    assert "component_id=lens%201" in url
    assert "token" not in url
    assert "@" not in url


async def test_verbose_gstreamer_debug_is_rejected_before_credential_unwrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GST_DEBUG", "6")
    request = SessionRequest(
        CameraTarget(UUID(int=1), "camera-01", "private"),
        QualificationMode.TRANSPORT,
        SessionClass.BATTERY_30_SECONDS,
        1,
    )
    with pytest.raises(QualificationEnvironmentError, match="diagnostics"):
        await GStreamerQualificationBackend().run_session(
            request, SecretStr("fixture-secret"), asyncio.Event()
        )
