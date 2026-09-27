"""V1-05B: the managed edge path - control-plane client, runtime config with last-known-good,
durable outbox and uploader, CLI precedence and lifecycle.

Everything runs against ``FakePlane``, an in-memory stand-in for the control plane's two machine
routes plugged in through the client's connection factory. No network, no real credential, no
image. The real API is exercised end to end in ``apps/api/tests/test_edge_room_events_e2e.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from test_portal_crossing import linear, runtime_for

from veotrex_edge_agent.edge_control_plane import (
    DEFAULT_TIMEOUT_SECONDS,
    RUNTIME_CONFIG_PATH,
    ControlPlaneEndpoint,
    ControlPlaneError,
    EdgeControlPlaneClient,
    resolve_control_plane,
)
from veotrex_edge_agent.live import cli as live_cli
from veotrex_edge_agent.live.managed_config import (
    CACHE_FILE_NAME,
    ConfigCache,
    ConfigRefresher,
    ManagedConfigError,
    ManagedPortalConfig,
    configuration_revision,
    parse_runtime_config,
)
from veotrex_edge_agent.live.managed_session import (
    ManagedEdgeSession,
    ManagedPlan,
    PortalPlanError,
    resolve_portal_plan,
)
from veotrex_edge_agent.live.portal_crossing import (
    Direction,
    PortalMonitor,
    RoomTransition,
    RoomTransitionKind,
)
from veotrex_edge_agent.live.portal_geometry import InsideSide, Portal, PortalSet
from veotrex_edge_agent.live.room_event_outbox import (
    BACKOFF_MAX_SECONDS,
    DEAD_LETTER_CAPACITY,
    OUTBOX_FILE_NAME,
    PAYLOAD_KEYS,
    RoomEventOutbox,
    RoomEventUploader,
    backoff_seconds,
    event_payload,
)

# Obviously synthetic: a fixed selector and a secret of repeated letters, assembled at
# runtime so no credential-shaped literal sits in the source (secret scanning).
SELECTOR = "3f2b1a09-8c7d-4e6f-a5b4-c3d2e1f0a9b8"
TOKEN = ".".join(("vte1", SELECTOR, "S" * 42 + "w"))
SECRET = TOKEN.rsplit(".", 1)[1]
NODE = "7a1d2c3b-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
CAMERA = "5e1f0c2a-7d3b-4a6e-9c1f-2b8d4e6a0c11"
OTHER_CAMERA = "0b9f8a3c-1d2e-4f50-8a6b-7c8d9e0f1a2b"
ASSIGNMENT = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
PORTAL = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"


# ================================================================================ fixtures
def portal_doc(portal_id: str = PORTAL, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "portal_id": portal_id,
        "label": "Main Door",
        "x1": 0.5,
        "y1": 0.05,
        "x2": 0.5,
        "y2": 0.95,
        "inside": "RIGHT",
        "enabled": True,
        "deadband": 0.02,
        "revision": 1,
    }
    body.update(changes)
    return body


def camera_doc(
    camera_id: str = CAMERA, portals: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    document = {
        "camera_id": camera_id,
        "assignment_id": ASSIGNMENT,
        "portals": sorted(
            [portal_doc()] if portals is None else portals, key=lambda p: str(p["portal_id"])
        ),
    }
    return {**document, "configuration_revision": configuration_revision(document)}


def runtime_doc(*cameras: dict[str, Any]) -> dict[str, Any]:
    cameras = cameras or (camera_doc(),)
    version = configuration_revision(
        {
            "cameras": sorted(
                [
                    {"camera_id": c["camera_id"], "revision": c["configuration_revision"]}
                    for c in cameras
                ],
                key=lambda item: item["camera_id"],
            )
        }
    )
    return {
        "schema_version": 1,
        "edge_node_id": NODE,
        "config_version": version,
        "cameras": list(cameras),
    }


class FakeResponse:
    def __init__(
        self, status: int, body: bytes, content_type: str, drip: Callable[[], None] | None
    ) -> None:
        self.status = status
        self._body = body
        self._type = content_type
        self._drip = drip

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._type if name.lower() == "content-type" else default

    def read(self, amount: int = -1) -> bytes:
        if self._drip is not None:
            self._drip()
            amount = min(amount, 1) if amount > 0 else 1
        chunk = self._body if amount < 0 else self._body[:amount]
        self._body = self._body[len(chunk) :]
        return chunk


class FakePlane:
    """The control plane's two machine routes, in memory, with server-like idempotency."""

    def __init__(self) -> None:
        self.config: Any = runtime_doc()
        self.config_status = 200
        self.event_status = 200
        self.content_type = "application/json"
        self.down = False
        self.reject: dict[str, str] = {}  # event_id -> category
        self.stored: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.timeouts: list[float] = []
        self.hosts: list[tuple[str, int]] = []
        self.raw: bytes | None = None
        self.drip: Callable[[], None] | None = None

    def factory(self, host: str, port: int, timeout: float) -> Any:
        self.hosts.append((host, port))
        self.timeouts.append(timeout)
        return FakeConnection(self)

    def respond(self, method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        if self.raw is not None:
            return 200, self.raw
        if path == RUNTIME_CONFIG_PATH:
            return self.config_status, json.dumps(self.config).encode()
        if self.event_status != 200:
            return self.event_status, b'{"detail":"x"}'
        results = []
        for item in json.loads(body or b"{}")["events"]:
            event_id = item["event_id"]
            if event_id in self.reject:
                results.append(
                    {"event_id": event_id, "status": "REJECTED", "category": self.reject[event_id]}
                )
            elif event_id in self.stored:
                results.append({"event_id": event_id, "status": "DUPLICATE", "category": None})
            else:
                self.stored[event_id] = item
                results.append({"event_id": event_id, "status": "ACCEPTED", "category": None})
        return 200, json.dumps({"results": results}).encode()

    def event_posts(self) -> list[list[dict[str, Any]]]:
        return [
            json.loads(body or b"{}")["events"]
            for method, path, _, body in self.requests
            if method == "POST"
        ]


class FakeConnection:
    def __init__(self, plane: FakePlane) -> None:
        self.plane = plane
        self._pending: tuple[str, str, bytes | None] | None = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self.plane.down:
            raise ConnectionRefusedError("synthetic outage")
        self.plane.requests.append((method, path, dict(headers or {}), body))
        self._pending = (method, path, body)

    def getresponse(self) -> FakeResponse:
        assert self._pending is not None
        status, body = self.plane.respond(*self._pending)
        return FakeResponse(status, body, self.plane.content_type, self.plane.drip)

    def close(self) -> None:
        pass


@pytest.fixture
def credential(tmp_path: Path) -> Path:
    path = tmp_path / "edge.credential"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(TOKEN + "\n")
    return path


@pytest.fixture
def plane() -> FakePlane:
    return FakePlane()


def endpoint() -> ControlPlaneEndpoint:
    return ControlPlaneEndpoint.parse("https://127.0.0.1:8443", environment="test")


def client(plane: FakePlane, credential: Path, **options: Any) -> EdgeControlPlaneClient:
    return EdgeControlPlaneClient(
        endpoint(), str(credential), connection_factory=plane.factory, **options
    )


def private_dir(tmp_path: Path, name: str = "state") -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    return path


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def transition(
    kind: RoomTransitionKind = RoomTransitionKind.PERSON_ENTERED_ROOM, track: int = 7
) -> RoomTransition:
    return RoomTransition(
        sequence=1,
        kind=kind,
        direction=Direction.OUTSIDE_TO_INSIDE,
        stream_id="synthetic",
        track_id=track,
        portal_id=PORTAL,
        portal_label="Main Door",
        timestamp_ms=1000.0,
        crossing_point=(0.5, 0.61),
        evidence_observations=3,
    )


def payload(**changes: Any) -> dict[str, Any]:
    body = event_payload(
        transition(),
        camera_id=CAMERA,
        stream_instance_id=uuid4().hex,
        occurred_at_unix=1_800_000_000.0,
    )
    body.update(changes)
    return body


# ============================================================================ config client
def test_a_valid_config_is_fetched_with_only_the_credential_header(
    plane: FakePlane, credential: Path
) -> None:
    document = client(plane, credential).get_json(RUNTIME_CONFIG_PATH)
    assert parse_runtime_config(document).cameras[CAMERA].portals.portals[0].label == "Main Door"
    method, path, headers, body = plane.requests[0]
    assert (method, path, body) == ("GET", RUNTIME_CONFIG_PATH, None)
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert plane.hosts == [("127.0.0.1", 8443)]
    assert plane.timeouts == [DEFAULT_TIMEOUT_SECONDS]


def test_https_is_required_and_the_credential_file_is_checked(
    credential: Path, tmp_path: Path
) -> None:
    for url in (
        "http://127.0.0.1:8443",
        "ftp://example.com",
        "https://user:pw@example.com",
        "https://example.com/v1",
    ):
        with pytest.raises(ControlPlaneError) as refused:
            resolve_control_plane(url, str(credential), environment="test")
        assert refused.value.category == "control_plane_url_invalid"
    # Outside local/test a loopback control plane is refused too.
    with pytest.raises(ControlPlaneError):
        resolve_control_plane("https://127.0.0.1:8443", str(credential), environment="production")
    loose = tmp_path / "loose.credential"
    loose.write_text(TOKEN)
    loose.chmod(0o644)
    with pytest.raises(ControlPlaneError) as refused:
        resolve_control_plane("https://127.0.0.1:8443", str(loose), environment="test")
    assert refused.value.category == "credential_file_permissions_too_open"
    assert resolve_control_plane(
        "https://127.0.0.1:8443", str(credential), environment="test"
    ).origin == ("https://127.0.0.1:8443")


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_refused_not_followed(
    plane: FakePlane, credential: Path, status: int
) -> None:
    plane.config_status = status
    with pytest.raises(ControlPlaneError) as refused:
        client(plane, credential).get_json(RUNTIME_CONFIG_PATH)
    assert refused.value.category == "redirect_refused" and not refused.value.retryable
    assert len(plane.requests) == 1


def test_timeouts_and_body_size_are_bounded(plane: FakePlane, credential: Path) -> None:
    for bad in (0, -1, 61):
        with pytest.raises(ValueError):
            client(plane, credential, timeout_seconds=bad)
    # A server trickling one byte at a time past the total budget.
    ticks = iter(range(1000))
    plane.drip = lambda: None
    slow = client(plane, credential, timeout_seconds=5, monotonic=lambda: float(next(ticks)))
    with pytest.raises(ControlPlaneError) as refused:
        slow.get_json(RUNTIME_CONFIG_PATH)
    assert refused.value.category == "transport_failed"
    plane.drip = None
    plane.raw = b"[" + b"0," * 200_000 + b"0]"
    with pytest.raises(ControlPlaneError) as refused:
        client(plane, credential).get_json(RUNTIME_CONFIG_PATH)
    assert refused.value.category == "response_too_large"


def test_only_the_two_routes_json_and_bounded_categories(
    plane: FakePlane, credential: Path
) -> None:
    edge = client(plane, credential)
    with pytest.raises(ControlPlaneError) as refused:
        edge.get_json("/v1/children")
    assert refused.value.category == "path_not_allowed" and plane.requests == []
    plane.content_type = "text/html"
    with pytest.raises(ControlPlaneError) as refused:
        edge.get_json(RUNTIME_CONFIG_PATH)
    assert refused.value.category == "unexpected_media_type"
    plane.content_type = "application/json"
    expected = {
        401: "auth_rejected",
        403: "auth_rejected",
        404: "endpoint_unavailable",
        405: "endpoint_unavailable",
        429: "rate_limited",
        500: "server_error",
        503: "server_error",
        400: "request_rejected",
        422: "request_rejected",
    }
    for status, category in expected.items():
        plane.config_status = status
        with pytest.raises(ControlPlaneError) as refused:
            edge.get_json(RUNTIME_CONFIG_PATH)
        assert refused.value.category == category, status
        assert TOKEN not in str(refused.value) and SECRET not in repr(refused.value)
    assert TOKEN not in repr(edge)


# ====================================================================== config validation
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(extra="x"),
        lambda d: d.update(edge_node_id="not-a-uuid"),
        lambda d: d.update(cameras="x"),
        lambda d: d["cameras"][0].update(provider_device_id="ring-123"),
        lambda d: d["cameras"][0]["portals"][0].update(x1=1.5),
        lambda d: d["cameras"][0]["portals"][0].update(x1="0.5"),
        lambda d: d["cameras"][0]["portals"][0].update(inside="INWARD"),
        lambda d: d["cameras"][0]["portals"][0].update(inside="ABOVE"),  # ambiguous
        lambda d: d["cameras"][0]["portals"][0].update(enabled="yes"),
        lambda d: d["cameras"][0]["portals"][0].update(person="x"),
        lambda d: d["cameras"][0]["portals"][0].update(revision=0),
        lambda d: d["cameras"][0].update(configuration_revision="sha256:" + "0" * 64),
        lambda d: d.update(config_version="sha256:" + "0" * 64),
        lambda d: d["cameras"].append(d["cameras"][0]),
    ],
)
def test_malformed_or_mismatched_documents_are_refused_whole(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    document = runtime_doc()
    mutate(document)
    with pytest.raises(ManagedConfigError):
        parse_runtime_config(document)


def test_a_tampered_geometry_fails_its_revision() -> None:
    document = runtime_doc()
    document["cameras"][0]["portals"][0]["x1"] = 0.4
    document["cameras"][0]["portals"][0]["x2"] = 0.4
    with pytest.raises(ManagedConfigError) as refused:
        parse_runtime_config(document)
    assert refused.value.category == "configuration_revision_mismatch"


def test_excessive_portals_and_cameras_are_refused() -> None:
    five = [
        portal_doc(str(uuid4()), label=f"Door {i}", x1=0.1 + i / 10, x2=0.1 + i / 10)
        for i in range(5)
    ]
    with pytest.raises(ManagedConfigError) as refused:
        parse_runtime_config(runtime_doc(camera_doc(portals=five)))
    assert refused.value.category == "too_many_portals"
    many = [camera_doc(str(uuid4())) for _ in range(33)]
    with pytest.raises(ManagedConfigError):
        parse_runtime_config(runtime_doc(*many))
    four = five[:4]
    assert (
        len(
            parse_runtime_config(runtime_doc(camera_doc(portals=four)))
            .cameras[CAMERA]
            .portals.portals
        )
        == 4
    )


def test_the_edge_revision_is_independent_of_portal_order() -> None:
    portals = [
        portal_doc(str(uuid4()), label=f"Door {i}", x1=0.2 + i / 10, x2=0.2 + i / 10)
        for i in range(3)
    ]
    forward = camera_doc(portals=portals)
    backward = camera_doc(portals=list(reversed(portals)))
    assert forward["configuration_revision"] == backward["configuration_revision"]
    parsed = parse_runtime_config(runtime_doc(forward))
    assert [p.portal_id for p in parsed.cameras[CAMERA].portals.portals] == sorted(
        p["portal_id"] for p in portals
    )


# ============================================================ refresh, last-known-good, cache
def managed(
    plane: FakePlane, credential: Path, tmp_path: Path, clock: Clock | None = None
) -> ManagedPortalConfig:
    return ManagedPortalConfig(
        client(plane, credential),
        ConfigCache(private_dir(tmp_path) / "config"),
        CAMERA,
        clock=clock or Clock(),
    )


def test_refresh_applies_atomically_and_keeps_last_known_good(
    plane: FakePlane, credential: Path, tmp_path: Path
) -> None:
    config = managed(plane, credential, tmp_path)
    assert config.portals() == (PortalSet(), None), "nothing until the first valid fetch"
    assert config.refresh() is True
    portals, revision = config.portals()
    assert [p.label for p in portals.portals] == ["Main Door"] and revision is not None
    assert config.refresh() is False, "an unchanged document is not a change"
    for failure in ("down", "500", "malformed", "tampered"):
        plane.down = failure == "down"
        plane.config_status = 500 if failure == "500" else 200
        plane.config = {"nope": 1} if failure == "malformed" else runtime_doc()
        if failure == "tampered":
            plane.config["cameras"][0]["portals"][0]["y1"] = 0.1
        assert config.refresh() is False, failure
        assert config.portals() == (portals, revision), failure
    snapshot = config.snapshot()
    assert snapshot["config_fetch_success_total"] == 2
    assert snapshot["config_fetch_failure_total"] == 4
    assert snapshot["config_rejected_total"] == 2
    assert snapshot["last_failure_category"] == "configuration_revision_mismatch"
    # A new valid document replaces the whole set at once.
    plane.down, plane.config_status = False, 200
    moved = [
        portal_doc(x1=0.6, x2=0.6, revision=2),
        portal_doc(str(uuid4()), label="Side", x1=0.2, x2=0.2),
    ]
    plane.config = runtime_doc(camera_doc(portals=moved))
    assert config.refresh() is True
    after, new_revision = config.portals()
    assert new_revision != revision and sorted(p.x1 for p in after.portals) == [0.2, 0.6]
    assert config.snapshot()["config_changes_total"] == 2


def test_an_unassigned_camera_fails_closed(
    plane: FakePlane, credential: Path, tmp_path: Path
) -> None:
    config = managed(plane, credential, tmp_path)
    assert config.refresh() is True
    plane.config = runtime_doc(camera_doc(OTHER_CAMERA))
    assert config.refresh() is True, "losing the camera is a change"
    assert config.portals() == (PortalSet(), None)


def test_the_cache_is_private_geometry_only_and_survives_restart(
    plane: FakePlane, credential: Path, tmp_path: Path
) -> None:
    state = private_dir(tmp_path)
    first = ManagedPortalConfig(
        client(plane, credential), ConfigCache(state / "config"), CAMERA, clock=Clock()
    )
    assert first.refresh() is True
    path = state / "config" / CACHE_FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "config").stat().st_mode) == 0o700
    assert path.stat().st_uid == os.geteuid()
    content = path.read_text()
    assert TOKEN not in content and SECRET not in content and "Bearer" not in content
    assert set(json.loads(content)) == {"fetched_at", "runtime_config"}
    assert set(json.loads(content)["runtime_config"]) == {
        "schema_version",
        "edge_node_id",
        "config_version",
        "cameras",
    }
    # Restart with the control plane unreachable: the cached lines are used.
    plane.down = True
    second = ManagedPortalConfig(
        client(plane, credential), ConfigCache(state / "config"), CAMERA, clock=Clock()
    )
    assert second.start_from_cache() is True
    assert second.portals() == first.portals() and second.snapshot()["source"] == "cache"


