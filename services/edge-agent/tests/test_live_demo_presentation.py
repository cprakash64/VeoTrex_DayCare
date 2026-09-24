"""Demo presentation and false-positive control (V1-DEMO-01R2).

Three things a human reviewer found at the Sunday rehearsal, and the invariants that keep them
fixed:

  the word RUNNING   read, beside a person's bounding box, as a claim about the person rather
                     than about the capture thread. Nothing here detects behaviour of any kind.
  a duplicated room  two preview buffers were being laid out side by side, because an author
                     `display:block` defeats the user agent's `[hidden]{display:none}`.
  a phantom person   the real detector finds a person in a poster on the wall, permanently.

No camera, no GPU and no model weight is needed for any test below.
"""

from __future__ import annotations

import ast
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from veotrex_edge_agent.live import (
    FakeLiveSource,
    LiveDemoRuntime,
    LocalCameraSource,
    PreviewConfig,
    PreviewRenderer,
    SourceHealth,
    crop_to_view,
    health_label,
    validate_source_view,
)
from veotrex_edge_agent.live.camera import SOURCE_VIEW_FULL
from veotrex_edge_agent.live.server import DemoServer, is_loopback
from veotrex_edge_agent.live.source import LiveSourceError
from veotrex_edge_agent.recorded.detector import FakePersonDetector
from veotrex_edge_agent.recorded.pipeline import RecordedTrackingPipeline
from veotrex_edge_agent.recorded.regions import (
    MAX_IGNORE_REGIONS,
    IgnoreRegion,
    IgnoreRegionError,
    IgnoreRegionSet,
    build_ignore_regions,
    parse_ignore_region,
)
from veotrex_edge_agent.tracking import TrackingConfig

cv2 = pytest.importorskip("cv2", reason="the recorded-video dependency group is not installed")

CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)
WIDTH, HEIGHT = 320, 240
PACED = 0.004
LIVE_ROOT = Path(__file__).resolve().parents[1] / "src/veotrex_edge_agent/live"
RECORDED_ROOT = Path(__file__).resolve().parents[1] / "src/veotrex_edge_agent/recorded"


def scene(canvas: Any, index: int) -> None:
    rng = np.random.default_rng(index)
    canvas[:] = (rng.random(canvas.shape) * 60 + 30).astype(np.uint8)


def boxes_for(frames: int, box: tuple[float, float, float, float]) -> dict[int, list[Any]]:
    return {index: [(*box, 0.9)] for index in range(frames)}


def run_with(
    detections: dict[int, list[Any]], *, regions: IgnoreRegionSet | None = None, frames: int = 24
) -> LiveDemoRuntime:
    source = FakeLiveSource(
        frame_count=frames, width=WIDTH, height=HEIGHT, painter=scene, interval_seconds=PACED
    )
    runtime = LiveDemoRuntime(
        source,
        FakePersonDetector(detections),
        tracking_config=CONFIG,
        ignore_regions=regions,
    )
    runtime.run()
    return runtime


# ------------------------------------------------------------------ the status wording
def test_the_dashboard_never_shows_a_bare_word_that_could_describe_a_person() -> None:
    """ "RUNNING" beside a bounding box is a sentence about the person, not the camera."""
    assert health_label(SourceHealth.RUNNING) == "SYSTEM ACTIVE"
    shown = {health_label(value) for value in SourceHealth}
    for label in shown:
        assert label.upper() == label, "the wording is a status, shown as one"
        for ambiguous in ("RUNNING", "WALKING", "STANDING", "SITTING", "FALLING", "MOVING"):
            assert ambiguous not in label, f"{label!r} could be read as a behaviour"


def test_every_health_value_has_wording_about_the_system_or_the_camera() -> None:
    for value in SourceHealth:
        label = health_label(value)
        assert label
        assert any(word in label for word in ("SYSTEM", "CAMERA", "SESSION", "STARTING"))


def test_an_unknown_health_value_still_reads_as_a_system_state() -> None:
    assert health_label("SOMETHING_NEW") == "SYSTEM SOMETHING_NEW"


def test_the_wording_reaches_the_page_from_the_server_not_from_the_browser() -> None:
    """One definition. A copy in JavaScript would drift from the one drawn onto the frame."""
    runtime = run_with({}, frames=4)
    payload = json.loads(json.dumps(runtime.state.as_dict()))
    assert "health_label" in payload["source"]
    assert payload["source"]["health"] in {value.value for value in SourceHealth}
    page = (LIVE_ROOT / "server.py").read_text(encoding="utf-8")
    assert "health_label" in page
    assert '"RUNNING"' not in page.replace('h === "RUNNING"', ""), (
        "the page may compare the machine state, never print it"
    )


