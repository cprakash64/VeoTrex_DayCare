"""The self-contained owner demo page and its telemetry endpoint.

No Node, no bundler, no database: the page is served by this process and polls this process.
"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from veotrex_edge_agent.frame_source.source import SourceHealth
from veotrex_edge_agent.monitoring.occupancy import CoverageState
from veotrex_edge_agent.monitoring.owner_demo import OWNER_DEMO_HTML
from veotrex_edge_agent.monitoring.pipeline import MonitoringPipeline
from veotrex_edge_agent.monitoring.server import make_server, owner_status_payload

from test_monitoring_pipeline import StubDetector, StubSource

BOX = [(10.0, 10.0, 60.0, 170.0), (120.0, 10.0, 170.0, 170.0)]


def built(frames: int = 5) -> tuple[MonitoringPipeline, StubSource]:
    source = StubSource(frames)
    pipeline = MonitoringPipeline(
        source,  # type: ignore[arg-type]
        StubDetector(BOX),
        area_label="Demo Classroom",
        camera_label="Demo Camera",
    )
    for frame in source.frames():
        pipeline._process(frame)
    return pipeline, source


@pytest.fixture
def served():
    pipeline, source = built()
    server = make_server(pipeline, host="127.0.0.1", port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", pipeline, source
    finally:
        server.shutdown()
        server.server_close()


def get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - loopback test URL
        return response.status, response.read().decode()


def test_owner_demo_returns_200_html(served) -> None:
    base, _, _ = served
    status, body = get(f"{base}/owner-demo")
    assert status == 200
    assert body.lstrip().startswith("<!doctype html>")


def test_the_page_says_recorded_demo_and_never_claims_live_ring() -> None:
    markup = OWNER_DEMO_HTML.lower()
    assert "recorded demo" in markup
    assert "live • ring" not in markup
    assert "live ring" not in markup


def test_the_page_carries_no_secrets_or_paths() -> None:
    markup = OWNER_DEMO_HTML.lower()
    for forbidden in ("bearer ", "secret", "password", "token", "/run/secrets", "auth0", ".mp4"):
        assert forbidden not in markup, forbidden


def test_status_endpoint_reports_real_runtime_state(served) -> None:
    base, _, _ = served
    status, body = get(f"{base}/api/demo/status")
    assert status == 200
    payload = json.loads(body)
    assert payload["source"] == "RECORDED_DEMO"
    assert payload["is_live"] is False
    assert payload["coverage"] == "ACTIVE"
    assert payload["occupancy"] == 2
    assert payload["active_tracks"] == 2
    assert payload["peak_occupancy"] == 2
    assert payload["tracks_observed"] == 2
    assert payload["inference_latency_ms"] == 12.5


def test_status_payload_never_reports_zero_occupancy_when_coverage_is_lost(served) -> None:
    base, _, source = served
    source.health = SourceHealth.STOPPED
    _, body = get(f"{base}/api/demo/status")
    payload = json.loads(body)
    assert payload["coverage"] == "UNKNOWN"
    assert payload["occupancy"] is None
    assert payload["active_tracks"] is None


def test_impaired_coverage_also_withholds_the_count() -> None:
    pipeline, _ = built()
    snapshot = pipeline.snapshot()
    assert snapshot.occupancy.coverage is CoverageState.ACTIVE
    # Drive the read-time staleness path rather than faking a snapshot.
    pipeline._last_frame_at = pipeline._clock() - 3600.0
    payload = owner_status_payload(pipeline.snapshot())
    assert payload["coverage"] == "IMPAIRED"
    assert payload["occupancy"] is None


def test_the_payload_carries_no_identity_or_configuration(served) -> None:
    base, _, _ = served
    _, body = get(f"{base}/api/demo/status")
    payload = json.loads(body)
    assert set(payload) == {
        "source", "is_live", "area", "coverage", "occupancy", "active_tracks",
        "peak_occupancy", "tracks_observed", "longest_track_seconds", "session_seconds",
        "fps", "inference_latency_ms", "frames_processed", "events",
    }


def test_track_lifecycle_produces_real_activity_entries(served) -> None:
    base, _, _ = served
    _, body = get(f"{base}/api/demo/status")
    kinds = {event["kind"] for event in json.loads(body)["events"]}
    assert "TRACK_STARTED" in kinds
    assert "PEAK_OCCUPANCY" in kinds