@pytest.mark.parametrize("damage", ["garbage", "tampered", "loose_mode", "symlink", "oversized"])
def test_a_damaged_cache_is_ignored_and_the_feature_stays_off(
    plane: FakePlane, credential: Path, tmp_path: Path, damage: str
) -> None:
    state = private_dir(tmp_path)
    ManagedPortalConfig(
        client(plane, credential), ConfigCache(state / "config"), CAMERA, clock=Clock()
    ).refresh()
    path = state / "config" / CACHE_FILE_NAME
    if damage == "garbage":
        path.write_bytes(b"\x00not json")
    elif damage == "tampered":
        wrapper = json.loads(path.read_text())
        wrapper["runtime_config"]["cameras"][0]["portals"][0]["x1"] = 0.3
        path.write_text(json.dumps(wrapper))
    elif damage == "loose_mode":
        path.chmod(0o644)
    elif damage == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text(path.read_text())
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    else:
        path.write_bytes(b" " * 300_000)
    restarted = ManagedPortalConfig(
        client(plane, credential), ConfigCache(state / "config"), CAMERA, clock=Clock()
    )
    assert restarted.start_from_cache() is False
    assert restarted.portals() == (PortalSet(), None)
    assert restarted.snapshot()["cache_rejection"] is not None


def test_a_state_directory_others_can_read_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(ManagedConfigError) as refused:
        ConfigCache(shared)
    assert refused.value.category == "state_directory_not_private"
    with pytest.raises(ManagedConfigError):
        RoomEventOutbox(shared)


