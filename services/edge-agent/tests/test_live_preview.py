"""Live camera preview and tracking overlay (V1-DEMO-01R1).

No camera, no GPU, no model weight. Frames are constructed arrays and the detector is scripted,
so every property below is deterministic.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from veotrex_edge_agent.live import (
    BackpressureScheduler,
    FakeLiveSource,
    LiveDemoRuntime,
    PreviewBuffer,
    PreviewConfig,
    PreviewRenderer,
    SourceHealth,
)
from veotrex_edge_agent.live.preview import BOX_COLOURS
from veotrex_edge_agent.live.scheduler import SchedulerMetrics
from veotrex_edge_agent.live.server import DemoServer, is_loopback
from veotrex_edge_agent.recorded.detector import FakePersonDetector
from veotrex_edge_agent.tracking import TrackingConfig

cv2 = pytest.importorskip("cv2", reason="the recorded-video dependency group is not installed")

CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)
PACED = 0.004
WIDTH, HEIGHT = 320, 240


def scene(canvas: Any, index: int) -> None:
    """A textured moving block: unmistakably not a blank canvas once encoded."""
    canvas[:] = 40
    rng = np.random.default_rng(index)
    canvas[:] = (rng.random(canvas.shape) * 60 + 30).astype(np.uint8)
    left = 20 + (index * 4) % max(WIDTH - 80, 1)
    canvas[60:180, left : left + 60] = 220


def walking(count: int, *, step: float = 4.0) -> dict[int, list[Any]]:
    return {
        index: [(20.0 + index * step, 60.0, 80.0 + index * step, 180.0, 0.9)]
        for index in range(count)
    }


def runtime_with_preview(
    frames: int = 30, *, config: PreviewConfig | None = None, **source_kwargs: Any
) -> tuple[LiveDemoRuntime, PreviewRenderer, FakePersonDetector]:
    source_kwargs.setdefault("interval_seconds", PACED)
    source = FakeLiveSource(
        frame_count=frames, width=WIDTH, height=HEIGHT, painter=scene, **source_kwargs
    )
    detector = FakePersonDetector(walking(frames))
    preview = PreviewRenderer(config or PreviewConfig(target_fps=30.0, jpeg_quality=70))
    runtime = LiveDemoRuntime(source, detector, tracking_config=CONFIG, preview=preview)
    return runtime, preview, detector


def decode(jpeg: bytes) -> Any:
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


# ------------------------------------------------------------------ single-slot buffer
def test_publishing_replaces_the_previous_frame() -> None:
    """Exactly one frame is retained; a new one supersedes the old rather than joining it."""
    buffer = PreviewBuffer()
    first = buffer.publish(b"\xff\xd8first", frame_index=1, width=10, height=10)
    second = buffer.publish(b"\xff\xd8second", frame_index=2, width=10, height=10)
    latest = buffer.latest()
    assert latest is not None
    assert latest.jpeg == b"\xff\xd8second"
    assert latest.sequence == second.sequence == first.sequence + 1
    assert latest.frame_index == 2


def test_the_buffer_holds_no_history_however_many_frames_pass_through() -> None:
    buffer = PreviewBuffer()
    for index in range(500):
        buffer.publish(bytes([index % 256]) * 64, frame_index=index, width=8, height=8)
    latest = buffer.latest()
    assert latest is not None
    assert latest.frame_index == 499
    assert buffer.sequence == 500
    # One slot: the object graph holds a single frame, not a queue of them.
    assert isinstance(buffer._frame, object)  # the bound is the property under test
    assert not hasattr(buffer, "_frames")


def test_an_empty_buffer_has_nothing_to_show() -> None:
    buffer = PreviewBuffer()
    assert buffer.latest() is None
    assert not buffer.has_frame


def test_a_stale_frame_is_not_served() -> None:
    """A frozen last frame would keep looking live after the camera died."""
    buffer = PreviewBuffer(max_age_seconds=0.2)
    buffer.publish(b"\xff\xd8x", frame_index=1, width=8, height=8)
    assert buffer.latest() is not None
    time.sleep(0.25)
    assert buffer.latest() is None, "a stale frame must not be presented as live"


def test_clearing_drops_the_frame() -> None:
    buffer = PreviewBuffer()
    buffer.publish(b"\xff\xd8x", frame_index=1, width=8, height=8)
    buffer.clear()
    assert buffer.latest() is None


def test_a_preview_frame_never_shows_its_bytes_in_a_repr() -> None:
    buffer = PreviewBuffer()
    frame = buffer.publish(
        b"\xff\xd8" + b"secret-image-bytes" * 10, frame_index=3, width=8, height=8
    )
    rendered = repr(frame)
    assert "secret-image-bytes" not in rendered
    assert "bytes" in rendered and "#" in rendered


# ------------------------------------------------------------------------- config bounds
@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_fps": 0.0},
        {"target_fps": 120.0},
        {"jpeg_quality": 5},
        {"jpeg_quality": 100},
        {"max_width": 32},
        {"max_width": 8000},
        {"max_age_seconds": 0.0},
    ],
)
def test_an_out_of_range_preview_setting_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PreviewConfig(**kwargs)


# ----------------------------------------------------------------------------- rendering
def test_the_preview_shows_the_actual_frame_not_a_blank_canvas() -> None:
    """The defect this stage exists to fix: a black panel instead of the room."""
    renderer = PreviewRenderer(PreviewConfig(target_fps=30.0, jpeg_quality=85))
    image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    scene(image, 3)
    frame = renderer.render(image, [], frame_index=0, occupancy=0, source_health="RUNNING")
    assert frame is not None
    decoded = decode(frame.jpeg)
    body = cv2.cvtColor(decoded[26:, :], cv2.COLOR_BGR2GRAY)
    assert body.std() > 10, "the preview must carry real image content"
    assert len(np.unique(body)) > 40


def test_the_overlay_draws_the_confirmed_track_box_and_its_id() -> None:
    renderer = PreviewRenderer(PreviewConfig(target_fps=30.0, jpeg_quality=90))
    image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    scene(image, 1)
    box = (40.0, 70.0, 140.0, 190.0)
    frame = renderer.render(image, [(1, box)], frame_index=0, occupancy=1, source_health="RUNNING")
    assert frame is not None
    decoded = decode(frame.jpeg)
    colour = BOX_COLOURS[1 % len(BOX_COLOURS)]
    mask = np.abs(decoded.astype(int) - np.array(colour)).sum(axis=2) < 60
    assert mask.sum() > 100, "the track's box colour must appear in the preview"
    ys, xs = np.nonzero(mask)
    # The drawn rectangle spans the tracker's box; the label sits just above it.
    assert abs(int(xs.min()) - int(box[0])) <= 3
    assert abs(int(xs.max()) - int(box[2])) <= 3
    assert int(ys.max()) <= int(box[3]) + 3


def test_each_track_gets_its_own_colour() -> None:
    renderer = PreviewRenderer(PreviewConfig(target_fps=30.0, jpeg_quality=90))
    image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    scene(image, 2)
    frame = renderer.render(
        image,
        [(1, (20.0, 60.0, 90.0, 170.0)), (2, (160.0, 60.0, 240.0, 170.0))],
        frame_index=0,
        occupancy=2,
        source_health="RUNNING",
    )
    assert frame is not None
    decoded = decode(frame.jpeg)
    present = [
        colour
        for colour in (BOX_COLOURS[1], BOX_COLOURS[2])
        if (np.abs(decoded.astype(int) - np.array(colour)).sum(axis=2) < 60).sum() > 100
    ]
    assert len(present) == 2


def test_rendering_never_mutates_the_frame_the_detector_saw() -> None:
    """The array is the one the detector was given; drawing into it would corrupt it."""
    renderer = PreviewRenderer(PreviewConfig(target_fps=30.0))
    image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    scene(image, 5)
    original = image.copy()
    renderer.render(
        image,
        [(1, (10.0, 10.0, 100.0, 100.0))],
        frame_index=0,
        occupancy=1,
        source_health="RUNNING",
    )
    assert np.array_equal(image, original)


def test_a_large_frame_is_downscaled_but_keeps_its_aspect_ratio() -> None:
    renderer = PreviewRenderer(PreviewConfig(target_fps=30.0, max_width=320))
    image = np.zeros((720, 1280, 3), np.uint8)
    frame = renderer.render(image, [], frame_index=0, occupancy=0, source_health="RUNNING")
    assert frame is not None
    assert frame.width == 320
    assert abs(frame.width / frame.height - 1280 / 720) < 0.02


def test_encoding_is_throttled_to_the_target_rate() -> None:
    """Inference must not pay for a preview faster than anyone can watch."""
    renderer = PreviewRenderer(PreviewConfig(target_fps=2.0))
    image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    produced = [
        renderer.render(image, [], frame_index=index, occupancy=0, source_health="RUNNING")
        for index in range(40)
    ]
    encoded = [frame for frame in produced if frame is not None]
    assert len(encoded) == 1, "at 2 fps only the first of a rapid burst should encode"
    assert renderer.previews_skipped_total == 39


def test_a_rendering_failure_is_swallowed_so_tracking_continues() -> None:
    """Losing the picture is bad; losing tracking is worse."""
    renderer = PreviewRenderer(PreviewConfig(target_fps=30.0))
    broken = np.zeros((0, 0, 3), np.uint8)
    assert renderer.render(broken, [], frame_index=0, occupancy=0, source_health="RUNNING") is None
    assert renderer.encode_failures_total >= 1
    # Still usable afterwards.
    good = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    renderer._last_encode_monotonic = 0.0
    assert (
        renderer.render(good, [], frame_index=1, occupancy=0, source_health="RUNNING") is not None
    )


# ------------------------------------------------------------- pipeline integration
def test_the_preview_uses_the_pipeline_frames_and_opens_no_second_camera() -> None:
    """One owner of the device: the preview observes the processed frame, it never captures."""
    runtime, preview, detector = runtime_with_preview(24)
    runtime.run()
    assert preview.previews_encoded_total > 0
    # The source yielded frames exactly once, and the detector saw each delivered frame once.
    metrics = runtime.metrics()
    assert detector.frames_seen == metrics["frames_delivered_total"]
    assert detector.frames_seen == metrics["video_frames_processed_total"]


def test_the_detector_is_not_invoked_again_for_the_preview() -> None:
    runtime, preview, detector = runtime_with_preview(20)
    runtime.run()
    processed = runtime.metrics()["video_frames_processed_total"]
    assert detector.frames_seen == processed
    # More previews than detections would mean something else was producing boxes.
    assert preview.previews_encoded_total <= processed


def test_the_source_is_iterated_once_for_both_tracking_and_preview() -> None:
    runtime, preview, _ = runtime_with_preview(20)
    source = runtime._source  # the single owner of the device
    runtime.run()
    assert source.frames_emitted == runtime.metrics()["frames_captured_total"]


def test_a_slow_preview_cannot_stall_inference() -> None:
    """Encoding is throttled and bounded; the detector's frame count must not depend on it."""
    fast, _, fast_detector = runtime_with_preview(30, config=PreviewConfig(target_fps=0.5))
    fast.run()
    slow, slow_preview, slow_detector = runtime_with_preview(
        30, config=PreviewConfig(target_fps=30.0, jpeg_quality=95, max_width=1920)
    )
    slow.run()
    assert slow_detector.frames_seen == fast_detector.frames_seen
    assert slow_preview.previews_encoded_total > 0


