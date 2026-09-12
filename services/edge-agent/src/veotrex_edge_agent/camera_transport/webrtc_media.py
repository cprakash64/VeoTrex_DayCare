"""WebRTC media-plane capability gate for WHEP.

`webrtcbin` is installed, but GStreamer's ICE agent lives in the libnice elements shipped by the
Ubuntu package `gstreamer1.0-nice`, which is **not installed** on this Jetson. Without it
`webrtcbin` cannot reach PLAYING ("libnice elements are not available") and `create-offer` returns
an empty promise, so no SDP offer can be produced and no WHEP media can flow.

Installing packages is outside this stage, so this module fails closed with
``WEBRTC_RUNTIME_UNAVAILABLE`` instead of silently degrading, and the probe is what the
qualification harness and the provider consult before any credential is touched.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veotrex_edge_agent.camera_transport.backend import BackendEvent
from veotrex_edge_agent.camera_transport.descriptor import LiveSessionLease
from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory

SYSTEM_PYTHON = Path("/usr/bin/python3")
# webrtcbin refuses to start without these; they are provided by gstreamer1.0-nice.
REQUIRED_ICE_ELEMENTS = ("nicesrc", "nicesink")
REQUIRED_WEBRTC_ELEMENTS = ("webrtcbin", "dtlssrtpenc", "dtlssrtpdec", "srtpenc", "srtpdec")
REQUIRED_DECODE_ELEMENTS = ("rtph264depay", "h264parse", "nvv4l2decoder")
_ICE_PACKAGE = "gstreamer1.0-nice"


@dataclass(frozen=True, slots=True)
class WebRtcRuntimeReport:
    available: bool
    missing_elements: tuple[str, ...]
    missing_package_hint: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "missing_elements": list(self.missing_elements),
            "missing_package_hint": self.missing_package_hint,
            "detail": self.detail,
        }


def _gst_inspect(name: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable name and literal element names
            ["/usr/bin/gst-inspect-1.0", name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def probe_webrtc_runtime(
    element_present: Callable[[str], bool] = _gst_inspect,
) -> WebRtcRuntimeReport:
    """Read-only capability probe. Never installs, never modifies the platform."""
    missing = tuple(
        name
        for name in REQUIRED_WEBRTC_ELEMENTS + REQUIRED_ICE_ELEMENTS + REQUIRED_DECODE_ELEMENTS
        if not element_present(name)
    )
    if not missing:
        return WebRtcRuntimeReport(True, (), None, "webrtc media elements present")
    ice_missing = [name for name in missing if name in REQUIRED_ICE_ELEMENTS]
    hint = _ICE_PACKAGE if ice_missing else None
    detail = (
        "webrtcbin cannot create an ICE agent: libnice elements are unavailable"
        if ice_missing
        else "required WebRTC media elements are unavailable"
    )
    return WebRtcRuntimeReport(False, missing, hint, detail)


class WhepMediaBackend:
    """Media backend for WHEP sessions, gated on the WebRTC runtime probe.

    On this host the probe fails, so `start` raises ``WEBRTC_RUNTIME_UNAVAILABLE`` before any
    credential is unwrapped or any process is spawned. The webrtcbin pipeline itself
    (webrtcbin -> depay -> parse -> nvv4l2decoder -> fakesink, video-only, recvonly) is
    deliberately not shipped unverified: it requires a separate package gate to install
    ``gstreamer1.0-nice``, after which it can be qualified exactly like the RTSP worker.
    """

    def __init__(
        self,
        generation: int,
        *,
        probe: Callable[[], WebRtcRuntimeReport] = probe_webrtc_runtime,
        exchange: Any = None,
        config: Any = None,
    ) -> None:
        self.generation = generation
        self._probe = probe
        self._exchange = exchange
        self._config = config
        self.report: WebRtcRuntimeReport | None = None
        self.delegate: Any = None

    @property
    def pid(self) -> int | None:
        return self.delegate.pid if self.delegate is not None else None

    def start(self, lease: LiveSessionLease, emit: Callable[[BackendEvent], None]) -> None:
        # Runtime capability is checked first so an unusable host fails before any credential use.
        self.report = self._probe()
        if not self.report.available:
            raise TransportError(TransportErrorCategory.WEBRTC_RUNTIME_UNAVAILABLE)
        if self._exchange is None:
            # No offer/answer exchange configured (Ring WHEP client or local qualification peer).
            raise TransportError(TransportErrorCategory.PROVIDER_NOT_CONFIGURED)
        from veotrex_edge_agent.camera_transport.webrtc_backend import WebRtcMediaBackend

        self.delegate = WebRtcMediaBackend(self.generation, self._exchange, self._config)
        self.delegate.start(lease, emit)

    def stop(self) -> None:
        if self.delegate is not None:
            self.delegate.stop()
