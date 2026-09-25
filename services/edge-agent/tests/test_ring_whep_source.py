"""Ring WHEP live-frame source (V1-DEMO-02).

Every test here runs with no Ring account, no OAuth, no network, no credential and no camera.
That is not a convenience: Ring account linking is blocked upstream by an Amazon-side IP-level
rejection, so if this adapter were only provable against a live Ring it would not be provable at
all. The session provider and the frame reader are protocols with deterministic fakes, and the
handful of tests that exercise the real GStreamer decode path skip themselves when the runtime
is absent rather than pretending to have run.
"""

from __future__ import annotations

import ast
import re
import socket
import threading
from itertools import islice
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from veotrex_edge_agent.camera_transport.reconnect import ReconnectPolicy
from veotrex_edge_agent.live import (
    FakeLiveSource,
    LiveDemoRuntime,
    LiveVideoSource,
    PreviewConfig,
    PreviewRenderer,
    SourceHealth,
    SourceKind,
)
from veotrex_edge_agent.live.ring import RingWhepSource, is_terminal
from veotrex_edge_agent.live.ring_fakes import (
    CountingThreadReader,
    FakeRingFrameReader,
    FakeRingSessionProvider,
    synthetic_scene,
)
from veotrex_edge_agent.live.ring_media import (
    BoundedFrameSlot,
    DecodedFrame,
    RingFrameReader,
    RingLiveSessionProvider,
    RingMediaError,
    RingMediaMetrics,
    RingSessionMaterial,
)
from veotrex_edge_agent.recorded.detector import FakePersonDetector
from veotrex_edge_agent.tracking import TrackingConfig

cv2 = pytest.importorskip("cv2", reason="the recorded-video dependency group is not installed")

CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src/veotrex_edge_agent"
LIVE_ROOT = SOURCE_ROOT / "live"
FAST = ReconnectPolicy(initial_delay_seconds=0.001, maximum_delay_seconds=0.002, max_attempts=3)


def build(
    reader: FakeRingFrameReader | None = None,
    provider: FakeRingSessionProvider | None = None,
    **kwargs: Any,
) -> tuple[RingWhepSource, FakeRingSessionProvider, FakeRingFrameReader]:
    provider = provider or FakeRingSessionProvider()
    reader = reader or FakeRingFrameReader(frame_count=6)
    kwargs.setdefault("reconnect_policy", FAST)
    kwargs.setdefault("first_frame_timeout_seconds", 0.5)
    kwargs.setdefault("stall_timeout_seconds", 0.5)
    return (RingWhepSource("test-cam", provider, reader, **kwargs), provider, reader)


# ------------------------------------------------------------------ the contract itself
def test_the_ring_source_satisfies_the_same_protocol_as_the_usb_camera() -> None:
    source, _, _ = build()
    assert isinstance(source, LiveVideoSource)
    for name in ("kind", "source_id", "health", "describe", "frames", "close"):
        assert hasattr(source, name), name


def test_frames_declare_themselves_as_ring_and_as_live() -> None:
    source, _, _ = build()
    produced = list(source.frames())
    assert produced
    assert source.kind is SourceKind.LIVE_RING_WHEP
    for frame in produced:
        assert frame.kind is SourceKind.LIVE_RING_WHEP
        assert SourceKind(frame.kind).is_live
    assert source.describe().kind is SourceKind.LIVE_RING_WHEP


def test_the_fakes_satisfy_the_protocols_they_stand_in_for() -> None:
    assert isinstance(FakeRingSessionProvider(), RingLiveSessionProvider)
    assert isinstance(FakeRingFrameReader(), RingFrameReader)


def test_a_source_can_be_built_and_closed_with_no_ring_and_no_hardware() -> None:
    source, provider, reader = build()
    source.close()
    assert provider.acquired == 0, "construction must not acquire anything"
    assert reader.started == 0


def test_an_empty_camera_id_is_refused() -> None:
    from veotrex_edge_agent.live.source import LiveSourceError

    with pytest.raises(LiveSourceError, match="invalid_ring_camera_id"):
        RingWhepSource("", FakeRingSessionProvider(), FakeRingFrameReader())