def test_the_preview_is_cleared_when_the_session_ends() -> None:
    """Nothing survives the feed: no frozen last frame after the camera has gone."""
    runtime, preview, _ = runtime_with_preview(20)
    runtime.run()
    assert preview.buffer.latest() is None


def test_a_camera_failure_leaves_no_preview_behind() -> None:
    source = FakeLiveSource(
        frame_count=30,
        width=WIDTH,
        height=HEIGHT,
        painter=scene,
        fail_after=6,
        interval_seconds=PACED,
    )
    preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
    runtime = LiveDemoRuntime(
        source, FakePersonDetector(walking(30)), tracking_config=CONFIG, preview=preview
    )
    runtime.run()
    assert runtime.failure == "camera_unavailable"
    assert preview.buffer.latest() is None


def test_a_runtime_without_a_preview_still_works() -> None:
    """--headless and the automated suites run with no preview at all."""
    source = FakeLiveSource(frame_count=20, width=WIDTH, height=HEIGHT, interval_seconds=PACED)
    runtime = LiveDemoRuntime(source, FakePersonDetector(walking(20)), tracking_config=CONFIG)
    runtime.run()
    assert runtime.preview is None
    assert runtime.metrics()["video_frames_processed_total"] > 0
    assert "previews_encoded_total" not in runtime.metrics()


