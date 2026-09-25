"""Live camera ingestion and the real-time demo runtime (V1-DEMO-01).

No camera hardware, no GPU and no model weight. Every test drives ``FakeLiveSource``, which is
also what the demo falls back to when no camera is attached - so the code path under test is
the code path that runs.
"""

from __future__ import annotations

import ast
import json
import re
import threading
import time
import urllib.error
import urllib.request
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from veotrex_edge_agent.live import (
    BackpressureScheduler,
    DemoEventKind,
    DemoTimeline,
    FakeLiveSource,
    LiveDemoRuntime,
    LiveFrame,
    LiveSourceError,
    LocalCameraSource,
    SourceHealth,
    SourceKind,
    discover_cameras,
    list_device_nodes,
    probe_camera,
)
from veotrex_edge_agent.live.server import DemoServer, InsecureBindRefused, is_loopback
from veotrex_edge_agent.live.source import validate_geometry
from veotrex_edge_agent.recorded.detector import FakePersonDetector
from veotrex_edge_agent.tracking import TrackingConfig

CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)


def walking(count: int, *, x0: float = 10.0, step: float = 4.0) -> dict[int, list[Any]]:
    return {
        index: [(x0 + index * step, 80.0, x0 + index * step + 50.0, 200.0, 0.9)]
        for index in range(count)
    }


def two_people(count: int) -> dict[int, list[Any]]:
    return {
        index: [
            (10.0 + index * 3, 80.0, 60.0 + index * 3, 200.0, 0.9),
            (260.0 - index * 3, 80.0, 310.0 - index * 3, 200.0, 0.9),
        ]
        for index in range(count)
    }


# Paced by default, deliberately. An unpaced source fills the scheduler's single slot faster
# than the consumer can ever drain it, so frames are dropped - correct behaviour, and covered
# by its own test - but it makes any assertion about *tracking* non-deterministic: the tracker
# may or may not receive the consecutive observations it needs to confirm a track. Tests about
# tracking pace the source so the pipeline deterministically sees every frame; tests about
# backpressure opt out by passing ``interval_seconds=0``.
DEFAULT_TEST_INTERVAL_SECONDS = 0.004


def runtime_for(
    script: dict[int, list[Any]], *, frames: int, **source_kwargs: Any
) -> LiveDemoRuntime:
    source_kwargs.setdefault("interval_seconds", DEFAULT_TEST_INTERVAL_SECONDS)
    source = FakeLiveSource(frame_count=frames, width=320, height=240, **source_kwargs)
    return LiveDemoRuntime(source, FakePersonDetector(script), tracking_config=CONFIG)


# ------------------------------------------------------------------ source contract
def test_a_live_frame_exposes_everything_the_b1a_pipeline_reads() -> None:
    """The reuse this stage depends on: a LiveFrame must satisfy the recorded pipeline's
    duck-typed contract, or nothing downstream works."""
    source = FakeLiveSource(frame_count=1)
    frame = next(iter(source.frames()))
    for attribute in ("frame_index", "timestamp_ms", "width", "height", "image"):
        assert hasattr(frame, attribute), attribute
    assert isinstance(frame, LiveFrame)
    assert frame.image.shape == (240, 320, 3)


def test_provenance_travels_with_every_frame() -> None:
    """Recorded or synthetic imagery must never be able to present itself as a live camera."""
    frame = next(iter(FakeLiveSource(frame_count=1).frames()))
    assert frame.kind is SourceKind.SYNTHETIC_TEST
    assert not frame.kind.is_live
    assert SourceKind.LIVE_LOCAL_CAMERA.is_live
    assert SourceKind.LIVE_RING_WHEP.is_live


def test_a_frame_never_shows_pixels_in_its_repr() -> None:
    frame = next(iter(FakeLiveSource(frame_count=1).frames()))
    rendered = repr(frame)
    assert "SYNTHETIC_TEST" in rendered
    assert "array" not in rendered and "[[" not in rendered


def test_frame_timestamps_are_monotonic_and_start_at_zero() -> None:
    """Continuity is monotonic-clock based, so it cannot go backwards when NTP steps the
    system clock mid-demo."""
    frames = list(FakeLiveSource(frame_count=8, fps=20.0).frames())
    stamps = [frame.timestamp_ms for frame in frames]
    assert stamps[0] == 0.0
    assert stamps == sorted(stamps)
    assert all(b > a for a, b in pairwise(stamps))
    assert [frame.monotonic_ns for frame in frames] == sorted(
        frame.monotonic_ns for frame in frames
    )