# --------------------------------------------------------------------- frames and timing
def test_the_first_frame_is_delivered() -> None:
    source, provider, reader = build()
    first = next(iter(source.frames()))
    assert first.frame_index == 0
    assert first.width == 320
    assert provider.acquired == 1
    assert reader.started == 1
    source.close()


def test_frame_indexes_are_monotonic_and_gapless_within_a_session() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=8))
    indexes = [frame.frame_index for frame in source.frames()]
    assert indexes == list(range(len(indexes)))
    assert len(indexes) == 8


def test_arrival_timestamps_are_monotonic() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=8))
    produced = list(source.frames())
    stamps = [frame.monotonic_ns for frame in produced]
    assert stamps == sorted(stamps)
    assert [frame.timestamp_ms for frame in produced] == sorted(
        frame.timestamp_ms for frame in produced
    )


def test_a_media_timestamp_is_carried_through_unchanged() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=4, pts_start_ms=500.0, pts_step_ms=40.0))
    produced = list(source.frames())
    assert [frame.capture_timestamp_ms for frame in produced] == [500.0, 540.0, 580.0, 620.0]


def test_an_absent_media_timestamp_stays_absent_and_never_becomes_zero() -> None:
    """None means the decoder gave no PTS. Zero would read as the start of the stream."""
    source, _, _ = build(FakeRingFrameReader(frame_count=3, pts_start_ms=None))
    produced = list(source.frames())
    assert all(frame.capture_timestamp_ms is None for frame in produced)


def test_the_media_clock_never_drives_continuity() -> None:
    """Arrival time is the timeline, exactly as it is for the USB camera."""
    source, _, _ = build(FakeRingFrameReader(frame_count=4, pts_start_ms=9_999_999.0))
    produced = list(source.frames())
    assert produced[0].timestamp_ms < 1000.0, "a large PTS must not move the session clock"


def test_a_reader_reported_discontinuity_reaches_the_frame() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=5, discontinuity_at=2))
    flags = [frame.discontinuity for frame in source.frames()]
    assert flags == [False, False, True, False, False]


def test_a_frame_the_pipeline_would_refuse_is_dropped_not_fatal() -> None:
    class TinyReader(FakeRingFrameReader):
        def read(self, timeout_seconds: float) -> DecodedFrame | None:
            frame = super().read(timeout_seconds)
            if frame is None or frame.width != 320:
                return frame
            # One frame with impossible geometry, then normal ones.
            return DecodedFrame(
                image=np.zeros((2, 2, 3), np.uint8),
                width=2,
                height=2,
                arrival_monotonic_ns=frame.arrival_monotonic_ns,
            )

    source, _, _ = build(TinyReader(frame_count=3))
    produced = list(source.frames())
    assert produced == []
    assert source.metrics.frames_dropped_total == 3, "dropped and counted, not raised"


# ------------------------------------------------------------------------- backpressure
def test_the_frame_slot_holds_exactly_one_frame() -> None:
    slot = BoundedFrameSlot()
    for index in range(20):
        slot.publish(
            DecodedFrame(synthetic_scene(32, 24, index), 32, 24, arrival_monotonic_ns=index)
        )
        assert slot.depth == 1, "the slot is a slot, never a queue"
    assert slot.stats.received_total == 20
    assert slot.stats.dropped_total == 19, "every overwritten frame is counted"


def test_the_slot_yields_the_newest_frame_not_the_oldest() -> None:
    slot = BoundedFrameSlot()
    for index in range(5):
        slot.publish(
            DecodedFrame(synthetic_scene(32, 24, index), 32, 24, arrival_monotonic_ns=index)
        )
    taken = slot.take(0.1)
    assert taken is not None
    assert taken.arrival_monotonic_ns == 4, "a stale frame is dropped, never shown"


def test_an_empty_slot_times_out_rather_than_blocking_forever() -> None:
    assert BoundedFrameSlot().take(0.05) is None