def test_the_overlay_draws_the_same_wording_as_the_page() -> None:
    preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
    canvas = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    scene(canvas, 1)
    frame = preview.render(
        canvas, [], frame_index=0, occupancy=0, source_health=str(SourceHealth.RUNNING)
    )
    assert frame is not None
    source = (LIVE_ROOT / "preview.py").read_text(encoding="utf-8")
    assert "health_label(source_health)" in source


# ------------------------------------------------------------- the duplicated preview
def test_only_one_preview_buffer_can_occupy_the_stage() -> None:
    """The duplicated-room defect, in one assertion.

    `.feed { display:block }` is an author rule and beats the user agent's
    `[hidden]{display:none}`, so the hidden attribute stopped hiding anything. The stage is a
    flex row, so both buffers were laid out side by side at half width and the room appeared
    twice. Hiding has to be stated by the page that broke it.
    """
    page = (LIVE_ROOT / "server.py").read_text(encoding="utf-8")
    assert ".feed[hidden]" in page, "the page must hide a hidden buffer explicitly"
    rule = page[page.index(".feed[hidden]") : page.index(".feed[hidden]") + 60]
    assert "display:none" in rule.replace(" ", "")


def test_the_page_still_declares_display_block_for_the_visible_buffer() -> None:
    """Guards the pair: removing one of these two rules reintroduces the defect."""
    page = (LIVE_ROOT / "server.py").read_text(encoding="utf-8")
    assert re.search(r"\.feed \{[^}]*display:block", page), "an inline image leaves a baseline gap"


# ------------------------------------------------------------------ the source view
def test_an_ordinary_camera_is_not_cropped() -> None:
    image = np.arange(4 * 10 * 3, dtype=np.uint8).reshape(4, 10, 3)
    assert crop_to_view(image, SOURCE_VIEW_FULL) is image, "full must not even copy"


def test_the_left_view_is_the_left_half() -> None:
    image = np.zeros((6, 100, 3), np.uint8)
    image[:, :50] = 11
    image[:, 50:] = 22
    left = crop_to_view(image, "left")
    assert left.shape == (6, 50, 3)
    assert set(np.unique(left)) == {11}


def test_the_right_view_is_the_right_half() -> None:
    image = np.zeros((6, 100, 3), np.uint8)
    image[:, :50] = 11
    image[:, 50:] = 22
    right = crop_to_view(image, "right")
    assert right.shape == (6, 50, 3)
    assert set(np.unique(right)) == {22}


def test_an_odd_width_never_yields_mismatched_halves() -> None:
    image = np.zeros((4, 101, 3), np.uint8)
    assert crop_to_view(image, "left").shape == crop_to_view(image, "right").shape


@pytest.mark.parametrize("view", ["middle", "LEFTish", "", "both", "1"])
def test_an_unknown_view_is_refused(view: str) -> None:
    with pytest.raises(LiveSourceError, match="invalid_source_view"):
        validate_source_view(view)
    with pytest.raises(LiveSourceError, match="invalid_source_view"):
        LocalCameraSource(0, view=view)


@pytest.mark.parametrize("view", ["full", "left", "right", "LEFT", " Right "])
def test_a_known_view_is_accepted_however_it_is_typed(view: str) -> None:
    assert validate_source_view(view) in {"full", "left", "right"}


class _FakeCapture:
    """A camera that hands back a side-by-side frame, so the crop can be tested end to end.

    It opens exactly once. A second construction reports a closed device, which is what ends
    the source's bounded reconnect loop instead of letting it re-open this fake forever.
    """

    opened = 0

    def __init__(self, *_: Any, **__: Any) -> None:
        self.released = False
        self._index = 0
        self._live = _FakeCapture.opened == 0
        _FakeCapture.opened += 1

    def isOpened(self) -> bool:  # OpenCV's naming contract
        return self._live

    def set(self, *_: Any) -> bool:
        return True

    def get(self, prop: int) -> float:
        return {cv2.CAP_PROP_FRAME_WIDTH: 640.0, cv2.CAP_PROP_FRAME_HEIGHT: 240.0}.get(prop, 0.0)

    def read(self) -> tuple[bool, Any]:
        self._index += 1
        if self._index > 6:
            return (False, None)
        image = np.zeros((240, 640, 3), np.uint8)
        image[:, :320] = 11  # "left lens"
        image[:, 320:] = 22  # "right lens"
        return (True, image)

    def release(self) -> None:
        self.released = True