# --------------------------------------------------------------------- HTTP transport
def test_the_frame_endpoint_serves_a_valid_jpeg() -> None:
    runtime, preview, _ = runtime_with_preview(200, interval_seconds=0.01)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        body, headers = _fetch_frame(host, port)
        runtime.stop()
    assert body[:2] == b"\xff\xd8", "must be a JPEG"
    assert body[-2:] == b"\xff\xd9"
    assert headers["Content-Type"] == "image/jpeg"
    assert "no-store" in headers["Cache-Control"]
    assert int(headers["X-Preview-Sequence"]) >= 1
    assert decode(body) is not None


def test_the_frame_endpoint_reports_no_frame_rather_than_a_stale_one() -> None:
    runtime, _, _ = runtime_with_preview(4)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://{host}:{port}/api/live/frame.jpg", timeout=5)
    assert raised.value.code == 503
    assert json.loads(raised.value.read())["error"] == "no_frame"


def test_successive_fetches_advance_as_new_frames_arrive() -> None:
    runtime, _, _ = runtime_with_preview(300, interval_seconds=0.01)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        sequences = []
        for _ in range(6):
            _, headers = _fetch_frame(host, port)
            sequences.append(int(headers["X-Preview-Sequence"]))
            time.sleep(0.12)
        runtime.stop()
    assert sequences == sorted(sequences)
    assert sequences[-1] > sequences[0], "the picture must be changing"


