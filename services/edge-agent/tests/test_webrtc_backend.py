import os
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from veotrex_edge_agent.camera_transport.backend import (
    BackendFailed,
    DecodedCaps,
    MediaBatch,
    MediaNegotiated,
)
from veotrex_edge_agent.camera_transport.descriptor import (
    LOCAL_FIXTURE_ENDPOINT_POLICY,
    NO_CREDENTIAL,
    LiveSessionDescriptor,
    LiveSessionLease,
    ProviderKind,
    VideoCodec,
    validate_endpoint,
)
from veotrex_edge_agent.camera_transport.errors import TransportError
from veotrex_edge_agent.camera_transport.errors import TransportErrorCategory as C
from veotrex_edge_agent.camera_transport.webrtc_backend import (
    WebRtcBackendConfig,
    WebRtcMediaBackend,
)
from veotrex_edge_agent.camera_transport.webrtc_media import (
    REQUIRED_ICE_ELEMENTS,
    WhepMediaBackend,
    probe_webrtc_runtime,
)

SYSTEM_PYTHON = Path("/usr/bin/python3")
CAMERA = UUID(int=0x7E60)
pytestmark = pytest.mark.skipif(not SYSTEM_PYTHON.exists(), reason="system Python is unavailable")


def _webrtc_available() -> bool:
    if not SYSTEM_PYTHON.exists():
        return False
    probe = (
        "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None);"
        "import sys; sys.exit(0 if all(Gst.ElementFactory.find(n) for n in "
        "('webrtcbin','nicesrc','nicesink','nvv4l2decoder','x264enc','videotestsrc')) else 1)"
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal probe
        [str(SYSTEM_PYTHON), "-I", "-c", probe], capture_output=True, timeout=30, check=False
    )
    return result.returncode == 0


WEBRTC = _webrtc_available()


def lease(generation: int = 1) -> LiveSessionLease:
    return LiveSessionLease(
        LiveSessionDescriptor(
            provider=ProviderKind.LOCAL_FIXTURE,
            logical_camera_id=CAMERA,
            generation=generation,
            endpoint=validate_endpoint(
                "rtsp://127.0.0.1:9/synthetic-webrtc", LOCAL_FIXTURE_ENDPOINT_POLICY
            ),
            created_monotonic=time.monotonic(),
        ),
        NO_CREDENTIAL,
    )


def _fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


# --------------------------------------------------------------------- config + guards
def test_backend_config_bounds_are_validated() -> None:
    for bad in (
        {"decoder": "ffmpeg"},
        {"stun_server": "http://stun.example"},
        {"gather_timeout_seconds": 0},
        {"gather_timeout_seconds": 120},
        {"hello_timeout_seconds": 0},
        {"offer_timeout_seconds": 900},
    ):
        with pytest.raises(ValueError):
            WebRtcBackendConfig(**bad)  # type: ignore[arg-type]
    assert WebRtcBackendConfig().codec is VideoCodec.H264


def test_generation_and_worker_path_are_validated(tmp_path: Path) -> None:
    backend = WebRtcMediaBackend(2, lambda offer, _l: offer)
    with pytest.raises(TransportError) as caught:
        backend.start(lease(1), lambda _e: None)
    assert caught.value.category is C.INTERNAL_TRANSPORT_ERROR
    rogue = tmp_path / "webrtc_worker.py"
    rogue.write_text("print('x')")
    with pytest.raises(TransportError):
        WebRtcMediaBackend(1, lambda offer, _l: offer, worker_path=rogue).start(
            lease(1), lambda _e: None
        )


def test_whep_media_backend_gates_on_runtime_then_exchange() -> None:
    unavailable = WhepMediaBackend(
        1, probe=lambda: probe_webrtc_runtime(element_present=lambda _n: False)
    )
    with pytest.raises(TransportError) as caught:
        unavailable.start(lease(), lambda _e: None)
    assert caught.value.category is C.WEBRTC_RUNTIME_UNAVAILABLE
    assert unavailable.pid is None
    unavailable.stop()
    # Runtime present but no offer/answer exchange configured: fails closed, spawns nothing.
    no_exchange = WhepMediaBackend(
        1, probe=lambda: probe_webrtc_runtime(element_present=lambda _n: True)
    )
    with pytest.raises(TransportError) as caught:
        no_exchange.start(lease(), lambda _e: None)
    assert caught.value.category is C.PROVIDER_NOT_CONFIGURED
    assert no_exchange.delegate is None


def test_runtime_probe_reflects_installed_ice_elements() -> None:
    report = probe_webrtc_runtime()
    assert report.available is WEBRTC or not WEBRTC
    injected = probe_webrtc_runtime(element_present=lambda n: n not in REQUIRED_ICE_ELEMENTS)
    assert injected.available is False
    assert injected.missing_package_hint == "gstreamer1.0-nice"