def test_a_closed_slot_wakes_a_waiter_and_retains_nothing() -> None:
    slot = BoundedFrameSlot()
    slot.publish(DecodedFrame(synthetic_scene(32, 24, 1), 32, 24, arrival_monotonic_ns=1))
    slot.close()
    assert slot.take(0.05) is None
    assert slot.depth == 0
    slot.publish(DecodedFrame(synthetic_scene(32, 24, 2), 32, 24, arrival_monotonic_ns=2))
    assert slot.depth == 0, "a closed slot accepts nothing"


def test_dropped_frames_are_reported_to_the_operator() -> None:
    source, _, reader = build(FakeRingFrameReader(frame_count=4))
    list(source.frames())
    assert "ring_frames_dropped_total" in source.snapshot()
    assert "ring_frames_received_total" in source.snapshot()
    assert reader.stats["frames_received_total"] == 4


# -------------------------------------------------------------------- failure and retry
@pytest.mark.parametrize(
    "category",
    ["WHEP_HTTP_UNAUTHORIZED", "WHEP_HTTP_FORBIDDEN", "PROVIDER_NOT_CONFIGURED",
     "WEBRTC_RUNTIME_UNAVAILABLE", "CODEC_UNSUPPORTED"],
)
def test_an_authorization_or_capability_failure_is_never_retried(category: str) -> None:
    """The defect this prevents: a revoked token turning into a retry loop against Ring."""
    assert is_terminal(category)
    provider = FakeRingSessionProvider(fail_with=category)
    source, _, _ = build(provider=provider)
    assert list(source.frames()) == []
    assert provider.acquired == 1, "asked exactly once"
    assert source.health is SourceHealth.FAILED
    assert source.failure_category == category
    assert source.reconnect_count == 0


@pytest.mark.parametrize("category", ["TRANSPORT_DISCONNECTED", "MEDIA_STALLED", "DECODER_FAILED"])
def test_a_transient_failure_is_retried_but_bounded(category: str) -> None:
    provider = FakeRingSessionProvider(fail_with=category)
    source, _, _ = build(provider=provider)
    assert not is_terminal(category)
    assert list(source.frames()) == []
    # One initial attempt plus the budget's attempts, and then it stops of its own accord.
    assert 1 < provider.acquired <= FAST.max_attempts + 1
    assert source.health is SourceHealth.FAILED


def test_a_session_that_recovers_resumes_streaming() -> None:
    provider = FakeRingSessionProvider(fail_with="TRANSPORT_DISCONNECTED", fail_first=2)
    source, _, _ = build(provider=provider)
    produced = list(source.frames())
    assert produced, "the third attempt succeeded"
    assert provider.acquired == 3
    assert source.reconnect_count >= 2


def test_a_reconnect_marks_the_next_frame_discontinuous() -> None:
    """Motion across a gap in the feed is not motion, and the tracker has to be told."""

    class FlakyReader(FakeRingFrameReader):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.sessions = 0

        def start(self, material: RingSessionMaterial) -> None:
            super().start(material)
            self.sessions += 1
            # First session dies after two frames; the second one is healthy.
            self._read_error_after = 2 if self.sessions == 1 else None

    source, _, _ = build(FlakyReader(frame_count=4))
    produced = list(source.frames())
    flags = [frame.discontinuity for frame in produced]
    assert flags[:2] == [False, False]
    assert True in flags[2:], f"the first frame after the reconnect must be marked: {flags}"


def test_a_reader_that_fails_to_start_is_handled_like_a_session_failure() -> None:
    source, provider, reader = build(FakeRingFrameReader(start_error="WEBRTC_RUNTIME_UNAVAILABLE"))
    assert list(source.frames()) == []
    assert source.failure_category == "WEBRTC_RUNTIME_UNAVAILABLE"
    assert provider.outstanding == 0, "a session opened for a reader that died must be released"


