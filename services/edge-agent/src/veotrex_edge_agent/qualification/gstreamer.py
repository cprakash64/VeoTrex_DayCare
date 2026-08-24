from __future__ import annotations

import asyncio
import os
import platform
import time
from dataclasses import dataclass
from itertools import pairwise
from typing import Any
from urllib.parse import quote

from pydantic import SecretStr

from veotrex_edge_agent.qualification.backend import QualificationEnvironmentError
from veotrex_edge_agent.qualification.metrics import BoundedSamples
from veotrex_edge_agent.qualification.models import (
    QualificationMode,
    SessionRequest,
    SessionResult,
    TerminationReason,
)

RING_RTSPS_HOST = "video.rtsp.amazonvision.com"
RING_RTSPS_PORT = 322


@dataclass(frozen=True, slots=True)
class CodecRoute:
    codec: str
    depayloader: str
    parser: str
    software_decoders: tuple[str, ...]


CODEC_ROUTES = {
    "H264": CodecRoute("H264", "rtph264depay", "h264parse", ("avdec_h264", "openh264dec")),
    "H265": CodecRoute("H265", "rtph265depay", "h265parse", ("avdec_h265", "libde265dec")),
}


def codec_route(encoding_name: str) -> CodecRoute:
    normalized = encoding_name.upper().replace(".", "")
    if normalized in {"H264", "AVC"}:
        return CODEC_ROUTES["H264"]
    if normalized in {"H265", "HEVC"}:
        return CODEC_ROUTES["H265"]
    raise QualificationEnvironmentError("negotiated video codec is unsupported")


def ring_rtsps_url(provider_device_id: str, provider_component_id: str | None = None) -> str:
    if not provider_device_id or any(character.isspace() for character in provider_device_id):
        raise ValueError("provider device identifier is invalid")
    path = quote(provider_device_id, safe="._~-")
    url = f"rtsps://{RING_RTSPS_HOST}:{RING_RTSPS_PORT}/v1/devices/{path}/stream"
    if provider_component_id is not None:
        if not provider_component_id or any(
            ord(character) < 32 for character in provider_component_id
        ):
            raise ValueError("provider component identifier is invalid")
        url += f"?component_id={quote(provider_component_id, safe='._~-')}"
    return url


def _load_gstreamer() -> tuple[Any, Any]:
    try:
        import gi  # type: ignore[import-not-found]

        gi.require_version("Gst", "1.0")
        gi.require_version("GstRtsp", "1.0")
        from gi.repository import Gst, GstRtsp  # type: ignore[import-not-found]
    except (ImportError, ValueError) as exc:
        raise QualificationEnvironmentError(
            "GStreamer/PyGObject is unavailable; run `veotrex-edge qualify-env`"
        ) from exc
    Gst.init(None)
    return Gst, GstRtsp


def _safe_failure(message: str) -> tuple[TerminationReason, str]:
    lowered = message.lower()
    if "401" in lowered or "unauthorized" in lowered:
        return TerminationReason.ACCESS_TOKEN_FAILURE, "access_token_failure"
    if "tls" in lowered or "certificate" in lowered:
        return TerminationReason.TLS_FAILURE, "tls_failure"
    if "offline" in lowered or "not found" in lowered:
        return TerminationReason.CAMERA_OFFLINE, "camera_offline"
    if "decode" in lowered:
        return TerminationReason.DECODER_FAILURE, "decoder_failure"
    return TerminationReason.NETWORK_DISCONNECT, "transport_failure"