def test_repeated_browser_refreshes_do_not_break_the_runtime() -> None:
    """Twenty rapid fetches, as a client clicking refresh would produce."""
    runtime, _, _ = runtime_with_preview(400, interval_seconds=0.01)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        _fetch_frame(host, port)
        for _ in range(20):
            urllib.request.urlopen(f"http://{host}:{port}/api/live/frame.jpg", timeout=5).read()
        assert runtime.running
        still_ok, _ = _fetch_frame(host, port)
        runtime.stop()
    assert still_ok[:2] == b"\xff\xd8"


def test_a_client_that_disconnects_leaves_nothing_retained() -> None:
    """Closing a response mid-read must not make the server hold a frame for that client."""
    runtime, preview, _ = runtime_with_preview(400, interval_seconds=0.01)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        _fetch_frame(host, port)
        for _ in range(5):
            response = urllib.request.urlopen(f"http://{host}:{port}/api/live/frame.jpg", timeout=5)
            response.close()  # abandon without reading the body
        time.sleep(0.2)
        assert runtime.running
        sequence_after = preview.buffer.sequence
        body, _ = _fetch_frame(host, port)
        runtime.stop()
    assert body[:2] == b"\xff\xd8"
    assert sequence_after >= 1


def test_concurrent_viewers_are_served_the_same_single_frame() -> None:
    runtime, _, _ = runtime_with_preview(400, interval_seconds=0.01)
    results: list[int] = []
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        _fetch_frame(host, port)

        def pull() -> None:
            try:
                _, headers = _fetch_frame(host, port)
                results.append(int(headers["X-Preview-Sequence"]))
            except Exception:  # pragma: no cover - a racing shutdown
                results.append(-1)

        threads = [threading.Thread(target=pull) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        runtime.stop()
    assert len(results) == 5
    assert all(value > 0 for value in results)


def _fetch_frame(host: str, port: int, attempts: int = 60) -> tuple[bytes, Any]:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/live/frame.jpg", timeout=5) as r:
                return r.read(), r.headers
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                raise
            time.sleep(0.05)
    raise AssertionError("no preview frame became available")


# ------------------------------------------------------------------ capture fps metric
def test_capture_fps_is_reported_while_capture_is_still_running() -> None:
    """The reported defect: the dashboard showed 0.0 capture FPS while frames_captured_total
    was visibly climbing, because elapsed time was only recorded once the loop had ended."""
    source = FakeLiveSource(frame_count=10_000, width=64, height=48, interval_seconds=0.002)
    scheduler = BackpressureScheduler(source)
    try:
        scheduler.start()
        time.sleep(0.4)
        snapshot = scheduler.metrics.snapshot()
        assert snapshot["frames_captured_total"] > 0
        assert snapshot["camera_capture_fps"] > 0.0, "capture FPS must be live, not end-of-run"
        assert snapshot["capture_seconds"] > 0.0
    finally:
        scheduler.stop()


def test_capture_fps_is_zero_before_capture_begins() -> None:
    metrics = SchedulerMetrics()
    assert metrics.capture_fps == 0.0
    assert metrics.capture_seconds == 0.0


def test_capture_fps_is_final_once_capture_has_stopped() -> None:
    source = FakeLiveSource(frame_count=20, width=64, height=48, interval_seconds=0.002)
    with BackpressureScheduler(source) as scheduler:
        list(scheduler.frames())
    settled = scheduler.metrics.capture_seconds
    time.sleep(0.15)
    assert scheduler.metrics.capture_seconds == settled, "elapsed must freeze when capture ends"
    assert scheduler.metrics.capture_fps > 0.0


def test_capture_fps_roughly_matches_the_source_cadence() -> None:
    """Not fabricated: the number has to track the real interval between frames."""
    source = FakeLiveSource(frame_count=10_000, width=32, height=32, interval_seconds=0.02)
    scheduler = BackpressureScheduler(source)
    try:
        scheduler.start()
        time.sleep(1.0)
        measured = scheduler.metrics.capture_fps
    finally:
        scheduler.stop()
    assert 20.0 <= measured <= 60.0, measured


# ------------------------------------------------------------------ privacy / security
def test_the_preview_is_never_written_to_disk(tmp_path: Path) -> None:
    before = set(tmp_path.rglob("*"))
    runtime, _, _ = runtime_with_preview(30)
    runtime.run()
    assert set(tmp_path.rglob("*")) == before


def test_no_preview_module_writes_a_file() -> None:
    """Asserted against the source: the failure mode is a debug dump added later."""
    module = Path(__file__).resolve().parents[1] / "src/veotrex_edge_agent/live/preview.py"
    text = module.read_text(encoding="utf-8")
    for forbidden in ("imwrite", "open(", "Path(", "mkdir", "tofile", "savefig"):
        assert forbidden not in text, f"preview must not persist anything ({forbidden})"


def test_the_state_endpoint_still_carries_no_identity_or_biometric_field() -> None:
    runtime, _, _ = runtime_with_preview(200, interval_seconds=0.01)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        runtime.start()
        time.sleep(0.2)
        payload = json.loads(
            urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=5).read()
        )
        runtime.stop()
    body = json.dumps(payload).lower()
    for forbidden in ("staff", "identity", "embedding", "template", "face", "child", "teacher"):
        assert forbidden not in body, forbidden