def test_a_stalled_feed_is_noticed_even_though_nothing_errored() -> None:
    frames_before_stall = 3
    source, provider, _ = build(
        FakeRingFrameReader(frame_count=10, stall_after=frames_before_stall)
    )
    stream = source.frames()
    maximum_expected = frames_before_stall * (FAST.max_attempts + 1)
    produced = list(islice(stream, maximum_expected + 1))
    stream.close()

    assert len(produced) >= frames_before_stall
    assert len(produced) <= maximum_expected, "a flapping feed must exhaust its reconnect budget"
    assert provider.acquired <= FAST.max_attempts + 1
    assert source.failure_category in {"MEDIA_STALLED", "FIRST_MEDIA_TIMEOUT"}


def test_a_feed_that_never_delivers_reports_the_first_frame_timeout() -> None:
    """Connected, negotiated, and silent. Not an EOS, so it must be reported as a timeout."""
    source, _, _ = build(FakeRingFrameReader(frame_count=8, stall_after=0))
    assert list(source.frames()) == []
    assert source.failure_category == "FIRST_MEDIA_TIMEOUT"


def test_a_provider_that_raises_something_unexpected_is_still_bounded() -> None:
    class RudeProvider(FakeRingSessionProvider):
        def acquire(self) -> RingSessionMaterial:
            raise ValueError("https://api.example/secret?token=abc123")

    source, _, _ = build(provider=RudeProvider())
    assert list(source.frames()) == []
    # The provider's message could carry a URL or a token; only a category survives.
    assert source.failure_category == "PROVIDER_UNAVAILABLE"


# ------------------------------------------------------------------------------ teardown
def test_a_normal_stop_releases_the_session_and_the_reader() -> None:
    source, provider, reader = build(FakeRingFrameReader(frame_count=4))
    list(source.frames())
    assert reader.closed >= 1
    assert provider.release_calls >= 1
    assert provider.outstanding == 0
    assert source.health is SourceHealth.STOPPED


def test_a_consumer_that_stops_early_still_releases_everything() -> None:
    source, provider, reader = build(FakeRingFrameReader(frame_count=50))
    stream = source.frames()
    next(stream)
    stream.close()  # the consumer walked away mid-iteration
    source.close()
    assert reader.closed >= 1
    assert provider.outstanding == 0


def test_a_consumer_exception_mid_iteration_still_releases_everything() -> None:
    source, provider, reader = build(FakeRingFrameReader(frame_count=50))
    with pytest.raises(RuntimeError, match="downstream detector exploded"):
        for _ in source.frames():
            raise RuntimeError("downstream detector exploded")
    source.close()
    assert reader.closed >= 1
    assert provider.outstanding == 0, "a detector failure must not strand a Ring session"


def test_cleanup_happens_after_a_decode_failure() -> None:
    source, provider, reader = build(FakeRingFrameReader(frame_count=10, read_error_after=2))
    list(source.frames())
    assert reader.closed >= 1
    assert provider.outstanding == 0


def test_the_source_is_a_context_manager() -> None:
    _, provider, reader = build()
    with RingWhepSource("cam", provider, reader, reconnect_policy=FAST) as source:
        assert source.kind is SourceKind.LIVE_RING_WHEP
    assert reader.closed >= 1


def test_closing_twice_is_safe() -> None:
    source, provider, _ = build()
    list(source.frames())
    source.close()
    source.close()
    assert provider.outstanding == 0


def test_repeated_start_and_stop_leaks_no_threads() -> None:
    before = threading.active_count()
    for _ in range(5):
        provider = FakeRingSessionProvider()
        reader = CountingThreadReader(frame_count=3)
        source = RingWhepSource("cam", provider, reader, reconnect_policy=FAST)
        list(source.frames())
        source.close()
        assert not reader.thread_alive
        assert provider.outstanding == 0
    assert threading.active_count() == before


# ------------------------------------------------- the rest of the pipeline is untouched
def test_ring_frames_run_through_the_existing_detector_and_tracker() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=20, width=320, height=240))
    detections = {index: [(40.0, 40.0, 140.0, 200.0, 0.9)] for index in range(20)}
    runtime = LiveDemoRuntime(source, FakePersonDetector(detections), tracking_config=CONFIG)
    runtime.run()
    metrics = runtime.metrics()
    assert metrics["video_frames_processed_total"] == 20
    assert metrics["tracks_created_total"] == 1
    assert runtime.state.source_kind == str(SourceKind.LIVE_RING_WHEP)
    assert runtime.state.is_live is True