# --------------------------------------------------------------------- event translation
def test_translate_accepts_valid_events_and_rejects_untrusted_shapes() -> None:
    backend = WebRtcMediaBackend(7, lambda offer, _l: offer)
    batch = backend.translate(
        {
            "type": "MEDIA",
            "generation": 7,
            "compressed": [[1_000_000_000, -1, 5, 1200]],
            "decoded": [[1_000_000_100, 42]],
        }
    )
    assert isinstance(batch, MediaBatch)
    assert batch.compressed[0].pts_ns is None and batch.decoded[0].pts_ns == 42
    caps = backend.translate(
        {
            "type": "CAPS",
            "generation": 7,
            "width": 1280,
            "height": 720,
            "framerate": 15.0,
            "nvmm": True,
        }
    )
    assert isinstance(caps, DecodedCaps) and caps.nvmm
    negotiated = backend.translate(
        {
            "type": "NEGOTIATED",
            "generation": 7,
            "codec": "H264",
            "decoder": "nvv4l2decoder",
            "hardware_decoder": True,
        }
    )
    assert isinstance(negotiated, MediaNegotiated) and negotiated.hardware_decoder
    failed = backend.translate(
        {"type": "FAILED", "generation": 7, "category": "rtsp://user:pw@host/x"}
    )
    assert isinstance(failed, BackendFailed) and failed.category is C.INTERNAL_TRANSPORT_ERROR
    assert backend.translate({"type": "FAILED", "generation": 7, "category": "WEBRTC_ICE_FAILED"})
    heartbeat = backend.translate(
        {
            "type": "HEARTBEAT",
            "generation": 7,
            "ice_state": "connected",
            "connection_state": "connected",
            "dropped_samples": 3,
        }
    )
    assert heartbeat is not None and backend.worker_dropped_samples == 3
    for bad in (
        {"type": "EXEC", "generation": 7},
        {"type": "MEDIA", "generation": 8, "compressed": [], "decoded": []},
        {"type": "MEDIA", "generation": 7, "compressed": [[True, 1, 1, 1]], "decoded": []},
        {"type": "MEDIA", "generation": 7, "compressed": [[1, 1, 1, 1]] * 401, "decoded": []},
        {"type": "CAPS", "generation": 7, "width": 10**9, "height": 1, "framerate": 1},
        {"type": "NEGOTIATED", "generation": 7, "codec": "VP9", "decoder": None},
        {"type": "NEGOTIATED", "generation": 7, "codec": "H264", "decoder": "filesink"},
    ):
        with pytest.raises((ValueError, TypeError, KeyError)):
            backend.translate(bad)


# --------------------------------------------------------------------- live runtime
@pytest.mark.skipif(not WEBRTC, reason="WebRTC/libnice/NVDEC stack unavailable")
def test_exchange_failure_is_reported_and_worker_reaped() -> None:
    before = _fds()

    def failing_exchange(_offer: str, _lease: LiveSessionLease) -> str:
        raise RuntimeError("exchange unavailable")

    backend = WebRtcMediaBackend(1, failing_exchange)
    with pytest.raises(TransportError) as caught:
        backend.start(lease(), lambda _e: None)
    assert caught.value.category is C.WHEP_OFFER_FAILED
    assert backend.offer_candidates is not None and backend.offer_candidates > 0
    backend.stop()
    assert backend.pid is None
    assert _fds() == before


@pytest.mark.skipif(not WEBRTC, reason="WebRTC/libnice/NVDEC stack unavailable")
@pytest.mark.parametrize("answer", ["", "not-an-sdp", "v=0\r\n" + "a=x\r\n" * 20_000])
def test_invalid_answers_are_refused_before_reaching_the_worker(answer: str) -> None:
    backend = WebRtcMediaBackend(1, lambda _o, _l: answer)
    with pytest.raises(TransportError) as caught:
        backend.start(lease(), lambda _e: None)
    assert caught.value.category is C.WHEP_INVALID_ANSWER
    backend.stop()
    assert backend.pid is None


@pytest.mark.skipif(not WEBRTC, reason="WebRTC/libnice/NVDEC stack unavailable")
def test_offer_is_complete_video_only_and_recvonly() -> None:
    captured: dict[str, Any] = {}

    def capture(offer: str, _lease: LiveSessionLease) -> str:
        captured["offer"] = offer
        raise RuntimeError("stop here")

    backend = WebRtcMediaBackend(1, capture)
    with pytest.raises(TransportError):
        backend.start(lease(), lambda _e: None)
    backend.stop()
    offer = captured["offer"]
    media_lines = [line for line in offer.splitlines() if line.startswith("m=")]
    assert media_lines and all(line.startswith("m=video") for line in media_lines)
    assert "a=recvonly" in offer
    assert sum(1 for line in offer.splitlines() if line.startswith("a=candidate")) > 0
    assert "a=fingerprint" in offer and "a=ice-ufrag" in offer