def test_the_refresher_is_one_bounded_thread_and_survives_callback_faults(
    plane: FakePlane, credential: Path, tmp_path: Path
) -> None:
    config = managed(plane, credential, tmp_path)
    staged: list[tuple[PortalSet, str | None]] = []
    with pytest.raises(ValueError):
        ConfigRefresher(config, lambda *a: None, interval_seconds=5)
    refresher = ConfigRefresher(config, lambda p, r: staged.append((p, r)), interval_seconds=30)
    assert refresher.run_once() is True and len(staged) == 1
    assert refresher.run_once() is False, "unchanged: nothing handed on"

    def boom(*_: Any) -> None:
        raise RuntimeError("callback fault")

    plane.config = runtime_doc(camera_doc(portals=[portal_doc(x1=0.7, x2=0.7, revision=2)]))
    faulty = ConfigRefresher(config, boom, interval_seconds=30)
    assert faulty.run_once() is False
    assert config.snapshot()["last_failure_category"] == "internal_error"
    # The thread makes its first attempt at once and stops promptly.
    plane.config = runtime_doc(camera_doc(portals=[portal_doc(x1=0.8, x2=0.8, revision=3)]))
    live = ConfigRefresher(config, lambda p, r: staged.append((p, r)), interval_seconds=600)
    live.start()
    for _ in range(200):
        if len(staged) == 2:
            break
        threading.Event().wait(0.01)
    assert len(staged) == 2 and live.running
    live.stop()
    assert not live.running