def test_the_preview_renders_a_ring_shaped_frame() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=12, width=320, height=240))
    preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
    detections = {index: [(40.0, 40.0, 140.0, 200.0, 0.9)] for index in range(12)}
    runtime = LiveDemoRuntime(
        source, FakePersonDetector(detections), tracking_config=CONFIG, preview=preview
    )
    runtime.run()
    assert preview.previews_encoded_total > 0
    assert preview.encode_failures_total == 0


def test_the_dashboard_state_needs_no_ring_specific_field() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=8, width=320, height=240))
    detections = {index: [(40.0, 40.0, 140.0, 200.0, 0.9)] for index in range(8)}
    runtime = LiveDemoRuntime(source, FakePersonDetector(detections), tracking_config=CONFIG)
    runtime.run()
    payload = runtime.state.as_dict()
    assert payload["source"]["kind"] == "LIVE_RING_WHEP"
    assert set(payload["source"]) == {"kind", "id", "health", "health_label", "is_live"}


def test_no_detector_tracker_scheduler_or_preview_module_mentions_ring() -> None:
    """A second inference path for Ring is the failure this stage exists to avoid."""
    targets = [
        SOURCE_ROOT / "recorded" / "pipeline.py",
        SOURCE_ROOT / "recorded" / "detector.py",
        SOURCE_ROOT / "recorded" / "yolox.py",
        LIVE_ROOT / "scheduler.py",
        LIVE_ROOT / "preview.py",
        LIVE_ROOT / "runtime.py",
        LIVE_ROOT / "timeline.py",
    ]
    # Whole word only: "load-bearing" and "rendering" are not Ring.
    # Ring the provider, not "ring buffer". Case-sensitive on purpose.
    mentions_ring = re.compile(r"\bRing\b|RingWhep|ring_whep|RING_WHEP|ring_media")
    for path in targets:
        found = mentions_ring.search(path.read_text(encoding="utf-8"))
        assert found is None, f"{path.name} mentions Ring at {found.group(0)!r}"


def test_the_usb_camera_source_is_untouched_by_this_stage() -> None:
    from veotrex_edge_agent.live.camera import LocalCameraSource

    source = LocalCameraSource(0)
    assert source.kind is SourceKind.LIVE_LOCAL_CAMERA
    assert isinstance(source, LiveVideoSource)
    assert source.describe().kind is SourceKind.LIVE_LOCAL_CAMERA
    source.close()


def test_the_synthetic_source_still_refuses_to_present_itself_as_live() -> None:
    source = FakeLiveSource(frame_count=2, width=64, height=48)
    assert source.kind is SourceKind.SYNTHETIC_TEST
    assert not SourceKind(source.kind).is_live


# -------------------------------------------------------- credentials, privacy, network
def test_session_material_carries_no_token() -> None:
    fields = set(RingSessionMaterial.__dataclass_fields__)
    for forbidden in ("token", "bearer", "authorization", "secret", "credential", "sdp",
                      "password", "refresh"):
        assert not any(forbidden in name for name in fields), forbidden


def test_session_material_never_prints_its_identifiers() -> None:
    material = RingSessionMaterial("super-secret-session", "/v1/devices/abc/.../sessions/xyz")
    rendered = repr(material)
    assert "super-secret-session" not in rendered
    assert "abc" not in rendered
    assert "chars" in rendered, "length is enough to debug a truncation"
    assert "session_id" not in material.as_dict()


def test_a_decoded_frame_never_renders_its_pixels() -> None:
    frame = DecodedFrame(synthetic_scene(32, 24, 1), 32, 24, arrival_monotonic_ns=1)
    rendered = repr(frame)
    assert "32x24" in rendered
    assert "[" not in rendered, "an array repr in a traceback is picture data in a log"