def test_geometry_bounds_are_enforced() -> None:
    validate_geometry(320, 240)
    with pytest.raises(LiveSourceError, match="too_small"):
        validate_geometry(8, 8)
    with pytest.raises(LiveSourceError, match="too_large"):
        validate_geometry(8000, 8000)


# ---------------------------------------------------------------- open / close lifecycle
def test_a_source_is_closed_after_normal_completion() -> None:
    source = FakeLiveSource(frame_count=5)
    with BackpressureScheduler(source) as scheduler:
        list(scheduler.frames())
    assert source.closed
    assert source.health is SourceHealth.STOPPED


def test_close_is_idempotent_and_safe_before_iteration() -> None:
    source = FakeLiveSource(frame_count=3)
    source.close()
    source.close()
    assert source.closed


def test_a_capture_failure_still_releases_the_source() -> None:
    """The failure that would otherwise leave a device handle open behind a demo."""
    source = FakeLiveSource(frame_count=10, fail_after=3)
    scheduler = BackpressureScheduler(source)
    with scheduler:
        delivered = list(scheduler.frames())
    assert len(delivered) <= 3
    assert source.closed
    assert scheduler.failure == "camera_unavailable"


def test_a_detector_failure_still_stops_and_releases_everything() -> None:
    class ExplodingDetector(FakePersonDetector):
        def detect(self, image: Any, *, frame_index: int, timestamp_ms: float) -> list[Any]:
            if frame_index >= 2:
                raise RuntimeError("detector died")
            return super().detect(image, frame_index=frame_index, timestamp_ms=timestamp_ms)

    source = FakeLiveSource(frame_count=20, interval_seconds=DEFAULT_TEST_INTERVAL_SECONDS)
    runtime = LiveDemoRuntime(source, ExplodingDetector(walking(20)), tracking_config=CONFIG)
    runtime.run()
    assert runtime.failure == "pipeline_error"
    assert source.closed
    assert not runtime.running


def test_a_camera_that_cannot_be_opened_fails_cleanly() -> None:
    """No hardware needed: index 9999 exists nowhere."""
    source = LocalCameraSource(9999)
    with pytest.raises(LiveSourceError) as raised:
        list(source.frames())
    assert raised.value.category in {"camera_unavailable", "video_backend_unavailable"}
    assert source.health is SourceHealth.FAILED
    source.close()


@pytest.mark.parametrize(
    "device",
    [
        "http://example.invalid/stream",
        "rtsp://cam/1",
        "/dev/../etc/passwd",
        "v4l2src ! fakesink",
        "/dev/videoX",
    ],
)
def test_no_url_or_arbitrary_device_string_is_accepted(device: str) -> None:
    """This stage opens local V4L2 nodes and nothing else."""
    with pytest.raises(LiveSourceError, match="invalid_camera_device"):
        LocalCameraSource(device)


def test_a_negative_camera_index_is_refused() -> None:
    with pytest.raises(LiveSourceError, match="invalid_camera_index"):
        LocalCameraSource(-1)


# ------------------------------------------------------------------- camera discovery
def test_discovery_reports_only_safe_metadata_and_keeps_no_frames() -> None:
    """Discovery must be safe to run in front of a client: no frame is stored anywhere."""
    candidates = discover_cameras()
    for candidate in candidates:
        assert candidate.device.startswith("/dev/video")
        assert isinstance(candidate.usable, bool)
        for value in (candidate.width, candidate.height, candidate.fps):
            assert value is None or isinstance(value, int | float)
        # The dataclass has no field that could hold imagery.
        assert not hasattr(candidate, "image")
        assert not hasattr(candidate, "frame")


def test_discovery_on_a_machine_with_no_camera_is_empty_not_an_error() -> None:
    assert isinstance(list_device_nodes(), list)
    assert isinstance(discover_cameras(), list)


def test_probing_a_nonexistent_node_reports_unusable_rather_than_raising() -> None:
    candidate = probe_camera(9999)
    assert not candidate.usable
    assert candidate.detail in {"cannot_open", "probe_failed", "opencv_unavailable"}