def test_a_staged_config_is_applied_whole_between_observations() -> None:
    old = Portal("a", 0.5, 0.05, 0.5, 0.95, InsideSide.RIGHT, "A")
    new = PortalSet((Portal("b", 0.3, 0.05, 0.3, 0.95, InsideSide.RIGHT, "B"),))
    monitor = PortalMonitor(PortalSet((old,)), stream_id="s")
    monitor.stage(new, "sha256:" + "1" * 64)
    assert monitor.portals.portals == (old,), "staging alone changes nothing"
    assert monitor.apply_staged() is True
    assert monitor.portals == new and monitor.revision == "sha256:" + "1" * 64
    monitor.stage(new, "sha256:" + "1" * 64)
    assert monitor.apply_staged() is False and monitor.config_applied_total == 1
    monitor.stage(PortalSet(), None)
    assert monitor.apply_staged() is True and not monitor


# ================================================================================== outbox
def test_enqueue_persists_before_upload_and_is_idempotent_per_event_id(tmp_path: Path) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path))
    event = payload()
    assert outbox.enqueue(event) is True
    assert outbox.enqueue(dict(event)) is True, "a re-enqueue is accepted and changes nothing"
    assert outbox.depth() == 1
    snapshot = outbox.snapshot()
    assert snapshot["room_transition_events_queued_total"] == 1
    assert snapshot["room_transition_events_duplicate_enqueue_total"] == 1
    assert snapshot["room_transition_queue_dropped_total"] == 0
    assert stat.S_IMODE((outbox.directory / OUTBOX_FILE_NAME).stat().st_mode) == 0o600
    outbox.close()