def test_metrics_carry_no_identifier_from_the_session() -> None:
    metrics = RingMediaMetrics()
    metrics.last_failure_category = "WHEP_HTTP_UNAUTHORIZED"
    payload = metrics.as_dict()
    rendered = repr(payload).lower()
    for forbidden in ("token", "bearer", "sdp", "account", "nonce", "authorization", "device_id"):
        assert forbidden not in rendered, forbidden
    assert payload["ring_last_failure_category"] == "WHEP_HTTP_UNAUTHORIZED"


def test_the_source_snapshot_is_safe_to_log() -> None:
    source, _, _ = build(FakeRingFrameReader(frame_count=4))
    list(source.frames())
    rendered = repr(source.snapshot()).lower()
    for forbidden in ("token", "bearer", "sdp", "authorization", "synthetic-session"):
        assert forbidden not in rendered, forbidden


def test_no_ring_module_writes_a_frame_to_disk() -> None:
    """The same grep that guards the preview, applied to the new media path."""
    targets = [
        LIVE_ROOT / "ring.py",
        LIVE_ROOT / "ring_media.py",
        LIVE_ROOT / "ring_gst.py",
        LIVE_ROOT / "ring_fakes.py",
        SOURCE_ROOT / "camera_transport" / "frame_worker.py",
    ]
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"imwrite", "imsave", "savefig", "write_bytes"}, (
                    f"{path.name} writes image data"
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "open", f"{path.name} opens a path"


def test_no_ring_module_reaches_the_prohibited_clip() -> None:
    prohibited = "daycare" + "_demo"
    for path in sorted(LIVE_ROOT.glob("ring*.py")):
        assert prohibited not in path.read_text(encoding="utf-8"), path.name


def test_nothing_in_the_ring_source_path_contacts_a_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket to anywhere would raise. The whole fake path must run regardless."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the Ring source path must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    source, _, _ = build(FakeRingFrameReader(frame_count=5))
    assert len(list(source.frames())) == 5


def test_no_ring_endpoint_is_named_in_the_new_source_modules() -> None:
    """The control-plane host belongs to whep_client, which this stage did not change."""
    for path in sorted(LIVE_ROOT.glob("ring*.py")):
        text = path.read_text(encoding="utf-8")
        assert "amazonvision.com" not in text, path.name
        assert "https://" not in text.replace("https://api", ""), path.name


def test_the_media_path_downloads_nothing_at_runtime() -> None:
    targets = [*sorted(LIVE_ROOT.glob("ring*.py")), SOURCE_ROOT / "camera_transport" / "frame_worker.py"]
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("urlretrieve", "pip install", "apt-get", "curl ", "wget "):
            assert forbidden not in text, f"{path.name}: {forbidden}"


def test_the_decode_worker_is_never_given_the_session_material() -> None:
    """Structural, not conventional: the worker process cannot leak what it never receives."""
    source_text = (LIVE_ROOT / "ring_gst.py").read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    start = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "start"
    )
    sent = [
        node
        for node in ast.walk(start)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_send"
    ]
    assert sent, "start must send a START message"
    for call in sent:
        rendered = ast.dump(call)
        assert "material" not in rendered, "session material must not reach the worker"


def test_the_worker_start_message_is_validated() -> None:
    from veotrex_edge_agent.camera_transport.frame_worker import validate_start

    ok = validate_start(
        {"type": "START", "protocol_version": 1, "source": "synthetic",
         "decoder": "nvidia", "width": 640, "height": 480}
    )
    assert ok["source"] == "synthetic"
    for bad in (
        {"type": "NOPE", "protocol_version": 1, "source": "synthetic"},
        {"type": "START", "protocol_version": 99, "source": "synthetic"},
        {"type": "START", "protocol_version": 1, "source": "http://elsewhere"},
        {"type": "START", "protocol_version": 1, "source": "synthetic", "decoder": "magic"},
        {"type": "START", "protocol_version": 1, "source": "synthetic", "width": 99999},
    ):
        with pytest.raises(ValueError):
            validate_start(bad)