class GStreamerQualificationBackend:  # pragma: no cover - exercised manually in Stage 1D-B
    """In-process RTSPS probe. It never invokes gst-launch or retains media buffers."""

    def __init__(self, *, latency_ms: int = 200, sample_capacity: int = 2048) -> None:
        if not 0 <= latency_ms <= 5000:
            raise ValueError("latency must be between 0 and 5000 ms")
        self._latency_ms = latency_ms
        self._sample_capacity = sample_capacity

    async def run_session(
        self,
        request: SessionRequest,
        access_token: SecretStr,
        cancel: asyncio.Event,
    ) -> SessionResult:
        if os.environ.get("GST_DEBUG") not in {None, "", "0"}:
            raise QualificationEnvironmentError(
                "verbose GStreamer diagnostics must be disabled for credential-bearing runs"
            )
        # The SecretStr is unwrapped only inside the worker and is never interpolated into a URL,
        # command line, exception, log, environment variable, or report.
        token = access_token.get_secret_value()
        try:
            return await asyncio.to_thread(self._run_sync, request, token, cancel)
        finally:
            token = ""  # Minimize the lifetime of the additional Python reference.

    def _run_sync(
        self, request: SessionRequest, token: str, cancel: asyncio.Event
    ) -> SessionResult:
        Gst, GstRtsp = _load_gstreamer()
        now = time.monotonic()
        result = SessionResult(session_number=request.session_number, requested_at=now)
        pipeline = Gst.Pipeline.new(f"qualification-{request.session_number}")
        source = Gst.ElementFactory.make("rtspsrc", "ring-source")
        if pipeline is None or source is None:
            raise QualificationEnvironmentError("required GStreamer rtspsrc plugin is unavailable")
        source.set_property(
            "location",
            ring_rtsps_url(request.target.provider_device_id, request.target.provider_component_id),
        )
        source.set_property("protocols", GstRtsp.RTSPLowerTrans.TCP)
        source.set_property("latency", self._latency_ms)
        source.set_property("user-id", "x")
        source.set_property("user-pw", token)
        pipeline.add(source)
        frame_gaps = BoundedSamples(self._sample_capacity)
        state: dict[str, float | int | str | None] = {
            "last_arrival": None,
            "last_pts": None,
            "last_dts": None,
            "last_audio_arrival": None,
        }

        def buffer_probe(_pad: Any, info: Any, decoded: bool) -> Any:
            buffer = info.get_buffer()
            if buffer is None:
                return Gst.PadProbeReturn.OK
            arrived = time.monotonic()
            if decoded:
                result.decoded_frames += 1
                if result.first_decoded_frame_at is None:
                    result.first_decoded_frame_at = arrived
                result.last_decoded_frame_at = arrived
                caps = _pad.get_current_caps()
                structure = caps.get_structure(0) if caps and caps.get_size() else None
                if structure is not None:
                    width_ok, width = structure.get_int("width")
                    height_ok, height = structure.get_int("height")
                    result.width = width if width_ok else result.width
                    result.height = height if height_ok else result.height
                return Gst.PadProbeReturn.OK
            result.media_buffers += 1
            result.encoded_bytes += buffer.get_size()
            if result.first_media_at is None:
                result.first_media_at = arrived
            result.last_media_at = arrived
            previous = state["last_arrival"]
            if isinstance(previous, float):
                gap_ms = (arrived - previous) * 1000
                frame_gaps.add(gap_ms)
                result.max_frame_gap_ms = max(result.max_frame_gap_ms or 0.0, gap_ms)
            state["last_arrival"] = arrived
            pts = int(buffer.pts)
            last_pts = state["last_pts"]
            if pts != Gst.CLOCK_TIME_NONE and isinstance(last_pts, int):
                if pts == last_pts:
                    result.repeated_pts += 1
                elif pts < last_pts:
                    result.pts_regressions += 1
            if pts != Gst.CLOCK_TIME_NONE:
                state["last_pts"] = pts
            dts = int(buffer.dts)
            last_dts = state["last_dts"]
            if dts != Gst.CLOCK_TIME_NONE and isinstance(last_dts, int):
                if dts == last_dts:
                    result.repeated_dts += 1
                elif dts < last_dts:
                    result.dts_regressions += 1
            if dts != Gst.CLOCK_TIME_NONE:
                state["last_dts"] = dts
            return Gst.PadProbeReturn.OK

        def audio_probe(_pad: Any, info: Any) -> Any:
            if info.get_buffer() is None:
                return Gst.PadProbeReturn.OK
            arrived = time.monotonic()
            previous = state["last_audio_arrival"]
            if isinstance(previous, float):
                gap_ms = (arrived - previous) * 1000
                result.max_audio_gap_ms = max(result.max_audio_gap_ms or 0.0, gap_ms)
            state["last_audio_arrival"] = arrived
            result.audio_buffers += 1
            return Gst.PadProbeReturn.OK

        def on_pad_added(_source: Any, pad: Any) -> None:
            caps = pad.get_current_caps() or pad.query_caps(None)
            structure = caps.get_structure(0) if caps and caps.get_size() else None
            if structure is None or structure.get_string("media") != "video":
                if structure is not None and structure.get_string("media") == "audio":
                    result.audio_present = True
                    result.audio_codec = structure.get_string("encoding-name")
                    encoding = (result.audio_codec or "").upper()
                    depay_name = {"OPUS": "rtpopusdepay", "PCMU": "rtppcmudepay"}.get(encoding)
                    if depay_name:
                        depay = Gst.ElementFactory.make(depay_name, "qualification-audio-depay")
                        audio_sink = Gst.ElementFactory.make("fakesink", "qualification-audio-sink")
                        if depay is not None and audio_sink is not None:
                            audio_sink.set_property("sync", False)
                            pipeline.add(depay)
                            pipeline.add(audio_sink)
                            depay.sync_state_with_parent()
                            audio_sink.sync_state_with_parent()
                            if depay.link(audio_sink) and (
                                pad.link(depay.get_static_pad("sink")) == Gst.PadLinkReturn.OK
                            ):
                                audio_sink.get_static_pad("sink").add_probe(
                                    Gst.PadProbeType.BUFFER, audio_probe
                                )
                return
            encoding = structure.get_string("encoding-name") or ""
            try:
                route = codec_route(encoding)
                elements, sink, decoder = self._make_video_chain(Gst, request, route)
            except QualificationEnvironmentError:
                result.termination_reason = TerminationReason.CODEC_NEGOTIATION_FAILURE
                result.failure_category = "codec_negotiation_failure"
                pipeline.send_event(Gst.Event.new_eos())
                return
            result.codec = route.codec
            result.decoder = decoder
            for element in elements:
                pipeline.add(element)
                element.sync_state_with_parent()
            for left, right in pairwise(elements):
                if not left.link(right):
                    result.termination_reason = TerminationReason.APPLICATION_ERROR
                    result.failure_category = "pipeline_link_failure"
                    pipeline.send_event(Gst.Event.new_eos())
                    return
            if not pad.link(elements[0].get_static_pad("sink")) == Gst.PadLinkReturn.OK:
                result.termination_reason = TerminationReason.APPLICATION_ERROR
                result.failure_category = "pipeline_pad_link_failure"
                pipeline.send_event(Gst.Event.new_eos())
                return
            compressed_probe_pad = elements[1].get_static_pad("src")
            compressed_probe_pad.add_probe(
                Gst.PadProbeType.BUFFER,
                lambda probe_pad, info: buffer_probe(probe_pad, info, False),
            )
            if request.mode is QualificationMode.DECODE:
                decoded_probe_pad = sink.get_static_pad("sink")
                decoded_probe_pad.add_probe(
                    Gst.PadProbeType.BUFFER,
                    lambda probe_pad, info: buffer_probe(probe_pad, info, True),
                )

        source.connect("pad-added", on_pad_added)
        source.connect(
            "on-sdp",
            lambda _source, _sdp: setattr(result, "describe_completed_at", time.monotonic()),
        )
        result.connection_started_at = time.monotonic()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise QualificationEnvironmentError("GStreamer pipeline failed to enter PLAYING")
        result.play_started_at = time.monotonic()
        bus = pipeline.get_bus()
        maximum = request.session_class.expected_limit_seconds + max(
            10.0, request.stall_timeout_seconds
        )
        deadline = time.monotonic() + maximum
        try:
            while time.monotonic() < deadline:
                if cancel.is_set():
                    result.termination_reason = TerminationReason.CANCELLED
                    result.failure_category = "cancelled"
                    break
                message = bus.timed_pop_filtered(
                    100 * Gst.MSECOND,
                    Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.STATE_CHANGED,
                )
                current = time.monotonic()
                if (
                    result.last_media_at is not None
                    and current - result.last_media_at > request.stall_timeout_seconds
                ):
                    result.termination_reason = TerminationReason.NO_FRAME_STALL
                    result.failure_category = "no_frame_stall"
                    break
                if message is None:
                    continue
                if message.type == Gst.MessageType.ERROR:
                    error, _debug = message.parse_error()
                    reason, category = _safe_failure(error.message)
                    result.termination_reason = reason
                    result.failure_category = category
                    break
                if message.type == Gst.MessageType.EOS:
                    if result.failure_category is None:
                        observed = current - (result.connection_started_at or current)
                        if observed >= request.session_class.expected_limit_seconds - 2:
                            result.termination_reason = (
                                TerminationReason.PROVIDER_SESSION_EXPIRATION
                            )
                        else:
                            result.termination_reason = TerminationReason.END_OF_STREAM
                    break
            else:
                result.termination_reason = TerminationReason.OPERATOR_DURATION_REACHED
        finally:
            result.ended_at = time.monotonic()
            pipeline.send_event(Gst.Event.new_eos())
            pipeline.set_state(Gst.State.NULL)
            result.teardown_completed_at = time.monotonic()
            result.frame_gap_samples_ms = list(frame_gaps.values())
        return result

    @staticmethod
    def _make_video_chain(
        Gst: Any, request: SessionRequest, route: CodecRoute
    ) -> tuple[list[Any], Any, str | None]:
        names = [route.depayloader, route.parser]
        decoder_name: str | None = None
        if request.mode is QualificationMode.DECODE:
            jetson = platform.machine() in {"aarch64", "arm64"} and platform.system() == "Linux"
            preference = request.decoder_preference.lower()
            hardware_required = preference in {"nvidia", "hardware"} or (
                preference == "auto" and jetson
            )
            if hardware_required:
                if Gst.ElementFactory.find("nvv4l2decoder") is None:
                    raise QualificationEnvironmentError(
                        "NVIDIA hardware decoder required but nvv4l2decoder is unavailable"
                    )
                decoder_name = "nvv4l2decoder"
            else:
                decoder_name = next(
                    (name for name in route.software_decoders if Gst.ElementFactory.find(name)),
                    None,
                )
                if decoder_name is None:
                    raise QualificationEnvironmentError(
                        "no compatible software decoder is available"
                    )
            names.append(decoder_name)
        names.append("fakesink")
        elements = [
            Gst.ElementFactory.make(name, f"qualification-{index}-{name}")
            for index, name in enumerate(names)
        ]
        if any(element is None for element in elements):
            raise QualificationEnvironmentError("required GStreamer codec plugin is unavailable")
        sink = elements[-1]
        sink.set_property("sync", False)
        sink.set_property("async", False)
        return elements, sink, decoder_name