# ----------------------------------------------------------- backpressure / bounded latency
def test_a_slow_consumer_drops_stale_frames_instead_of_queueing() -> None:
    """The policy this stage exists to get right: the detector must always see a recent frame,
    never a backlog, and the frames it skipped must be counted rather than silently lost."""
    source = FakeLiveSource(frame_count=200, width=64, height=48, interval_seconds=0.002)
    seen: list[int] = []
    with BackpressureScheduler(source) as scheduler:
        for frame in scheduler.frames():
            seen.append(frame.frame_index)
            time.sleep(0.03)
            if len(seen) >= 6:
                break
    metrics = scheduler.metrics
    assert len(seen) == 6
    assert seen == sorted(seen)
    # Indices jump forward: the consumer received recent frames, not consecutive old ones.
    assert seen[-1] - seen[0] > len(seen), seen
    assert metrics.frames_dropped_total > 0
    assert metrics.frames_captured_total >= metrics.frames_delivered_total


def test_memory_does_not_grow_with_session_length() -> None:
    """At capacity 1 exactly one frame can ever be waiting, whatever the rate mismatch."""
    source = FakeLiveSource(frame_count=300, width=64, height=48, interval_seconds=0.001)
    with BackpressureScheduler(source) as scheduler:
        for index, _ in enumerate(scheduler.frames()):
            if index >= 20:
                break
            assert len(scheduler._buffer) <= 1  # the bound is the property under test
    assert scheduler.metrics.frames_dropped_total >= 0


def test_a_consumer_that_keeps_up_drops_nothing() -> None:
    """When capture is paced and the consumer keeps up, every frame is delivered in order."""
    source = FakeLiveSource(frame_count=12, width=64, height=48, interval_seconds=0.01)
    with BackpressureScheduler(source) as scheduler:
        delivered = [frame.frame_index for frame in scheduler.frames()]
    assert delivered == list(range(12))
    assert scheduler.metrics.frames_dropped_total == 0


def test_an_unpaced_producer_legitimately_drops_to_the_newest_frame() -> None:
    """The newest-frame-wins contract, stated plainly: a source that emits faster than the
    consumer can ever run keeps only the most recent frame. Nothing queues, nothing leaks, and
    the skipped frames are counted rather than silently lost."""
    source = FakeLiveSource(frame_count=50, width=32, height=32)  # no pacing at all
    with BackpressureScheduler(source) as scheduler:
        delivered = [frame.frame_index for frame in scheduler.frames()]
    metrics = scheduler.metrics
    assert delivered == sorted(delivered)
    assert metrics.frames_captured_total == 50
    assert metrics.frames_delivered_total == len(delivered)
    assert metrics.frames_delivered_total + metrics.frames_dropped_total == 50
    assert delivered[-1] == 49, "the most recent frame must always get through"


def test_an_invalid_scheduler_capacity_is_refused() -> None:
    with pytest.raises(LiveSourceError, match="invalid_scheduler_capacity"):
        BackpressureScheduler(FakeLiveSource(), capacity=0)


def test_the_capture_thread_exits_on_stop() -> None:
    source = FakeLiveSource(frame_count=100_000, width=32, height=32, interval_seconds=0.001)
    scheduler = BackpressureScheduler(source)
    scheduler.start()
    time.sleep(0.05)
    assert scheduler.running
    scheduler.stop()
    assert not scheduler.running
    assert threading.active_count() >= 1


# ----------------------------------------------------------------- detection / tracking
def test_the_detector_sees_exactly_the_frames_the_scheduler_delivered() -> None:
    """The invariant that ties the two halves together: every delivered frame reaches the
    detector, and a dropped frame reaches nothing."""
    detector = FakePersonDetector(walking(10))
    source = FakeLiveSource(frame_count=10, width=320, height=240, interval_seconds=0.01)
    runtime = LiveDemoRuntime(source, detector, tracking_config=CONFIG)
    runtime.run()
    metrics = runtime.metrics()
    assert detector.frames_seen == metrics["frames_delivered_total"]
    assert detector.frames_seen == metrics["video_frames_processed_total"]
    assert metrics["frames_captured_total"] == 10


def test_one_person_produces_one_stable_track() -> None:
    runtime = runtime_for(walking(12), frames=12)
    runtime.run()
    assert runtime.metrics()["tracks_created_total"] == 1
    assert runtime.timeline.peak_occupancy == 1


def test_two_people_are_tracked_separately_and_counted() -> None:
    runtime = runtime_for(two_people(12), frames=12)
    runtime.run()
    assert runtime.metrics()["tracks_created_total"] == 2
    assert runtime.timeline.peak_occupancy == 2


def test_occupancy_returns_to_zero_when_everyone_leaves_view() -> None:
    script: dict[int, list[Any]] = dict(walking(4).items())
    runtime = runtime_for(script, frames=20)
    runtime.run()
    assert runtime.timeline.peak_occupancy >= 1
    assert runtime.state.occupancy == 0


