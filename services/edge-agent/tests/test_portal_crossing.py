"""Portal crossing and anonymous room entry / exit (V1-05A).

Synthetic geometry and synthetic tracks only: no camera, no GPU, no model, no recorded footage.
Appearing in view is never an entry, leaving the view is never an exit, and nothing here names,
classifies or identifies anyone.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from veotrex_edge_agent.live import FakeLiveSource, LiveDemoRuntime
from veotrex_edge_agent.live import cli as live_cli
from veotrex_edge_agent.live.portal_crossing import (
    CrossingOutcome,
    CrossingPolicy,
    Direction,
    PortalMonitor,
    PortalTrackState,
    RoomTransition,
    RoomTransitionKind,
    Side,
    advance,
    classify,
)
from veotrex_edge_agent.live.portal_geometry import (
    MAX_PORTALS,
    InsideSide,
    Portal,
    PortalError,
    PortalSet,
    build_portals,
    parse_portal,
    track_reference_point,
)
from veotrex_edge_agent.live.server import PAGE, DemoServer
from veotrex_edge_agent.live.source import LiveFrame
from veotrex_edge_agent.recorded.detector import FakePersonDetector
from veotrex_edge_agent.tracking import TrackingConfig

LIVE_ROOT = Path(__file__).resolve().parents[1] / "src/veotrex_edge_agent/live"
WIDTH, HEIGHT = 320, 240
CONFIG = TrackingConfig(confirmation_observations=2, max_lost_seconds=0.5)
PACED = 0.008

# A vertical doorway down the middle of the picture; the room is to its right.
DOOR = Portal("door", 0.5, 0.05, 0.5, 0.95, InsideSide.RIGHT, "main door")
POLICY = CrossingPolicy()


def point_at(x: float, y: float = 0.6) -> tuple[float, float]:
    return (x, y)


def feed(
    portal: Portal,
    xs: list[float],
    *,
    state: PortalTrackState | None = None,
    start_ms: float = 0.0,
    step_ms: float = 180.0,
    eligible: bool = True,
    policy: CrossingPolicy = POLICY,
    y: float = 0.6,
) -> tuple[PortalTrackState, list[CrossingOutcome]]:
    state = state or PortalTrackState()
    outcomes = [
        advance(
            portal, state, point_at(x, y), start_ms + i * step_ms, policy, eligible=eligible
        ).outcome
        for i, x in enumerate(xs)
    ]
    return state, outcomes


def events(outcomes: list[CrossingOutcome]) -> list[CrossingOutcome]:
    return [o for o in outcomes if o in (CrossingOutcome.ENTERED, CrossingOutcome.EXITED)]


# ================================================================================ geometry
def test_a_valid_portal_and_its_inside_normal() -> None:
    assert DOOR.inside_normal == (1.0, 0.0)
    assert DOOR.inside_offset((0.7, 0.5)) == pytest.approx(0.2)
    assert DOOR.inside_offset((0.3, 0.5)) == pytest.approx(-0.2)
    left = Portal("left", 0.5, 0.05, 0.5, 0.95, InsideSide.LEFT)
    assert left.inside_normal == (-1.0, 0.0)
    # The same line drawn in the other direction means the same room.
    reversed_door = Portal("rev", 0.5, 0.95, 0.5, 0.05, InsideSide.RIGHT)
    assert reversed_door.inside_normal == pytest.approx((1.0, 0.0))
    across = Portal("across", 0.1, 0.8, 0.9, 0.8, InsideSide.ABOVE)
    assert across.inside_normal == pytest.approx((0.0, -1.0))
    assert classify(across, (0.5, 0.5)) is Side.INSIDE
    diagonal = Portal("diag", 0.2, 0.2, 0.8, 0.8, InsideSide.RIGHT)
    assert diagonal.inside_offset((0.9, 0.1)) > 0


@pytest.mark.parametrize(
    "coordinates",
    [(0.5, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.505), (0.3, 0.3, 0.3001, 0.3)],
)
def test_zero_length_and_degenerate_lines_are_rejected(
    coordinates: tuple[float, float, float, float],
) -> None:
    with pytest.raises(PortalError, match="at least"):
        Portal("p", *coordinates, InsideSide.RIGHT)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nan_and_infinity_are_rejected(bad: float) -> None:
    with pytest.raises(PortalError, match="finite"):
        Portal("p", bad, 0.1, 0.5, 0.9, InsideSide.RIGHT)
    with pytest.raises(PortalError, match="finite"):
        Portal("p", 0.5, 0.1, 0.5, bad, InsideSide.RIGHT)


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0, -1.0])
def test_out_of_frame_coordinates_are_rejected(bad: float) -> None:
    with pytest.raises(PortalError, match="normalized"):
        Portal("p", bad, 0.1, 0.5, 0.9, InsideSide.RIGHT)


def test_ambiguous_inside_sides_are_rejected() -> None:
    with pytest.raises(PortalError, match="ambiguous"):
        Portal("p", 0.5, 0.1, 0.5, 0.9, InsideSide.ABOVE)  # vertical line, ABOVE
    with pytest.raises(PortalError, match="ambiguous"):
        Portal("p", 0.1, 0.5, 0.9, 0.5, InsideSide.LEFT)  # horizontal line, LEFT
    with pytest.raises(PortalError, match="ambiguous"):
        Portal("p", 0.1, 0.5, 0.9, 0.6, InsideSide.RIGHT)  # nearly horizontal
    with pytest.raises(PortalError, match="inside must be"):
        Portal("p", 0.5, 0.1, 0.5, 0.9, "RIGHT")  # type: ignore[arg-type]


def test_inside_side_parsing() -> None:
    parsed = parse_portal("door-a:0.5,0.05,0.5,0.95,right,Main door")
    assert (parsed.portal_id, parsed.inside, parsed.label) == (
        "door-a",
        InsideSide.RIGHT,
        "Main door",
    )
    assert parse_portal("0.5,0.05,0.5,0.95,LEFT", index=3).portal_id == "portal-3"
    assert parse_portal("0.1,0.8,0.9,0.8,Below").inside is InsideSide.BELOW
    for text in (
        "0.5,0.05,0.5,0.95,INWARD",
        "0.5,0.05,0.5,0.95",
        "0.5,0.05,0.5,0.95,RIGHT,label,extra",
        "a,0.05,0.5,0.95,RIGHT",
        "Door:0.5,0.05,0.5,0.95,RIGHT",  # ids are lowercase
        "0.5,0.05,0.5,0.95,RIGHT,<script>",
    ):
        with pytest.raises(PortalError):
            parse_portal(text)


def test_reference_point_is_bottom_centre_normalised_and_clamped() -> None:
    assert track_reference_point((100, 50, 140, 200), width=320, height=240) == pytest.approx(
        (120 / 320, 200 / 240)
    )
    # Boxes that spill past the frame are clamped, never extrapolated.
    assert track_reference_point((-40, 10, 20, 400), width=320, height=240) == (0.0, 1.0)
    assert track_reference_point((300, 10, 400, 100), width=320, height=240) == (1.0, 100 / 240)
    for box in ((10, 10, 10, 50), (10, 50, 40, 20), (math.nan, 1, 2, 3), (1, 2, 3)):
        assert track_reference_point(box, width=320, height=240) is None
    assert track_reference_point((1, 2, 3, 4), width=0, height=240) is None


def test_portal_set_bounds_and_unique_ids() -> None:
    four = build_portals([f"p{i}:0.{i + 1},0.1,0.{i + 1},0.9,RIGHT" for i in range(MAX_PORTALS)])
    assert len(four) == MAX_PORTALS
    with pytest.raises(PortalError, match="at most"):
        build_portals([f"p{i}:0.{i + 1},0.1,0.{i + 1},0.9,RIGHT" for i in range(MAX_PORTALS + 1)])
    with pytest.raises(PortalError, match="unique"):
        build_portals(["d:0.2,0.1,0.2,0.9,RIGHT", "d:0.4,0.1,0.4,0.9,RIGHT"])
    assert not build_portals(None) and not build_portals([])


# ================================================================================ crossing
def test_outside_to_inside_emits_one_entry() -> None:
    _, outcomes = feed(DOOR, [0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.85, 0.9])
    assert outcomes[0] is CrossingOutcome.INITIALISED
    assert events(outcomes) == [CrossingOutcome.ENTERED]
    assert outcomes.index(CrossingOutcome.ENTERED) == 5  # the third observation inside


def test_inside_to_outside_emits_one_exit() -> None:
    _, outcomes = feed(DOOR, [0.9, 0.8, 0.7, 0.4, 0.3, 0.2, 0.1])
    assert events(outcomes) == [CrossingOutcome.EXITED]


def test_startup_inside_or_outside_emits_nothing() -> None:
    for xs in ([0.8, 0.82, 0.8, 0.79, 0.81], [0.2, 0.21, 0.19, 0.2]):
        state, outcomes = feed(DOOR, xs)
        assert outcomes[0] is CrossingOutcome.INITIALISED
        assert events(outcomes) == []
        assert state.side in (Side.INSIDE, Side.OUTSIDE)


def test_jitter_in_the_deadband_emits_nothing() -> None:
    _, outcomes = feed(DOOR, [0.3] + [0.49, 0.51, 0.495, 0.505, 0.5] * 6)
    assert events(outcomes) == []


def test_noisy_near_line_observations_do_not_duplicate_events() -> None:
    # Standing in the doorway, wobbling a little past the dead-band on either side.
    xs = [0.3, 0.3] + [0.53, 0.47, 0.53, 0.53, 0.47, 0.53, 0.47, 0.47] * 4
    _, outcomes = feed(DOOR, xs)
    assert events(outcomes) == []
    # Then really walks in: exactly one entry.
    state, outcomes = feed(DOOR, [*xs, 0.6, 0.7, 0.8, 0.8, 0.8])
    assert events(outcomes) == [CrossingOutcome.ENTERED]
    assert state.side is Side.INSIDE


def test_a_stable_crossing_emits_exactly_once_however_long_they_stay() -> None:
    _, outcomes = feed(DOOR, [0.2, 0.3] + [0.7] * 50)
    assert events(outcomes) == [CrossingOutcome.ENTERED]


def test_a_later_real_return_emits_the_opposite_event() -> None:
    state, first = feed(DOOR, [0.2, 0.3, 0.7, 0.7, 0.7, 0.7])
    _, second = feed(DOOR, [0.7, 0.3, 0.2, 0.2, 0.2], state=state, start_ms=5000.0)
    assert events(first) == [CrossingOutcome.ENTERED]
    assert events(second) == [CrossingOutcome.EXITED]


def test_cooldown_delays_but_never_hides_a_real_return() -> None:
    policy = CrossingPolicy(cooldown_seconds=2.0)
    state, first = feed(DOOR, [0.2, 0.7, 0.7, 0.7], policy=policy, step_ms=100.0)
    assert events(first) == [CrossingOutcome.ENTERED]
    # Straight back out: the confirmed run waits for the cooldown, then fires once.
    _, second = feed(DOOR, [0.2] * 30, state=state, start_ms=400.0, step_ms=100.0, policy=policy)
    assert CrossingOutcome.COOLDOWN_PENDING in second
    assert events(second) == [CrossingOutcome.EXITED]
    # A step in and straight back within the cooldown nets out to nothing extra.
    state, first = feed(DOOR, [0.2, 0.7, 0.7, 0.7], policy=policy, step_ms=100.0)
    _, bounce = feed(
        DOOR, [0.2, 0.2, 0.2, 0.7, 0.7], state=state, start_ms=400.0, step_ms=100.0, policy=policy
    )
    assert events(bounce) == []


def test_walking_past_the_end_of_the_door_line_is_not_an_entry() -> None:
    short = Portal("short", 0.5, 0.4, 0.5, 0.6, InsideSide.RIGHT)
    state, outcomes = feed(short, [0.2, 0.3, 0.7, 0.8, 0.8], y=0.95)
    assert events(outcomes) == [] and CrossingOutcome.OUTSIDE_SEGMENT in outcomes
    assert state.side is Side.INSIDE  # rebased, so it cannot fire late either


def test_an_unvalidated_track_moves_state_but_emits_nothing() -> None:
    state, outcomes = feed(DOOR, [0.2, 0.7, 0.7, 0.7], eligible=False)
    assert CrossingOutcome.SUPPRESSED_NOT_VALIDATED in outcomes and events(outcomes) == []
    _, later = feed(DOOR, [0.7] * 5, state=state, start_ms=2000.0)
    assert events(later) == []


def test_out_of_order_observations_are_ignored() -> None:
    state, _ = feed(DOOR, [0.2, 0.3])
    result = advance(DOOR, state, (0.9, 0.6), 1.0, POLICY, eligible=True)
    assert result.outcome is CrossingOutcome.OUT_OF_ORDER


# ============================================================================ monitor unit
def box_at(
    x: float, *, y_bottom: float = 0.8, width: int = WIDTH, height: int = HEIGHT
) -> tuple[float, float, float, float]:
    cx, bottom = x * width, y_bottom * height
    return (cx - 20, bottom - 100, cx + 20, bottom)


def monitor_walk(
    monitor: PortalMonitor, track_id: int, xs: list[float], *, start_ms: float = 0.0
) -> list[RoomTransition]:
    emitted: list[RoomTransition] = []
    for i, x in enumerate(xs):
        emitted += monitor.observe(
            track_id,
            box_at(x),
            width=WIDTH,
            height=HEIGHT,
            timestamp_ms=start_ms + i * 180.0,
            eligible=True,
        )
    return emitted


def test_track_birth_and_death_never_emit() -> None:
    monitor = PortalMonitor(PortalSet((DOOR,)), stream_id="s")
    assert monitor_walk(monitor, 1, [0.8, 0.8, 0.8]) == []  # born inside
    monitor.forget(1)  # dies inside: no exit
    assert monitor_walk(monitor, 2, [0.2, 0.2]) == []  # born outside
    monitor.forget(2)  # dies outside: no entry
    assert monitor.snapshot()["entries_total"] == monitor.snapshot()["exits_total"] == 0
    assert monitor.tracked() == 0


def test_a_new_track_id_starts_with_independent_state() -> None:
    monitor = PortalMonitor(PortalSet((DOOR,)), stream_id="s")
    monitor_walk(monitor, 1, [0.2, 0.2])
    # Track 2 is first seen inside: its birth there is not track 1's entry.
    assert monitor_walk(monitor, 2, [0.8, 0.8, 0.8, 0.8]) == []


def test_reset_clears_state_and_synthesises_nothing() -> None:
    monitor = PortalMonitor(PortalSet((DOOR,)), stream_id="s")
    monitor_walk(monitor, 1, [0.8, 0.8])
    monitor.reset()
    assert monitor.tracked() == 0 and monitor.resets_total == 1
    # After the break the same person, under whatever id, initialises where they are seen.
    assert monitor_walk(monitor, 1, [0.2, 0.2, 0.2], start_ms=10_000.0) == []


def test_two_portals_are_independent_and_events_are_ordered() -> None:
    inner = Portal("inner", 0.4, 0.05, 0.4, 0.95, InsideSide.RIGHT, "inner door")
    outer = Portal("outer", 0.7, 0.05, 0.7, 0.95, InsideSide.RIGHT, "outer door")
    monitor = PortalMonitor(PortalSet((inner, outer)), stream_id="s")
    emitted = monitor_walk(monitor, 7, [0.1, 0.2, 0.5, 0.55, 0.6, 0.62, 0.8, 0.85, 0.9, 0.9])
    assert [(e.portal_id, e.kind) for e in emitted] == [
        ("inner", RoomTransitionKind.PERSON_ENTERED_ROOM),
        ("outer", RoomTransitionKind.PERSON_ENTERED_ROOM),
    ]
    assert emitted[0].timestamp_ms < emitted[1].timestamp_ms
    assert [e.sequence for e in emitted] == [1, 2]
    per = monitor.snapshot()["per_portal"]
    assert (per["inner"]["entries"], per["outer"]["entries"]) == (1, 1)


def test_state_is_bounded_per_portal() -> None:
    monitor = PortalMonitor(
        PortalSet((DOOR,)), stream_id="s", policy=CrossingPolicy(max_tracked_per_portal=8)
    )
    for track in range(1, 50):
        monitor_walk(monitor, track, [0.2])
    snapshot = monitor.snapshot()
    assert snapshot["tracked_states"] == 8
    assert snapshot["per_portal"]["door"]["evictions"] == 41
    for _ in range(300):
        monitor.observe(
            99, box_at(0.3), width=WIDTH, height=HEIGHT, timestamp_ms=0.0, eligible=True
        )
    assert len(monitor.snapshot(recent_limit=1000)["recent"]) <= 100


def test_disabled_portals_are_not_evaluated() -> None:
    off = Portal("off", 0.5, 0.05, 0.5, 0.95, InsideSide.RIGHT, enabled=False)
    monitor = PortalMonitor(PortalSet((off,)), stream_id="s")
    assert not monitor
    assert monitor_walk(monitor, 1, [0.2, 0.3, 0.7, 0.7, 0.7, 0.7]) == []


# ========================================================================== runtime-level
def scene(canvas: Any, index: int) -> None:
    canvas[:] = 40


def walking_script(xs: list[float]) -> dict[int, list[Any]]:
    return {i: [(*box_at(x), 0.9)] for i, x in enumerate(xs)}


def runtime_for(
    xs: list[float] | dict[int, list[Any]],
    *,
    portals: PortalSet | None = None,
    source: Any = None,
    **kwargs: Any,
) -> LiveDemoRuntime:
    script = walking_script(xs) if isinstance(xs, list) else xs
    frames = (max(script) + 1) if script else 1
    runtime = LiveDemoRuntime(
        source
        or FakeLiveSource(
            frame_count=frames, width=WIDTH, height=HEIGHT, painter=scene, interval_seconds=PACED
        ),
        FakePersonDetector(script),  # type: ignore[arg-type]
        tracking_config=CONFIG,
        portals=PortalSet((DOOR,)) if portals is None else portals,
        **kwargs,
    )
    runtime.run()
    return runtime


def kinds(runtime: LiveDemoRuntime) -> list[str]:
    return [event["kind"] for event in reversed(runtime.room_transitions()["recent"])]


def linear(start: float, end: float, steps: int) -> list[float]:
    return [start + (end - start) * i / (steps - 1) for i in range(steps)]


def test_a_person_walking_in_through_the_door_is_one_entry_end_to_end() -> None:
    runtime = runtime_for(linear(0.15, 0.85, 40))
    assert kinds(runtime) == ["PERSON_ENTERED_ROOM"]
    # Appearing in view was still reported as appearing, and the tracker's end at stream end
    # was not an exit.
    timeline = [event["kind"] for event in runtime.timeline.recent(200)]
    assert "PERSON_APPEARED_IN_VIEW" in timeline


def test_walking_in_and_out_again_is_an_entry_then_an_exit() -> None:
    runtime = runtime_for(linear(0.15, 0.85, 30) + linear(0.85, 0.15, 30))
    assert kinds(runtime) == ["PERSON_ENTERED_ROOM", "PERSON_EXITED_ROOM"]


def test_scheduler_gaps_keep_one_track_and_one_crossing() -> None:
    # Sampled inference: the tracker only sees the frames that were detected on, 200 ms apart
    # here, each with its own capture timestamp - well inside the tracker's lost tolerance.
    xs = linear(0.15, 0.85, 14)
    source = _ScriptedSource([(WIDTH, HEIGHT)] * len(xs), breaks=set(), step_ms=200.0)
    runtime = runtime_for(xs, source=source)
    assert kinds(runtime) == ["PERSON_ENTERED_ROOM"]
    assert runtime.metrics()["tracks_created_total"] == 1


def test_a_person_already_inside_at_startup_and_then_lost_produces_nothing() -> None:
    runtime = runtime_for([0.8] * 20)  # stream ends with the track still inside
    assert kinds(runtime) == []
    lost = runtime_for({i: [(*box_at(0.8), 0.9)] for i in range(10)} | {40: []})
    assert kinds(lost) == []  # the track died inside: no exit


def test_a_tentative_track_never_reaches_the_portals() -> None:
    # One detection only: the tracker never confirms it, so no observation exists at all.
    runtime = runtime_for({0: [(*box_at(0.2), 0.9)], 1: [], 2: []})
    assert runtime.room_transitions()["tracked_states"] == 0
    assert kinds(runtime) == []


def test_an_unvalidated_candidate_is_suppressed_not_announced() -> None:
    xs = linear(0.15, 0.85, 40)
    # Low-confidence all the way: confirmed by the tracker only via high scores initially, then
    # candidates in the occupancy ledger - its crossing is counted, not announced.
    script = {i: [(*box_at(x), 0.9 if i < 2 else 0.12)] for i, x in enumerate(xs)}
    runtime = runtime_for(script)
    transitions = runtime.room_transitions()
    assert kinds(runtime) == []
    assert transitions["per_portal"]["door"]["suppressed_not_validated"] == 1
    assert runtime.metrics()["tracks_created_total"] == 1


class _ScriptedSource(FakeLiveSource):
    """Frames with a scripted discontinuity flag and frame geometry."""

    def __init__(
        self, sizes: list[tuple[int, int]], breaks: set[int], step_ms: float = 1000.0 / 30.0
    ) -> None:
        super().__init__(frame_count=len(sizes), width=WIDTH, height=HEIGHT, painter=scene)
        self._sizes, self._breaks, self._step = sizes, breaks, step_ms

    def frames(self) -> Iterator[LiveFrame]:
        for index, (width, height) in enumerate(self._sizes):
            time.sleep(PACED)
            yield LiveFrame(
                kind=self.kind,
                source_id=self.source_id,
                frame_index=index,
                timestamp_ms=index * self._step,
                monotonic_ns=time.monotonic_ns(),
                width=width,
                height=height,
                image=np.zeros((height, width, 3), dtype=np.uint8),
                discontinuity=index in self._breaks,
            )


def test_a_reconnect_while_inside_synthesises_no_exit_or_entry() -> None:
    # Inside, reconnect, then the (new) track is first seen outside: no EXIT, and its later
    # re-entry is a genuine crossing.
    xs = [0.8] * 10 + [0.2] * 10 + linear(0.2, 0.8, 15)
    source = _ScriptedSource([(WIDTH, HEIGHT)] * len(xs), breaks={10})
    runtime = runtime_for(xs, source=source)
    assert kinds(runtime) == ["PERSON_ENTERED_ROOM"]
    assert runtime.room_transitions()["resets_total"] >= 1


def test_a_resolution_change_synthesises_nothing() -> None:
    xs = [0.8] * 10 + [0.2] * 10
    sizes = [(WIDTH, HEIGHT)] * 10 + [(WIDTH * 2, HEIGHT * 2)] * 10
    script = {
        i: [(*box_at(x, width=w, height=h), 0.9)]
        for i, (x, (w, h)) in enumerate(zip(xs, sizes, strict=True))
    }
    runtime = runtime_for(script, source=_ScriptedSource(sizes, breaks=set()))
    assert kinds(runtime) == []
    assert runtime.room_transitions()["resets_total"] >= 1


def test_no_portal_preserves_the_previous_behaviour() -> None:
    xs = linear(0.15, 0.85, 30)
    with_door = runtime_for(xs)
    without = runtime_for(xs, portals=PortalSet())
    assert without.room_transitions()["enabled"] == 0 and kinds(without) == []
    keys = ("tracks_created_total", "peak_occupancy")
    assert {k: without.metrics()[k] for k in keys} == {k: with_door.metrics()[k] for k in keys}
    # Room transitions are their own vocabulary; the camera-view timeline never gains them.
    for runtime in (with_door, without):
        assert not {e["kind"] for e in runtime.timeline.recent(200)} & {
            "PERSON_ENTERED_ROOM",
            "PERSON_EXITED_ROOM",
        }


# =================================================================================== CLI
def cli_arguments(*portals: str, **overrides: Any) -> argparse.Namespace:
    parser = live_cli.add_demo_arguments(argparse.ArgumentParser())
    argv = ["--source", "synthetic", "--headless", "--max-frames", "3", "--detector", "none"]
    for portal in portals:
        argv += ["--portal", portal]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return parser.parse_args(argv)


@pytest.mark.parametrize(
    "portals",
    [
        ("0.5,0.5,0.5,0.5,RIGHT",),
        ("0.5,0.1,0.5,nan,RIGHT",),
        ("0.5,0.1,0.5,1.5,RIGHT",),
        ("0.5,0.1,0.5,0.9,ABOVE",),
        tuple(f"p{i}:0.{i + 1},0.1,0.{i + 1},0.9,RIGHT" for i in range(MAX_PORTALS + 1)),
    ],
)
def test_an_invalid_portal_fails_before_any_source_or_detector_starts(
    portals: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[str] = []
    monkeypatch.setattr(live_cli, "_source", lambda *_: started.append("source"))
    monkeypatch.setattr(live_cli, "_detector", lambda *_: started.append("detector"))
    assert live_cli.run_demo_cli(cli_arguments(*portals)) == 2
    assert started == []
    assert "invalid portal" in capsys.readouterr().err


def test_an_invalid_portal_policy_fails_before_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[str] = []
    monkeypatch.setattr(live_cli, "_source", lambda *_: started.append("source"))
    arguments = cli_arguments("0.5,0.1,0.5,0.9,RIGHT", portal_confirm_observations=0)
    assert live_cli.run_demo_cli(arguments) == 2 and started == []
    arguments = cli_arguments("0.5,0.1,0.5,0.9,RIGHT", portal_deadband=0.5)
    assert live_cli.run_demo_cli(arguments) == 2 and started == []


def test_the_cli_runs_with_portals_within_the_bound_and_without_any(
    capsys: pytest.CaptureFixture[str],
) -> None:
    multiple = [f"p{i}:0.{i + 2},0.1,0.{i + 2},0.9,RIGHT,door {i}" for i in range(MAX_PORTALS)]
    assert live_cli.run_demo_cli(cli_arguments(*multiple)) == 0
    output = capsys.readouterr().out
    assert "Room transitions from 4 configured portal(s)" in output
    assert json.loads(output[output.index("\n{") :])["room_transitions"]["enabled"] == MAX_PORTALS
    assert live_cli.run_demo_cli(cli_arguments()) == 0
    output = capsys.readouterr().out
    assert "Room transitions" not in output
    assert json.loads(output[output.index("{") :])["room_transitions"]["configured"] == 0


def test_portal_flags_round_trip() -> None:
    portal = parse_portal("door-a:0.5,0.05,0.5,0.95,RIGHT,Main door")
    assert parse_portal(portal.as_flag()) == portal
    wide = parse_portal("0.5,0.05,0.5,0.95,RIGHT,Side door,deadband=0.04")
    assert wide.deadband == 0.04 and parse_portal(wide.as_flag()) == wide
    bare = parse_portal("0.5,0.05,0.5,0.95,RIGHT,deadband=0.03")
    assert (bare.label, bare.deadband) == ("", 0.03)
    uuid_id = "3f2b7c1e-8d4a-4f6b-9a1c-0e5d7b2a9c41"
    assert parse_portal(f"{uuid_id}:0.5,0.05,0.5,0.95,RIGHT,Door").portal_id == uuid_id
    for bad in ("0.5,0.05,0.5,0.95,RIGHT,Door,deadband=x", "0.5,0.05,0.5,0.95,RIGHT,Door,oops"):
        with pytest.raises(PortalError):
            parse_portal(bad)
    with pytest.raises(PortalError, match="deadband"):
        parse_portal("0.5,0.05,0.5,0.95,RIGHT,Door,deadband=0.5")


# ============================================================================== dashboard
def served(runtime: LiveDemoRuntime) -> tuple[str, dict[str, Any]]:
    with DemoServer(runtime, port=0) as server:
        host, port = server.address
        page = urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read().decode()
        state = json.loads(
            urllib.request.urlopen(f"http://{host}:{port}/api/state", timeout=5).read()
        )
    return page, state


def test_the_dashboard_shows_entries_exits_and_the_portal_safely() -> None:
    runtime = runtime_for(linear(0.15, 0.85, 30) + linear(0.85, 0.15, 30))
    page, state = served(runtime)
    transitions = state["room_transitions"]
    assert (transitions["entries_total"], transitions["exits_total"]) == (1, 1)
    assert [e["kind"] for e in transitions["recent"]] == [
        "PERSON_EXITED_ROOM",
        "PERSON_ENTERED_ROOM",
    ]
    portal = transitions["portals"][0]
    assert portal == DOOR.as_dict() and portal["inside_normal"] == [1.0, 0.0]
    json.dumps(transitions)  # serialises
    assert "Room transitions" in page and "Entered via " in page and "Exited via " in page
    # Operator labels reach the page through textContent only.
    body = page[page.index("function transitions") : page.index("function teardown")]
    assert "textContent" in body and "innerHTML" not in body


def test_the_dashboard_uses_no_identity_or_demographic_words() -> None:
    runtime = runtime_for(linear(0.15, 0.85, 30))
    page, state = served(runtime)
    words = set(re.findall(r"[a-z]+", page.lower()))
    for forbidden in (
        "teacher",
        "teachers",
        "child",
        "children",
        "adult",
        "age",
        "gender",
        "identity",
        "name",
        "names",
        "score",
        "staff",
        "guardian",
        "parent",
        "kid",
    ):
        assert forbidden not in words, forbidden
    rendered = json.dumps(state["room_transitions"]).lower()
    for forbidden in ("teacher", "child", "staff", "guardian", "face", "identity", "adult"):
        assert forbidden not in rendered, forbidden


def test_the_preview_draws_the_portal_without_its_label() -> None:
    from veotrex_edge_agent.live.preview import PreviewConfig, PreviewRenderer

    preview = PreviewRenderer(PreviewConfig(target_fps=30.0))
    runtime = runtime_for(linear(0.15, 0.85, 20), preview=preview)
    assert preview.previews_encoded_total > 0 and preview.encode_failures_total == 0
    text = (LIVE_ROOT / "preview.py").read_text(encoding="utf-8")
    assert "portal.label" not in text
    assert runtime.room_transitions()["enabled"] == 1


# ================================================================================ privacy
def test_the_event_schema_carries_no_identity_image_or_cross_camera_field() -> None:
    names = {field.name for field in fields(RoomTransition)}
    assert names == {
        "sequence",
        "kind",
        "direction",
        "stream_id",
        "track_id",
        "portal_id",
        "portal_label",
        "timestamp_ms",
        "crossing_point",
        "evidence_observations",
    }
    for name in names:
        for forbidden in (
            "child",
            "staff",
            "guardian",
            "profile",
            "face",
            "embedding",
            "biometric",
            "crop",
            "image",
            "frame",
            "clothing",
            "global",
            "person_id",
            "identity",
        ):
            assert forbidden not in name, (name, forbidden)


def test_the_vocabulary_is_anonymous() -> None:
    assert {str(kind) for kind in RoomTransitionKind} == {
        "PERSON_ENTERED_ROOM",
        "PERSON_EXITED_ROOM",
    }
    assert {str(d) for d in Direction} == {"OUTSIDE_TO_INSIDE", "INSIDE_TO_OUTSIDE"}
    joined = " ".join(str(kind) for kind in RoomTransitionKind).lower()
    for forbidden in ("teacher", "child", "staff", "guardian", "adult", "intruder"):
        assert forbidden not in joined


@pytest.mark.parametrize("module", ["portal_geometry.py", "portal_crossing.py"])
def test_the_portal_modules_reach_no_face_identity_network_or_storage(module: str) -> None:
    text = (LIVE_ROOT / module).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    for forbidden in (
        "face_backend",
        "face_matching",
        "sface",
        "yunet",
        "cv2",
        "urllib",
        "requests",
        "httpx",
        "open(",
        "imwrite",
        "staff_profile",
        "child_profile",
        "guardian",
        "embedding",
    ):
        assert forbidden not in code, (module, forbidden)


def test_no_room_specific_coordinates_are_written_into_the_source() -> None:
    suspicious = re.compile(r"\b0\.\d{2,}\s*,\s*0\.\d{2,}\s*,\s*0\.\d{2,}\s*,\s*0\.\d{2,}")
    for path in (LIVE_ROOT / "portal_geometry.py", LIVE_ROOT / "portal_crossing.py"):
        assert not suspicious.search(path.read_text(encoding="utf-8")), path.name


def test_the_dashboard_page_says_appearance_is_not_entry() -> None:
    assert "Appearing\n        in or leaving the view is not an entry or an exit" in PAGE


# ============================================================================ performance
def test_portal_evaluation_is_cheap_and_bounded() -> None:
    portals = PortalSet(
        tuple(
            Portal(f"p{i}", 0.2 + 0.2 * i, 0.05, 0.2 + 0.2 * i, 0.95, InsideSide.RIGHT)
            for i in range(MAX_PORTALS)
        )
    )
    monitor = PortalMonitor(portals, stream_id="bench")
    rng = np.random.default_rng(7)
    started = time.perf_counter()
    for step in range(200):
        for track in range(1, 201):  # 200 tracks x 200 observations x 4 portals
            x = float(np.clip(0.5 + 0.45 * math.sin(step / 15 + track) + rng.normal(0, 0.01), 0, 1))
            monitor.observe(
                track,
                box_at(x),
                width=WIDTH,
                height=HEIGHT,
                timestamp_ms=step * 180.0,
                eligible=True,
            )
    elapsed = time.perf_counter() - started
    snapshot = monitor.snapshot()
    assert snapshot["tracked_states"] == 200 * MAX_PORTALS
    assert snapshot["evaluation_us"]["count"] == 40_000
    # Generous: tens of microseconds per observation against a ~100 ms detector.
    assert snapshot["evaluation_us"]["p95"] < 2_000
    assert elapsed < 30