def test_events_survive_a_restart_and_leave_oldest_first(
    tmp_path: Path, plane: FakePlane, credential: Path
) -> None:
    state = private_dir(tmp_path)
    outbox = RoomEventOutbox(state)
    ids = []
    for track in range(1, 6):
        event = payload(ephemeral_track_id=track)
        ids.append(event["event_id"])
        assert outbox.enqueue(event)
    outbox.close()
    reopened = RoomEventOutbox(state)
    assert reopened.depth() == 5
    uploader = RoomEventUploader(reopened, client(plane, credential), clock=Clock())
    assert uploader.deliver_once() == "delivered"
    assert [e["event_id"] for e in plane.event_posts()[0]] == ids
    assert reopened.depth() == 0
    reopened.close()


def test_acknowledged_and_duplicate_events_are_removed(
    tmp_path: Path, plane: FakePlane, credential: Path
) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path))
    uploader = RoomEventUploader(outbox, client(plane, credential), clock=Clock())
    first = payload()
    outbox.enqueue(first)
    assert uploader.deliver_once() == "delivered" and outbox.depth() == 0
    outbox.enqueue(first)  # the same event again, e.g. an ACK lost before the delete
    assert uploader.deliver_once() == "delivered" and outbox.depth() == 0
    snapshot = outbox.snapshot()
    assert (
        snapshot["room_transition_events_uploaded_total"],
        snapshot["room_transition_events_duplicate_ack_total"],
    ) == (1, 1)
    assert list(plane.stored) == [first["event_id"]]


@pytest.mark.parametrize("failure", ["down", 500, 503, 429, 404])
def test_retryable_failures_keep_the_same_event_with_bounded_backoff(
    tmp_path: Path, plane: FakePlane, credential: Path, failure: Any
) -> None:
    clock = Clock()
    outbox = RoomEventOutbox(private_dir(tmp_path))
    uploader = RoomEventUploader(
        outbox, client(plane, credential), clock=clock, jitter=lambda d: 0.0
    )
    event = payload()
    outbox.enqueue(event)
    plane.down = failure == "down"
    plane.event_status = failure if isinstance(failure, int) else 200
    delays = []
    for _ in range(12):
        before = clock.now
        assert uploader.deliver_once() == "retry"
        assert uploader.deliver_once() == "idle", "not retried before its backoff"
        clock.now += BACKOFF_MAX_SECONDS
        delays.append(clock.now - before)
    assert outbox.depth() == 1 and outbox.attempts(event["event_id"]) == 12
    assert all(delay <= BACKOFF_MAX_SECONDS for delay in delays)
    plane.down, plane.event_status = False, 200
    assert uploader.deliver_once() == "delivered"
    sent = {e["event_id"] for batch in plane.event_posts() for e in batch}
    assert sent == {event["event_id"]}, "every attempt carried the same event id"
    assert outbox.depth() == 0 and list(plane.stored) == [event["event_id"]]


def test_backoff_is_exponential_capped_and_jitter_bounded() -> None:
    assert [backoff_seconds(n, lambda d: 0.0) for n in range(4)] == [2.0, 4.0, 8.0, 16.0]
    assert backoff_seconds(30, lambda d: 0.0) == BACKOFF_MAX_SECONDS
    assert backoff_seconds(1, lambda d: 1000.0) == 5.0, "jitter is at most a quarter"
    assert backoff_seconds(1, lambda d: -5.0) == 4.0


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_wait_the_maximum_and_never_discard(
    tmp_path: Path, plane: FakePlane, credential: Path, status: int
) -> None:
    clock = Clock()
    outbox = RoomEventOutbox(private_dir(tmp_path))
    uploader = RoomEventUploader(outbox, client(plane, credential), clock=clock)
    outbox.enqueue(payload())
    plane.event_status = status
    assert uploader.deliver_once() == "auth_rejected"
    clock.now += BACKOFF_MAX_SECONDS - 1
    assert uploader.deliver_once() == "idle"
    clock.now += 1
    assert uploader.deliver_once() == "auth_rejected"
    snapshot = outbox.snapshot()
    assert snapshot["room_transition_auth_failures_total"] == 2
    assert (
        snapshot["room_transition_queue_depth"] == 1
        and snapshot["room_transition_events_rejected_total"] == 0
    )


def test_a_missing_credential_file_never_discards(
    tmp_path: Path, plane: FakePlane, credential: Path
) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path))
    uploader = RoomEventUploader(outbox, client(plane, credential), clock=Clock())
    outbox.enqueue(payload())
    credential.unlink()
    assert uploader.deliver_once() == "credential_unavailable"
    assert outbox.depth() == 1 and plane.requests == []