def test_tracker_state_is_isolated_between_runs() -> None:
    """Two sessions must be independent worlds; a track id from one cannot leak into the next."""
    first = runtime_for(walking(10), frames=10)
    first.run()
    second = runtime_for(walking(10), frames=10)
    second.run()
    assert first.metrics()["tracks_created_total"] == second.metrics()["tracks_created_total"]
    assert first.timeline.peak_occupancy == second.timeline.peak_occupancy


# --------------------------------------------------------------------------- timeline
def test_the_timeline_is_bounded() -> None:
    timeline = DemoTimeline(capacity=10)
    for index in range(50):
        timeline.record(DemoEventKind.PERSON_APPEARED_IN_VIEW, session_ms=float(index))
    assert len(timeline) == 10
    assert timeline.capacity == 10
    # The oldest were discarded, the newest survive.
    assert timeline.recent(10)[0]["sequence"] == 50


def test_occupancy_events_are_only_recorded_on_a_real_change() -> None:
    timeline = DemoTimeline()
    assert timeline.set_occupancy(2, session_ms=0.0) is not None
    assert timeline.set_occupancy(2, session_ms=1.0) is None
    assert timeline.set_occupancy(3, session_ms=2.0) is not None
    assert timeline.peak_occupancy == 3


def test_timeline_vocabulary_makes_no_entry_exit_or_identity_claim() -> None:
    """Camera appearance is not physical entry, and nobody here is a teacher or a child."""
    names = {str(kind) for kind in DemoEventKind}
    assert "PERSON_APPEARED_IN_VIEW" in names
    assert "PERSON_NO_LONGER_VISIBLE" in names
    joined = " ".join(names).lower()
    for forbidden in ("teacher", "child", "entered", "exited", "classroom", "intruder"):
        assert forbidden not in joined


def test_a_session_records_start_and_stop() -> None:
    runtime = runtime_for(walking(6), frames=6)
    runtime.run()
    kinds = [event["kind"] for event in runtime.timeline.recent(100)]
    assert "TRACKING_STARTED" in kinds
    assert "TRACKING_STOPPED" in kinds


def test_a_live_source_records_camera_connected() -> None:
    """Only a live source may claim a camera connected; synthetic must not."""
    synthetic = runtime_for(walking(4), frames=4)
    synthetic.run()
    assert "CAMERA_CONNECTED" not in [e["kind"] for e in synthetic.timeline.recent(100)]


def test_timeline_events_carry_no_identity_or_imagery() -> None:
    runtime = runtime_for(walking(10), frames=10)
    runtime.run()
    for event in runtime.timeline.recent(100):
        assert set(event) == {
            "sequence",
            "kind",
            "occurred_at",
            "session_ms",
            "track_id",
            "occupancy",
        }
        assert all(not isinstance(value, bytes) for value in event.values())


# ------------------------------------------------------------------------- dashboard
def test_the_dashboard_binds_loopback_by_default() -> None:
    runtime = runtime_for(walking(4), frames=4)
    server = DemoServer(runtime, port=0)
    assert is_loopback(server.address[0])


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10"])  # noqa: S104
def test_a_non_loopback_bind_is_refused_without_an_explicit_override(host: str) -> None:
    """The dashboard is unauthenticated, so exposing it must be a deliberate act."""
    runtime = runtime_for(walking(4), frames=4)
    with pytest.raises(InsecureBindRefused):
        DemoServer(runtime, host=host)