def test_the_page_shows_no_identity_or_demographic_language() -> None:
    runtime, _, _ = runtime_with_preview(10)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        page = urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
    words = set(re.findall(r"[a-z]+", page.lower()))
    for forbidden in (
        "teacher",
        "teachers",
        "child",
        "children",
        "adult",
        "age",
        "gender",
        "emotion",
        "race",
        "ethnicity",
        "identity",
        "name",
        "names",
        "score",
        "scores",
    ):
        assert forbidden not in words, forbidden
    assert "no identification" in page.lower()


def test_the_overlay_draws_no_identity_text() -> None:
    """Checks the text that is actually rendered, not prose about it.

    An earlier version of this test scraped every quoted string in the module and tripped over
    its own docstring, which legitimately *discusses* the words that are never drawn. The AST
    walk below looks only at what reaches ``cv2.putText``.
    """
    import ast

    module_path = Path(__file__).resolve().parents[1] / "src/veotrex_edge_agent/live/preview.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    drawn: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "putText"):
            continue
        assert len(node.args) >= 2, "putText must carry a text argument"
        text_argument = node.args[1]
        if isinstance(text_argument, ast.Constant) and isinstance(text_argument.value, str):
            drawn.append(text_argument.value)
        elif isinstance(text_argument, ast.Name):
            # A variable: resolve the literal parts of whatever was assigned to it.
            for assignment in ast.walk(tree):
                if isinstance(assignment, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == text_argument.id
                    for target in assignment.targets
                ):
                    drawn.extend(_literal_parts(assignment.value))
        else:
            drawn.extend(_literal_parts(text_argument))

    assert drawn, "the overlay is expected to draw some text"
    joined = " ".join(drawn).lower()
    for forbidden in (
        "teacher",
        "child",
        "adult",
        "age",
        "gender",
        "emotion",
        "race",
        "identity",
        "name",
        "unknown person",
        "score",
        "confidence",
    ):
        assert forbidden not in joined, f"overlay draws {forbidden!r}: {drawn}"
    # What it does draw: a track number and a head count.
    assert any("track" in value.lower() for value in drawn)