@pytest.mark.parametrize("status", [400, 413, 422])
def test_a_permanently_refused_batch_is_split_and_the_bad_event_dead_lettered(
    tmp_path: Path, plane: FakePlane, credential: Path, status: int
) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path))
    uploader = RoomEventUploader(outbox, client(plane, credential), clock=Clock())
    good, bad = payload(), payload(ephemeral_track_id=8)
    outbox.enqueue(bad)
    outbox.enqueue(good)
    original = plane.respond

    def respond(method: str, path: str, body: bytes | None) -> tuple[int, bytes]:
        ids = [e["event_id"] for e in json.loads(body or b"{}").get("events", [])]
        return (status, b"{}") if bad["event_id"] in ids else original(method, path, body)

    plane.respond = respond  # type: ignore[method-assign]
    outcomes = [uploader.deliver_once() for _ in range(4)]
    assert outcomes == ["split", "rejected", "delivered", "idle"]
    assert outbox.depth() == 0 and list(plane.stored) == [good["event_id"]]
    assert outbox.dead_letters() == [(bad["event_id"], "request_rejected")]
    assert outbox.snapshot()["room_transition_events_rejected_total"] == 1


def test_server_rejections_are_dead_lettered_once_with_a_bounded_category(
    tmp_path: Path, plane: FakePlane, credential: Path
) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path))
    uploader = RoomEventUploader(outbox, client(plane, credential), clock=Clock())
    rejected, weird, accepted = (
        payload(),
        payload(ephemeral_track_id=9),
        payload(ephemeral_track_id=10),
    )
    plane.reject = {rejected["event_id"]: "portal_unavailable", weird["event_id"]: "<script>"}
    for event in (rejected, weird, accepted):
        outbox.enqueue(event)
    assert uploader.deliver_once() == "delivered"
    assert uploader.deliver_once() == "idle", "never retried"
    assert outbox.dead_letters() == [
        (rejected["event_id"], "portal_unavailable"),
        (weird["event_id"], "rejected"),
    ]
    assert len(plane.event_posts()) == 1


def test_the_dead_letter_table_is_bounded(tmp_path: Path) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path), capacity=100)
    for _ in range(DEAD_LETTER_CAPACITY // 100 + 1):
        ids = []
        for _ in range(100):
            event = payload()
            outbox.enqueue(event)
            ids.append(event["event_id"])
        outbox.dead_letter(ids, "request_rejected", 0.0)
    assert outbox.dead_letter_depth() == DEAD_LETTER_CAPACITY
    assert outbox.snapshot()["room_transition_dead_letter_evicted_total"] == 100


def test_capacity_is_enforced_counted_and_never_blocks(tmp_path: Path) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path), capacity=3)
    assert [outbox.enqueue(payload()) for _ in range(5)] == [True, True, True, False, False]
    snapshot = outbox.snapshot()
    assert snapshot["room_transition_queue_depth"] == 3
    assert snapshot["room_transition_queue_capacity"] == 3
    assert snapshot["room_transition_queue_dropped_total"] == 2
    assert snapshot["room_transition_events_generated_total"] == 5
    for bad in (0, 100_001):
        with pytest.raises(ValueError):
            RoomEventOutbox(private_dir(tmp_path, f"x{bad}"), capacity=bad)


@pytest.mark.parametrize(
    "changes",
    [
        {"image": "x"},
        {"frame": "x"},
        {"crop": [1]},
        {"embedding": [0.1]},
        {"credential": TOKEN},
        {"person_id": "p"},
        {"event_type": "PERSON_APPEARED_IN_VIEW"},
        {"crossing_x": 1.5},
        {"ephemeral_track_id": True},
        {"camera_id": "not-a-uuid"},
    ],
)
def test_nothing_but_the_anonymous_fields_can_be_queued(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path))
    assert outbox.enqueue(payload(**changes)) is False
    assert outbox.depth() == 0 and outbox.snapshot()["room_transition_queue_dropped_total"] == 1