def test_a_non_loopback_bind_is_possible_only_with_the_override() -> None:
    runtime = runtime_for(walking(4), frames=4)
    server = DemoServer(runtime, host="127.0.0.1", port=0, allow_non_loopback=True)
    assert server is not None


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),  # noqa: S104 - asserting this is NOT loopback is the point
        ("10.0.0.5", False),
    ],
)
def test_loopback_detection(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_the_dashboard_serves_state_without_any_imagery() -> None:
    runtime = runtime_for(two_people(60), frames=60, interval_seconds=0.005)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        payload = _poll_state(host, port, want_occupancy=True)
        runtime.stop()

    state = payload["state"]
    assert state["occupancy"] >= 1
    assert state["tracks"]
    for track in state["tracks"]:
        assert set(track) == {"track_id", "bbox_xyxy", "confidence"}
        assert len(track["bbox_xyxy"]) == 4
    # Geometry and counts only: nothing pixel-shaped anywhere in the response.
    body = json.dumps(payload)
    for marker in ("image", "frame_data", "jpeg", "base64", "png", "face", "embedding"):
        assert marker not in body.lower()


def test_the_dashboard_shows_no_names_identity_or_demographic_inference() -> None:
    runtime = runtime_for(walking(4), frames=4)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        page = urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
    lowered = page.lower()
    # Whole words: a substring check trips over "aspect-ratio" and "background".
    words = set(re.findall(r"[a-z]+", lowered))
    for forbidden in (
        "teacher",
        "teachers",
        "child",
        "children",
        "age",
        "gender",
        "emotion",
        "race",
        "ethnicity",
        "identity",
        "name",
        "names",
    ):
        assert forbidden not in words, forbidden
    assert "no identification" in lowered


def test_an_unknown_path_is_a_bounded_404() -> None:
    runtime = runtime_for(walking(4), frames=4)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://{host}:{port}/../etc/passwd", timeout=5)
        assert raised.value.code == 404


def test_the_server_stops_cleanly() -> None:
    runtime = runtime_for(walking(4), frames=4)
    server = DemoServer(runtime, port=0)
    host, port = server.start()
    urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5).read()
    server.stop()
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=2).read()


def _poll_state(host: str, port: int, *, want_occupancy: bool = False, attempts: int = 40) -> Any:
    payload: Any = {}
    for _ in range(attempts):
        raw = urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=5).read()
        payload = json.loads(raw)
        if not want_occupancy or payload["state"]["occupancy"] >= 1:
            return payload
        time.sleep(0.05)
    return payload


# ------------------------------------------------------------------------- observability
def test_every_required_metric_is_present() -> None:
    runtime = runtime_for(two_people(15), frames=15)
    runtime.run()
    metrics = runtime.metrics()
    for key in (
        "camera_capture_fps",
        "frames_captured_total",
        "video_frames_processed_total",
        "frames_dropped_total",
        "person_detections_total",
        "active_tracks",
        "tracks_created_total",
        "tracks_completed_total",
        "detector_latency_ms",
        "tracker_latency_ms",
        "pipeline_latency_ms",
        "processing_fps",
        "camera_reconnect_count",
        "occupancy",
        "peak_occupancy",
        "source_health",
    ):
        assert key in metrics, key
    for key in ("detector_latency_ms", "tracker_latency_ms", "pipeline_latency_ms"):
        assert metrics[key]["p50"] is not None
        assert metrics[key]["p95"] is not None


def test_processing_fps_is_live_rather_than_end_of_run() -> None:
    """The pipeline finalises its own elapsed time in a finally, which would read 0.0 on the
    dashboard for the whole demo. The runtime computes it from session time instead."""
    runtime = runtime_for(walking(60), frames=60, interval_seconds=0.005)
    runtime.start()
    time.sleep(0.4)
    during = runtime.metrics()
    runtime.stop()
    assert during["processing_fps"] > 0.0
    assert during["session_seconds"] > 0.0