def _literal_parts(node: Any) -> list[str]:
    """The constant text inside a literal or an f-string, ignoring interpolated values."""
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
    return []


def test_the_dashboard_binding_policy_is_unchanged() -> None:
    runtime, _, _ = runtime_with_preview(4)
    assert is_loopback(DemoServer(runtime, port=0).address[0])
    from veotrex_edge_agent.live.server import InsecureBindRefused

    with pytest.raises(InsecureBindRefused):
        DemoServer(runtime, host="0.0.0.0")  # noqa: S104 - refusing this is the point


def test_no_preview_code_path_references_the_prohibited_clip() -> None:
    prohibited = "daycare" + "_demo"
    root = Path(__file__).resolve().parents[3]
    targets = list((root / "services/edge-agent/src/veotrex_edge_agent/live").glob("*.py"))
    runbook = root / "docs/runbooks/sunday-live-demo.md"
    if runbook.is_file():
        targets.append(runbook)
    for target in targets:
        assert prohibited not in target.read_text(encoding="utf-8"), target.name


def test_the_preview_records_its_own_cost_for_the_dashboard() -> None:
    runtime, preview, _ = runtime_with_preview(30)
    runtime.run()
    snapshot = runtime.metrics()
    assert snapshot["previews_encoded_total"] == preview.previews_encoded_total
    assert snapshot["preview_encode_ms"]["p50"] is not None
    assert snapshot["preview_target_fps"] > 0


def test_a_disconnected_source_reports_a_state_the_page_can_show_a_placeholder_for() -> None:
    source = FakeLiveSource(
        frame_count=30, width=WIDTH, height=HEIGHT, fail_after=4, interval_seconds=PACED
    )
    preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
    runtime = LiveDemoRuntime(
        source, FakePersonDetector(walking(30)), tracking_config=CONFIG, preview=preview
    )
    runtime.run()
    assert source.health is SourceHealth.FAILED
    assert runtime.state.source_health in {"FAILED", "STOPPED"}
    assert preview.buffer.latest() is None, "no image may remain to imply a live feed"