def test_the_crop_happens_in_the_source_so_everything_downstream_shares_one_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeCapture.opened = 0
    monkeypatch.setattr(cv2, "VideoCapture", _FakeCapture)
    source = LocalCameraSource(0, width=640, height=240, view="right")
    frames = list(source.frames())
    source.close()
    assert frames, "the fake camera must have produced frames"
    for frame in frames:
        assert frame.width == 320, "the frame that leaves the source is the picture"
        assert frame.height == 240
        assert frame.image.shape == (240, 320, 3)
        assert set(np.unique(frame.image)) == {22}, "the right lens, not the left"
    assert source.describe().width == 320, "the dashboard must be told the picture's geometry"


def test_the_detector_receives_the_cropped_view_not_the_sensor_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cropping before inference is what keeps boxes geometrically correct."""
    _FakeCapture.opened = 0
    monkeypatch.setattr(cv2, "VideoCapture", _FakeCapture)
    seen: list[tuple[int, int]] = []

    class Recording(FakePersonDetector):
        def detect(self, image: Any, **kwargs: Any) -> Any:
            seen.append((int(image.shape[1]), int(image.shape[0])))
            return super().detect(image, **kwargs)

    source = LocalCameraSource(0, width=640, height=240, view="left")
    runtime = LiveDemoRuntime(source, Recording({}), tracking_config=CONFIG)
    runtime.run()
    assert seen, "the detector must have been called"
    assert set(seen) == {(320, 240)}, "inference ran on the selected view only"


def test_boxes_are_expressed_in_the_displayed_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box the detector returns must land inside the picture the operator is shown."""
    _FakeCapture.opened = 0
    monkeypatch.setattr(cv2, "VideoCapture", _FakeCapture)
    source = LocalCameraSource(0, width=640, height=240, view="right")
    detector = FakePersonDetector(boxes_for(8, (10.0, 20.0, 300.0, 230.0)))
    runtime = LiveDemoRuntime(source, detector, tracking_config=CONFIG)
    runtime.run()
    state = runtime.state
    assert state.width == 320
    for track in state.tracks:
        x1, y1, x2, y2 = track.bbox_xyxy
        assert 0 <= x1 < x2 <= state.width
        assert 0 <= y1 < y2 <= state.height


# --------------------------------------------------------------------- ignore regions
def test_no_regions_is_the_default_and_changes_nothing() -> None:
    empty = IgnoreRegionSet()
    assert not empty
    assert len(empty) == 0
    assert empty.matching([1, 1, 50, 50], width=WIDTH, height=HEIGHT) is None
    assert RecordedTrackingPipeline(FakePersonDetector({})).ignore_regions == IgnoreRegionSet()


def test_a_detection_wholly_inside_a_region_is_ignored() -> None:
    regions = build_ignore_regions(["0.1,0.1,0.4,0.5,poster"])
    matched = regions.matching([40, 30, 120, 110], width=400, height=400)
    assert matched is not None
    assert matched.label == "poster"


def test_a_detection_outside_every_region_is_preserved() -> None:
    regions = build_ignore_regions(["0.1,0.1,0.4,0.5"])
    assert regions.matching([200, 200, 300, 380], width=400, height=400) is None


def test_a_person_standing_in_front_of_the_masked_object_is_kept() -> None:
    """The documented policy, stated as the case it exists to protect.

    The region is a poster on the wall. The detection is an adult in front of it: taller and
    wider than the poster, so most of their area is outside the region and they survive - even
    though the centre of their box is over the poster, which is what a centre-in-region rule
    would have keyed on.
    """
    regions = build_ignore_regions(["0.4,0.1,0.6,0.35"])
    poster = [400, 100, 600, 350]
    person = [380, 60, 640, 900]
    assert regions.matching(poster, width=1000, height=1000) is not None
    assert regions.matching(person, width=1000, height=1000) is None
    centre_x = (person[0] + person[2]) / 2 / 1000
    centre_y = (person[1] + person[3]) / 2 / 1000
    region = regions.regions[0]
    assert region.x1 <= centre_x <= region.x2, "the centre really is over the region"
    assert centre_y > region.y2, "and the documented rule does not use the centre anyway"


@pytest.mark.parametrize(
    ("containment", "ignored"),
    [(0.99, False), (0.8, False), (0.5, True), (0.2, True)],
)
def test_partial_overlap_follows_the_configured_containment(
    containment: float, ignored: bool
) -> None:
    """Exactly 60% of this box lies inside the region.

    The region is kept under the half-frame cap on purpose - a fixture that the module's own
    validation would refuse is not a fixture.
    """
    regions = IgnoreRegionSet((IgnoreRegion(0.0, 0.0, 0.5, 0.6),), min_containment=containment)
    box = [0.0, 0.0, 50.0, 100.0]
    fraction = regions.regions[0].containment_of(box, width=100, height=100)
    assert fraction == pytest.approx(0.6)
    assert (regions.matching(box, width=100, height=100) is not None) is ignored