def test_the_outbox_schema_and_file_hold_no_image_or_credential(tmp_path: Path) -> None:
    state = private_dir(tmp_path)
    outbox = RoomEventOutbox(state)
    outbox.enqueue(payload())
    outbox.close()
    database = sqlite3.connect(str(state / OUTBOX_FILE_NAME))
    columns = {
        table: {row[1] for row in database.execute(f"PRAGMA table_info({table})")}
        for (table,) in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    stored = json.loads(database.execute("SELECT payload FROM outbox").fetchone()[0])
    database.close()
    assert columns == {
        "outbox": {"seq", "event_id", "payload", "attempts", "next_attempt_at", "solo"},
        "dead_letter": {"seq", "event_id", "category", "payload", "dead_at"},
    }
    assert set(stored) == PAYLOAD_KEYS
    for path in state.iterdir():
        data = path.read_bytes()
        assert TOKEN.encode() not in data and SECRET.encode() not in data and b"Bearer" not in data


def test_the_uploader_thread_is_bounded_and_stops(
    tmp_path: Path, plane: FakePlane, credential: Path
) -> None:
    outbox = RoomEventOutbox(private_dir(tmp_path))
    uploader = RoomEventUploader(outbox, client(plane, credential), poll_seconds=0.01)
    outbox.enqueue(payload())
    uploader.start()
    for _ in range(300):
        if outbox.depth() == 0:
            break
        threading.Event().wait(0.01)
    assert outbox.depth() == 0 and uploader.running
    uploader.stop()
    assert not uploader.running
    outbox.close()
    # Closed: everything is a quiet no-op, never an exception.
    assert outbox.enqueue(payload()) is False and outbox.due(0.0) == [] and outbox.depth() == 0


# ======================================================================= runtime handoff
def test_runtime_transitions_are_handed_to_the_sink_with_the_session_stream_id() -> None:
    handed: list[tuple[RoomTransition, str]] = []
    runtime = runtime_for(
        linear(0.15, 0.85, 30) + linear(0.85, 0.15, 30),
        transition_sink=lambda t, stream: handed.append((t, stream)),
    )
    assert [str(t.kind) for t, _ in handed] == ["PERSON_ENTERED_ROOM", "PERSON_EXITED_ROOM"]
    assert {stream for _, stream in handed} == {runtime.stream_instance_id}
    assert len(runtime.stream_instance_id) == 32


def test_a_failing_sink_never_stops_tracking() -> None:
    def broken(*_: Any) -> None:
        raise OSError("disk full")

    runtime = runtime_for(linear(0.15, 0.85, 30) + linear(0.85, 0.15, 30), transition_sink=broken)
    assert runtime.failure is None
    assert [e["kind"] for e in reversed(runtime.room_transitions()["recent"])] == [
        "PERSON_ENTERED_ROOM",
        "PERSON_EXITED_ROOM",
    ]


# ================================================================== portal plan / the CLI
def plan_options(credential: Path, tmp_path: Path, **changes: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "environment": "local",
        "static_portals": False,
        "managed": True,
        "camera_id": CAMERA,
        "control_plane_url": "https://127.0.0.1:8443",
        "credential_file": str(credential),
        "state_dir": str(tmp_path / "state"),
    }
    options.update(changes)
    return options


def test_portal_source_precedence_is_explicit(credential: Path, tmp_path: Path) -> None:
    for environment in ("local", "development", "test", "ci"):
        static = resolve_portal_plan(
            **plan_options(
                credential, tmp_path, environment=environment, static_portals=True, managed=False
            )
        )
        assert static.mode == "static"
    managed = resolve_portal_plan(**plan_options(credential, tmp_path, environment="test"))
    assert managed.mode == "managed" and managed.managed is not None
    assert managed.managed.camera_id == CAMERA
    for _ in range(3):
        with pytest.raises(PortalPlanError) as refused:
            resolve_portal_plan(**plan_options(credential, tmp_path, static_portals=True))
        assert refused.value.category == "portal_sources_conflict"
    for environment in ("staging", "production", "prod", ""):
        with pytest.raises(PortalPlanError) as refused:
            resolve_portal_plan(
                **plan_options(
                    credential,
                    tmp_path,
                    environment=environment,
                    static_portals=True,
                    managed=False,
                )
            )
        assert refused.value.category == "portal_override_refused_in_this_environment"
    none = resolve_portal_plan(
        **plan_options(credential, tmp_path, environment="production", managed=False)
    )
    assert none.mode == "none"


@pytest.mark.parametrize(
    ("changes", "category"),
    [
        ({"camera_id": None}, "managed_camera_required"),
        ({"camera_id": "front-door"}, "managed_camera_must_be_a_veotrex_camera_uuid"),
        ({"camera_id": CAMERA.upper()}, "managed_camera_must_be_a_veotrex_camera_uuid"),
        ({"control_plane_url": None}, "control_plane_url_not_configured"),
        ({"control_plane_url": "http://127.0.0.1:8443"}, "control_plane_url_invalid"),
        ({"credential_file": None}, "credential_file_not_configured"),
        ({"credential_file": "relative.credential"}, "credential_file_path_not_absolute"),
        ({"state_dir": None}, "state_dir_not_configured"),
        ({"state_dir": "relative/state"}, "state_dir_path_not_absolute"),
        ({"refresh_seconds": 5.0}, "config_refresh_seconds_out_of_range"),
        ({"refresh_seconds": 3600.0}, "config_refresh_seconds_out_of_range"),
        ({"outbox_capacity": 0}, "outbox_capacity_out_of_range"),
    ],
)
def test_invalid_managed_options_are_refused(
    credential: Path, tmp_path: Path, changes: dict[str, Any], category: str
) -> None:
    with pytest.raises(PortalPlanError) as refused:
        resolve_portal_plan(**plan_options(credential, tmp_path, **changes))
    assert refused.value.category == category
    assert TOKEN not in str(refused.value)


def cli_args(*extra: str) -> argparse.Namespace:
    parser = live_cli.add_demo_arguments(argparse.ArgumentParser())
    return parser.parse_args(
        ["--source", "synthetic", "--headless", "--max-frames", "3", "--detector", "none", *extra]
    )


def managed_args(credential: Path, state: Path, *extra: str) -> argparse.Namespace:
    return cli_args(
        "--managed-portals",
        "--managed-camera",
        CAMERA,
        "--control-plane-url",
        "https://127.0.0.1:8443",
        "--credential-file",
        str(credential),
        "--state-dir",
        str(state),
        "--environment",
        "test",
        *extra,
    )


def with_plane(monkeypatch: pytest.MonkeyPatch, plane: FakePlane) -> list[ManagedEdgeSession]:
    opened: list[ManagedEdgeSession] = []
    real = ManagedEdgeSession

    def open_(plan: ManagedPlan, **options: Any) -> ManagedEdgeSession:
        session = real(plan, connection_factory=plane.factory, **options)
        opened.append(session)
        return session

    monkeypatch.setattr(ManagedEdgeSession, "open", staticmethod(open_))
    return opened


@pytest.mark.parametrize(
    "argv",
    [
        ("--environment", "staging", "--portal", "0.5,0.1,0.5,0.9,RIGHT"),
        ("--environment", "production", "--portal", "0.5,0.1,0.5,0.9,RIGHT"),
        ("--portal", "0.5,0.1,0.5,0.9,RIGHT", "--managed-portals"),
        ("--managed-portals",),
        ("--managed-portals", "--managed-camera", "nope"),
    ],
)
def test_refused_portal_options_fail_before_source_or_detector(
    argv: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[str] = []
    monkeypatch.setattr(live_cli, "_source", lambda *_: started.append("source"))
    monkeypatch.setattr(live_cli, "_detector", lambda *_: started.append("detector"))
    assert live_cli.run_demo_cli(cli_args(*argv)) == 2
    assert started == []
    assert "portal source refused" in capsys.readouterr().err


def test_a_non_private_state_dir_fails_before_startup(
    credential: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[str] = []
    monkeypatch.setattr(live_cli, "_source", lambda *_: started.append("source"))
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    assert live_cli.run_demo_cli(managed_args(credential, shared)) == 2
    assert started == [] and "state_directory_not_private" in capsys.readouterr().err


def test_local_explicit_portal_still_runs(capsys: pytest.CaptureFixture[str]) -> None:
    assert live_cli.run_demo_cli(cli_args("--portal", "0.5,0.1,0.5,0.9,RIGHT,door")) == 0
    output = capsys.readouterr().out
    report = json.loads(output[output.index("\n{") :])
    assert (
        report["room_transitions"]["enabled"] == 1 and "managed" not in report["room_transitions"]
    )


def test_managed_mode_runs_fetches_and_stops_its_workers(
    credential: Path,
    tmp_path: Path,
    plane: FakePlane,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened = with_plane(monkeypatch, plane)
    before = {t.name for t in threading.enumerate()}
    assert (
        live_cli.run_demo_cli(managed_args(credential, tmp_path / "state", "--max-frames", "60"))
        == 0
    )
    output = capsys.readouterr().out
    assert "Room transitions from control-plane doorway lines" in output
    report = json.loads(output[output.index("\n{") :])
    status = report["room_transitions"]["managed"]
    assert status["camera_id"] == CAMERA and status["events"]["room_transition_queue_depth"] == 0
    assert status["config"]["config_fetch_success_total"] >= 1
    assert TOKEN not in output and SECRET not in output
    session = opened[0]
    assert not session.threads_running
    assert {t.name for t in threading.enumerate()} - before == set()
    session.close()  # idempotent


def test_an_unreachable_control_plane_never_stops_video(
    credential: Path,
    tmp_path: Path,
    plane: FakePlane,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plane.down = True
    with_plane(monkeypatch, plane)
    assert (
        live_cli.run_demo_cli(managed_args(credential, tmp_path / "state", "--max-frames", "20"))
        == 0
    )
    output = capsys.readouterr().out
    assert "No last-known-good configuration yet" in output
    report = json.loads(output[output.index("\n{") :])
    assert report["failure"] is None
    assert report["room_transitions"]["configured"] == 0
    assert report["room_transitions"]["managed"]["config"]["last_failure_category"] in (
        None,
        "transport_failed",
    )


def test_the_managed_session_hands_events_to_the_outbox_and_uploads_them(
    credential: Path, tmp_path: Path, plane: FakePlane
) -> None:
    plan = resolve_portal_plan(**plan_options(credential, tmp_path, environment="test")).managed
    assert plan is not None
    clock = Clock()
    session = ManagedEdgeSession(plan, connection_factory=plane.factory, clock=clock)
    staged: list[tuple[PortalSet, str | None]] = []
    assert session.submit(transition(), "a" * 32) is True
    session.start(lambda p, r: staged.append((p, r)))
    try:
        for _ in range(300):
            if plane.stored and len(staged) >= 2:
                break
            threading.Event().wait(0.01)
        assert staged[0] == (PortalSet(), None), "no cache yet: nothing staged at start"
        assert [p.label for p in staged[-1][0].portals] == ["Main Door"]
        [stored] = plane.stored.values()
        assert stored["camera_id"] == CAMERA and stored["stream_instance_id"] == "a" * 32
        assert stored["occurred_at"].startswith("2027-01-15T08:00:00")
    finally:
        session.close()
    assert not session.threads_running
    assert session.outbox.enqueue(payload()) is False, "closed on shutdown"


# ================================================================================= metrics
def test_metrics_are_counters_and_depths_without_track_labels(
    credential: Path, tmp_path: Path, plane: FakePlane
) -> None:
    plan = resolve_portal_plan(**plan_options(credential, tmp_path, environment="test")).managed
    assert plan is not None
    session = ManagedEdgeSession(plan, connection_factory=plane.factory, clock=Clock())
    session.config.refresh()
    for track in (101, 202, 303):
        session.submit(transition(track=track), "b" * 32)
    session.uploader.deliver_once()
    snapshot = session.snapshot()
    session.close()
    flat = json.dumps(snapshot)
    values = [*snapshot["config"].values(), *snapshot["events"].values()]
    assert not {101, 202, 303} & {v for v in values if isinstance(v, int)}, "no per-track value"
    keys = [*snapshot["config"], *snapshot["events"]]
    assert not [k for k in keys if "track" in k or "stream" in k], "no per-track label"
    assert "bbbb" not in flat and TOKEN not in flat
    config, events = snapshot["config"], snapshot["events"]
    assert config["config_fetch_success_total"] == 1 and config["config_version"].startswith(
        "sha256:"
    )
    assert config["config_age_seconds"] == 0.0 and config["camera_revision"].startswith("sha256:")
    assert events["room_transition_events_generated_total"] == 3
    assert events["room_transition_events_queued_total"] == 3
    assert events["room_transition_events_uploaded_total"] == 3
    assert events["room_transition_queue_depth"] == 0
    assert events["room_transition_queue_capacity"] == plan.outbox_capacity
    for value in (*config.values(), *events.values()):
        assert value is None or isinstance(value, int | float | str)
    assert all(isinstance(v, int) for k, v in events.items() if k.endswith("_total"))


def test_the_edge_payload_matches_the_control_plane_contract() -> None:
    event = payload()
    assert set(event) == PAYLOAD_KEYS
    assert event["event_type"] in {"PERSON_ENTERED_ROOM", "PERSON_EXITED_ROOM"}
    assert UUID(event["event_id"]).version == 4
    assert event["occurred_at"].endswith("+00:00")