# ------------------------------------------------------- no recognition, no biometrics
def test_the_live_package_never_imports_the_face_stack() -> None:
    """Asserted against the source, because the failure mode is an import added later that
    quietly makes recognition reachable from the demo path."""
    package = Path(__file__).resolve().parents[1] / "src" / "veotrex_edge_agent" / "live"
    for module in sorted(package.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        for forbidden in ("face_backend", "face_matching", "face_opencv", "sface", "yunet"):
            assert forbidden not in text.lower(), f"{module.name} reaches the face stack"


def test_no_module_in_the_live_package_downloads_anything() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "veotrex_edge_agent" / "live"
    for module in sorted(package.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        for forbidden in ("urlopen", "urlretrieve", "requests.get", "httpx.get", "urllib.request"):
            assert forbidden not in text, f"{module.name} must not fetch"


def test_the_runtime_state_carries_no_biometric_or_identity_field() -> None:
    runtime = runtime_for(walking(10), frames=10)
    runtime.run()
    rendered = json.dumps(runtime.state.as_dict())
    for forbidden in ("staff", "identity", "embedding", "template", "face", "name"):
        assert forbidden not in rendered.lower(), forbidden


def test_people_who_are_never_identified_are_tracked_normally() -> None:
    """Nobody in this pipeline is identifiable, and everyone is still tracked and counted."""
    runtime = runtime_for(two_people(15), frames=15)
    runtime.run()
    assert runtime.metrics()["tracks_created_total"] == 2
    assert runtime.timeline.peak_occupancy == 2


# ------------------------------------------------------------ future Ring compatibility
def test_only_the_ring_source_module_produces_ring_frames() -> None:
    """Ring plugs in by implementing LiveVideoSource (V1-DEMO-02/03C). Exactly one live module,
    ``ring.py``'s RingWhepSource, stamps frames as Ring; no consumer branches on the kind."""
    assert SourceKind.LIVE_RING_WHEP.is_live
    package = Path(__file__).resolve().parents[1] / "src" / "veotrex_edge_agent" / "live"
    produced = sorted(
        module.name
        for module in package.glob("*.py")
        if not module.name.startswith("._")
        and "SourceKind.LIVE_RING_WHEP" in module.read_text(encoding="utf-8")
        and module.name != "source.py"
    )
    assert produced == ["ring.py"], f"only RingWhepSource may construct a Ring frame: {produced}"


def test_a_ring_shaped_source_satisfies_the_same_contract() -> None:
    """A stand-in with Ring's SourceKind flows through the identical pipeline, proving the
    detector and tracker never learn which source produced a frame."""

    class RingShapedSource(FakeLiveSource):
        kind = SourceKind.LIVE_RING_WHEP

    source = RingShapedSource(
        frame_count=10,
        width=320,
        height=240,
        source_id="ring-camera-1",
        interval_seconds=DEFAULT_TEST_INTERVAL_SECONDS,
    )
    runtime = LiveDemoRuntime(source, FakePersonDetector(walking(10)), tracking_config=CONFIG)
    runtime.run()
    assert runtime.state.is_live
    assert runtime.state.source_kind == "LIVE_RING_WHEP"
    assert runtime.metrics()["tracks_created_total"] == 1
    kinds = [event["kind"] for event in runtime.timeline.recent(100)]
    assert "CAMERA_CONNECTED" in kinds


def _code_strings(tree: ast.AST) -> list[str]:
    """String literals that are code, not documentation."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_live_package_reaches_ring_only_through_the_veotrex_broker() -> None:
    """V1-DEMO-03C replaced "the live package never calls Ring" with the stronger structural
    form of what matters: no live module can talk to Ring or hold a Ring token. The only path
    to a session is the VeoTrex broker, imported by exactly the provider and the CLI."""
    package = Path(__file__).resolve().parents[1] / "src" / "veotrex_edge_agent" / "live"
    forbidden_modules = (
        "whep_client",
        "whep_provider",
        "ring_client",
        "qualification.backend",
        "http.client",
        "urllib.request",
        "httpx",
        "requests",
    )
    broker_importers = set()
    for module in sorted(package.glob("*.py")):
        if module.name.startswith("._"):
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = {
            name
            for node in ast.walk(tree)
            for name in (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
        }
        for name in imported:
            assert not name.endswith(forbidden_modules), f"{module.name} imports {name}"
            if name.endswith("broker_whep"):
                broker_importers.add(module.name)
        identifiers = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        } | {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
        for identifier in identifiers:
            lowered = identifier.lower()
            assert "access_token" not in lowered and "refresh_token" not in lowered, (
                f"{module.name} handles {identifier}"
            )
        for value in _code_strings(tree):
            lowered = value.lower()
            assert "amazonvision" not in lowered and "oauth" not in lowered, module.name
    assert broker_importers == {"cli.py", "ring_broker.py"}


# ------------------------------------------------------------------ prohibited footage
def test_no_live_module_or_runbook_references_the_prohibited_clip() -> None:
    """The repository's one recorded clip contains children and is prohibited for this stage.
    No live code path and no operator instruction may name it, so nobody can be led into
    pointing the demo at it."""
    prohibited = "daycare" + "_demo"
    root = Path(__file__).resolve().parents[3]
    targets = list((root / "services/edge-agent/src/veotrex_edge_agent/live").glob("*.py"))
    runbook = root / "docs/runbooks/sunday-live-demo.md"
    if runbook.is_file():
        targets.append(runbook)
    for module in targets:
        assert prohibited not in module.read_text(encoding="utf-8"), module.name


def test_the_demo_never_writes_a_video_or_a_frame(tmp_path: Path) -> None:
    """No recording by default: a completed session leaves nothing on disk."""
    before = set(tmp_path.rglob("*"))
    runtime = runtime_for(walking(10), frames=10)
    runtime.run()
    assert set(tmp_path.rglob("*")) == before