# ------------------------------------------------------------------------- the CLI shape
def test_the_usb_command_line_is_unchanged() -> None:
    from veotrex_edge_agent.live.cli import add_demo_arguments
    import argparse

    parser = add_demo_arguments(argparse.ArgumentParser())
    parsed = parser.parse_args(["--device", "/dev/video0"])
    assert parsed.source == "camera"
    assert parsed.device == "/dev/video0"
    assert parsed.ring_camera is None
    assert parsed.ring_media_qualification is False


def test_ring_without_a_camera_id_is_refused() -> None:
    import argparse

    from veotrex_edge_agent.live.cli import _source, add_demo_arguments
    from veotrex_edge_agent.live.source import LiveSourceError

    parser = add_demo_arguments(argparse.ArgumentParser())
    parsed = parser.parse_args(["--source", "ring"])
    with pytest.raises(LiveSourceError, match="ring_camera_id_required"):
        _source(parsed)


def test_ring_without_an_authorized_provider_fails_safely() -> None:
    """No silent fallback to another source, and no attempt to reach Ring."""
    import argparse

    from veotrex_edge_agent.live.cli import _source, add_demo_arguments
    from veotrex_edge_agent.live.source import LiveSourceError

    parser = add_demo_arguments(argparse.ArgumentParser())
    parsed = parser.parse_args(["--source", "ring", "--ring-camera", "front"])
    with pytest.raises(LiveSourceError, match="ring_session_provider_unavailable"):
        _source(parsed)


# --------------------------------------------------- the real decode path, when available
def _decode_runtime() -> tuple[bool, str]:
    from veotrex_edge_agent.live.ring_gst import worker_available

    return worker_available()


requires_decoder = pytest.mark.skipif(
    not _decode_runtime()[0], reason=f"decode worker runtime unavailable: {_decode_runtime()[1]}"
)


@requires_decoder
def test_the_real_decode_path_delivers_frames_out_of_the_media_subsystem() -> None:
    """The capability this stage exists to add: pixels leaving GStreamer, not just counters."""
    from veotrex_edge_agent.live.ring_gst import GstFrameReader

    provider = FakeRingSessionProvider()
    reader = GstFrameReader(source="synthetic", width=320, height=240, frames=8)
    source = RingWhepSource("qual", provider, reader, first_frame_timeout_seconds=25.0)
    produced = []
    try:
        for frame in source.frames():
            produced.append(frame)
            if len(produced) >= 5:
                break
    finally:
        source.close()
    assert len(produced) >= 5
    first = produced[0]
    assert first.image.shape == (240, 320, 3)
    assert first.image.dtype == np.uint8
    assert len({frame.image.tobytes() for frame in produced}) == len(produced), (
        "each delivered frame is a different picture"
    )
    assert first.kind is SourceKind.LIVE_RING_WHEP
    assert provider.outstanding == 0
    assert not reader.running


@requires_decoder
def test_the_real_decode_path_reports_a_monotonic_media_clock() -> None:
    from veotrex_edge_agent.live.ring_gst import GstFrameReader

    reader = GstFrameReader(source="synthetic", width=320, height=240, frames=8)
    source = RingWhepSource("qual", FakeRingSessionProvider(), reader,
                            first_frame_timeout_seconds=25.0)
    stamps = []
    try:
        for frame in source.frames():
            stamps.append(frame.capture_timestamp_ms)
            if len(stamps) >= 5:
                break
    finally:
        source.close()
    present = [value for value in stamps if value is not None]
    assert present, "the decoder supplied presentation timestamps"
    assert present == sorted(present)


@requires_decoder
def test_the_real_decode_path_tears_the_worker_down() -> None:
    from veotrex_edge_agent.live.ring_gst import GstFrameReader

    reader = GstFrameReader(source="synthetic", width=320, height=240, frames=40)
    source = RingWhepSource("qual", FakeRingSessionProvider(), reader,
                            first_frame_timeout_seconds=25.0)
    stream = source.frames()
    next(stream)
    assert reader.running
    source.close()
    assert not reader.running, "no decode worker outlives its source"