def test_the_number_of_regions_is_bounded() -> None:
    fine = [f"0.0,{i / 100:.2f},0.05,{i / 100 + 0.01:.2f}" for i in range(MAX_IGNORE_REGIONS)]
    assert len(build_ignore_regions(fine)) == MAX_IGNORE_REGIONS
    with pytest.raises(IgnoreRegionError, match="at most"):
        build_ignore_regions([*fine, "0.5,0.5,0.55,0.55"])


def test_the_regions_together_may_not_blind_the_camera() -> None:
    with pytest.raises(IgnoreRegionError, match="in total"):
        build_ignore_regions(["0,0,1,0.45", "0,0.5,1,0.95"])


@pytest.mark.parametrize(
    "text",
    [
        "0.1,0.1,0.2",
        "0.1,0.1,0.2,0.3,0.4,0.5",
        "a,0.1,0.2,0.3",
        "-0.1,0.1,0.2,0.3",
        "0.1,0.1,1.5,0.3",
        "0.4,0.1,0.2,0.3",
        "0.1,0.4,0.2,0.3",
        "0.1,0.1,0.1,0.3",
        "0,0,1,1",
        "nan,0.1,0.2,0.3",
        "",
    ],
)
def test_a_malformed_region_is_refused_with_the_rule_it_broke(text: str) -> None:
    with pytest.raises(IgnoreRegionError) as raised:
        parse_ignore_region(text)
    assert str(raised.value), "the operator is told what was wrong"


def test_an_out_of_range_containment_is_refused() -> None:
    for value in (0.0, -0.5, 1.5):
        with pytest.raises(IgnoreRegionError, match="min_containment"):
            IgnoreRegionSet((IgnoreRegion(0.1, 0.1, 0.2, 0.2),), min_containment=value)


def test_an_ignored_detection_never_reaches_the_tracker() -> None:
    """Not a filter on the output: the phantom must never have had a track at all."""
    poster = (0.2 * WIDTH, 0.2 * HEIGHT, 0.4 * WIDTH, 0.5 * HEIGHT)
    regions = build_ignore_regions(["0.15,0.15,0.45,0.55,poster"])
    suppressed = run_with(boxes_for(24, poster), regions=regions)
    assert suppressed.state.occupancy == 0
    assert suppressed.metrics()["tracks_created_total"] == 0
    assert not suppressed.timeline.recent(50) or all(
        event["kind"] != "PERSON_APPEARED_IN_VIEW" for event in suppressed.timeline.recent(50)
    )


def test_the_same_detection_without_the_region_does_produce_a_track() -> None:
    """The control: the suppression above has to be the region's doing, not the scene's."""
    poster = (0.2 * WIDTH, 0.2 * HEIGHT, 0.4 * WIDTH, 0.5 * HEIGHT)
    allowed = run_with(boxes_for(24, poster))
    assert allowed.metrics()["tracks_created_total"] == 1
    assert allowed.metrics()["detections_ignored_total"] == 0


def test_the_ignored_count_is_reported_for_the_operator() -> None:
    poster = (0.2 * WIDTH, 0.2 * HEIGHT, 0.4 * WIDTH, 0.5 * HEIGHT)
    regions = build_ignore_regions(["0.15,0.15,0.45,0.55"])
    runtime = run_with(boxes_for(24, poster), regions=regions)
    metrics = runtime.metrics()
    assert metrics["detections_ignored_total"] >= 20
    assert metrics["person_detections_total"] == 0, "an ignored detection is not a person"


def test_a_real_person_elsewhere_in_the_frame_is_still_tracked() -> None:
    """A configured region must not cost the camera the rest of its view."""
    regions = build_ignore_regions(["0.05,0.05,0.25,0.3,poster"])
    person = (0.55 * WIDTH, 0.3 * HEIGHT, 0.8 * WIDTH, 0.95 * HEIGHT)
    runtime = run_with(boxes_for(24, person), regions=regions)
    assert runtime.metrics()["tracks_created_total"] == 1
    assert runtime.metrics()["detections_ignored_total"] == 0
    # Peak, not final: run() drains the source, and a stream that ends closes its live tracks,
    # so the occupancy at the last frame is 0 for every run whether or not anyone was seen.
    assert runtime.metrics()["peak_occupancy"] == 1


def test_one_cameras_regions_do_not_reach_another() -> None:
    poster = (0.2 * WIDTH, 0.2 * HEIGHT, 0.4 * WIDTH, 0.5 * HEIGHT)
    configured = run_with(
        boxes_for(24, poster), regions=build_ignore_regions(["0.15,0.15,0.45,0.55"])
    )
    plain = run_with(boxes_for(24, poster))
    assert configured.metrics()["peak_occupancy"] == 0, "the configured camera saw nobody"
    assert plain.metrics()["peak_occupancy"] == 1, "the unconfigured one saw the same artifact"
    assert configured.metrics()["detections_ignored_total"] > 0
    assert plain.metrics()["detections_ignored_total"] == 0


def test_the_dashboard_reports_the_ignored_count() -> None:
    runtime = run_with({}, frames=4)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        page = urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
    assert "Ignored detections" in page
    assert "detections_ignored_total" in page


# ----------------------------------------------------------- nothing room-specific in code
def test_no_room_specific_coordinates_are_written_into_the_source() -> None:
    """Regions are configuration. A literal rectangle in the code would be this room, forever."""
    suspicious = re.compile(r"\b0\.\d{2,}\s*,\s*0\.\d{2,}\s*,\s*0\.\d{2,}\s*,\s*0\.\d{2,}")
    for path in [*sorted(LIVE_ROOT.glob("*.py")), RECORDED_ROOT / "regions.py"]:
        text = path.read_text(encoding="utf-8")
        assert not suspicious.search(text), f"{path.name} carries a hardcoded rectangle"


def test_the_default_configuration_masks_nothing() -> None:
    tree = ast.parse((RECORDED_ROOT / "regions.py").read_text(encoding="utf-8"))
    defaults = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "regions"
    ]
    assert defaults, "IgnoreRegionSet must declare its regions field"
    assert all(isinstance(node.value, ast.Tuple) and not node.value.elts for node in defaults), (
        "the default must be an empty tuple"
    )


# ------------------------------------------------------ prior invariants, still standing
def test_the_preview_still_renders_with_regions_configured() -> None:
    source = FakeLiveSource(
        frame_count=24, width=WIDTH, height=HEIGHT, painter=scene, interval_seconds=PACED
    )
    preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
    person = (0.55 * WIDTH, 0.3 * HEIGHT, 0.8 * WIDTH, 0.95 * HEIGHT)
    runtime = LiveDemoRuntime(
        source,
        FakePersonDetector(boxes_for(24, person)),
        tracking_config=CONFIG,
        preview=preview,
        ignore_regions=build_ignore_regions(["0.05,0.05,0.25,0.3"]),
    )
    runtime.run()
    assert preview.previews_encoded_total > 0
    assert preview.encode_failures_total == 0


def test_regions_add_no_second_detector_or_tracker() -> None:
    calls = {"detect": 0}

    class Counting(FakePersonDetector):
        def detect(self, image: Any, **kwargs: Any) -> Any:
            calls["detect"] += 1
            return super().detect(image, **kwargs)

    source = FakeLiveSource(
        frame_count=12, width=WIDTH, height=HEIGHT, painter=scene, interval_seconds=PACED
    )
    runtime = LiveDemoRuntime(
        source,
        Counting(boxes_for(12, (10.0, 10.0, 60.0, 120.0))),
        tracking_config=CONFIG,
        ignore_regions=build_ignore_regions(["0.0,0.0,0.3,0.6"]),
    )
    runtime.run()
    assert calls["detect"] == runtime.metrics()["video_frames_processed_total"]


def test_the_region_mechanism_has_no_face_or_biometric_behaviour() -> None:
    text = (RECORDED_ROOT / "regions.py").read_text(encoding="utf-8").lower()
    for forbidden in ("embedding", "template", "recognis", "recogniz", "biometric", "landmark"):
        assert forbidden not in text, forbidden


def test_the_loopback_binding_is_unchanged_by_this_stage() -> None:
    runtime = run_with({}, frames=4)
    assert is_loopback(DemoServer(runtime, port=0).address[0])


def test_the_page_still_shows_no_identity_or_demographic_language() -> None:
    runtime = run_with({}, frames=4)
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        page = urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
    words = set(re.findall(r"[a-z]+", page.lower()))
    for forbidden in (
        "teacher",
        "child",
        "children",
        "adult",
        "age",
        "gender",
        "emotion",
        "identity",
        "name",
        "score",
    ):
        assert forbidden not in words, forbidden
