"""V1-05B synthetic end to end: the real edge managed session against the real API. No Ring.

The edge side is the production code path - ``ManagedEdgeSession`` (client, config cache,
refresher, durable outbox, uploader) and a real ``PortalMonitor`` producing the transition the
runtime would hand to its sink. Its HTTP connection factory is bridged into the API's ASGI app
running on this test's event loop, so each request crosses the same machine authentication,
body limits, validation and PostgreSQL row-level security as a deployed node's would. The
edge's blocking calls run in a worker thread; ``outage`` makes the bridge refuse connections.

A config / B version update / C event -> one row / D duplicate replay / E outage then delivery
across a restart / F node A cannot report camera B / G the operator timeline is anonymous.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from test_camera_portals_api import url
from test_classroom_api import stack  # noqa: F401 - fixture
from test_edge_runtime_api import rows, world

from veotrex_api.edge_runtime import configuration_revision as api_revision
from veotrex_edge_agent.edge_control_plane import RUNTIME_CONFIG_PATH, ControlPlaneEndpoint
from veotrex_edge_agent.live.managed_config import configuration_revision as edge_revision
from veotrex_edge_agent.live.managed_session import ManagedEdgeSession, ManagedPlan
from veotrex_edge_agent.live.portal_crossing import PortalMonitor, RoomTransition
from veotrex_edge_agent.live.portal_geometry import PortalSet
from veotrex_edge_agent.live.room_event_outbox import event_payload

WIDTH, HEIGHT = 320, 240


@dataclass
class Bridge:
    """``http.client``-shaped connections that run each request on the API app's loop."""

    client: Any
    loop: asyncio.AbstractEventLoop
    outage: bool = False
    requests: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, host: str, port: int, timeout: float) -> _Connection:
        return _Connection(self)


class _Response:
    def __init__(self, status: int, headers: Any, body: bytes) -> None:
        self.status, self._headers, self._body = status, headers, body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name, default)  # type: ignore[no-any-return]

    def read(self, amount: int = -1) -> bytes:
        chunk = self._body if amount < 0 else self._body[:amount]
        self._body = self._body[len(chunk) :]
        return chunk


class _Connection:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self._request: tuple[str, str, bytes | None, dict[str, str]] | None = None

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: Any = None
    ) -> None:
        if self.bridge.outage:
            raise ConnectionRefusedError("synthetic control-plane outage")
        self.bridge.requests.append((method, path))
        self._request = (method, path, body, dict(headers or {}))

    def getresponse(self) -> _Response:
        assert self._request is not None
        method, path, body, headers = self._request
        headers.pop("Host", None)
        future = asyncio.run_coroutine_threadsafe(
            self.bridge.client.request(method, path, content=body, headers=headers),
            self.bridge.loop,
        )
        response = future.result(timeout=30)
        return _Response(response.status_code, response.headers, response.content)

    def close(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


def box(x: float) -> tuple[float, float, float, float]:
    cx, bottom = x * WIDTH, 0.8 * HEIGHT
    return (cx - 20, bottom - 100, cx + 20, bottom)


def walk_in(monitor: PortalMonitor, track: int) -> list[RoomTransition]:
    """A validated anonymous track walking left-to-right across a vertical doorway line."""
    emitted: list[RoomTransition] = []
    for i in range(30):
        x = 0.15 + 0.7 * i / 29
        emitted += monitor.observe(
            track, box(x), width=WIDTH, height=HEIGHT, timestamp_ms=i * 180.0, eligible=True
        )
    return emitted


def credential_file(tmp_path: Path, token: str) -> Path:
    path = tmp_path / "edge.credential"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    return path


async def test_synthetic_end_to_end(stack: dict[str, Any], tmp_path: Path) -> None:  # noqa: F811
    evidence: list[str] = []
    w = await world(stack)
    bridge = Bridge(stack["client"], asyncio.get_running_loop())
    state = tmp_path / "state"
    plan = ManagedPlan(
        camera_id=w["camera_a"],
        endpoint=ControlPlaneEndpoint.parse("https://127.0.0.1:8443", environment="test"),
        credential_file=str(credential_file(tmp_path, w["token_a"].get_secret_value())),
        state_dir=state,
    )
    clock = Clock()

    def open_session() -> ManagedEdgeSession:
        return ManagedEdgeSession(plan, connection_factory=bridge, clock=clock)

    async def edge(call: Any, *args: Any) -> Any:
        return await asyncio.to_thread(call, *args)

    session = open_session()
    monitor = PortalMonitor(PortalSet(), stream_id=w["camera_a"])
    stream = "0123456789abcdef0123456789abcdef"

    # A. CONFIG ------------------------------------------------------------------------------
    assert await edge(session.config.refresh) is True
    # The same document the refresh just validated and applied, as the node received it.
    document = await edge(session.client.get_json, RUNTIME_CONFIG_PATH)
    assert session.snapshot()["config"]["config_version"] == document["config_version"]
    cameras = {c["camera_id"]: c for c in document["cameras"]}
    assert list(cameras) == [w["camera_a"]] and w["camera_b"] not in cameras
    [portal] = cameras[w["camera_a"]]["portals"]
    assert portal["portal_id"] == w["portal_a"] and portal["label"] == "Main Door"
    body = str(document).lower()
    assert not any(word in body for word in ("provider", "ring", "token", "staff", "child"))
    # The edge recomputes exactly the control plane's revision.
    camera = cameras[w["camera_a"]]
    revision_1 = camera["configuration_revision"]
    stripped = {k: v for k, v in camera.items() if k != "configuration_revision"}
    assert edge_revision(stripped) == api_revision(stripped) == revision_1
    monitor.stage(*session.current())
    assert monitor.apply_staged() is True
    evidence.append(
        f"A CONFIG camera_a=present portal_a=present camera_b=absent revision={revision_1[:19]}"
    )

    # B. VERSION UPDATE ----------------------------------------------------------------------
    edited = await stack["client"].patch(
        url(w["room_a"], w["camera_a"], f"/{w['portal_a']}"),
        json={"x1": 0.52, "x2": 0.52},
        headers=stack["admin-1"],
    )
    assert edited.status_code == 200
    assert await edge(session.config.refresh) is True
    portals, revision_2 = session.current()
    assert revision_2 != revision_1 and portals.portals[0].x1 == 0.52
    monitor.stage(portals, revision_2)
    assert monitor.apply_staged() is True and monitor.revision == revision_2
    evidence.append(f"B VERSION revision_changed=True applied={monitor.revision == revision_2}")

    # C. ROOM EVENT: runtime handoff -> outbox -> uploader -> machine route -> one row ---------
    [entered] = walk_in(monitor, track=11)
    assert str(entered.kind) == "PERSON_ENTERED_ROOM"
    assert session.submit(entered, stream) is True  # exactly what the runtime's sink does
    [(event_id, queued)] = session.outbox.due(clock.now)
    assert await edge(session.uploader.deliver_once) == "delivered"
    stored = await rows(stack)
    assert [str(r.id) for r in stored] == [event_id] and stored[0].event_type == "ENTERED"
    assert session.outbox.depth() == 0
    evidence.append(f"C EVENT rows={len(stored)} type={stored[0].event_type} queue_depth=0")

    # D. DUPLICATE: the same event id replayed --------------------------------------------------
    assert session.outbox.enqueue(queued) is True
    assert await edge(session.uploader.deliver_once) == "delivered"
    assert len(await rows(stack)) == 1
    duplicates = session.outbox.snapshot()["room_transition_events_duplicate_ack_total"]
    assert duplicates == 1
    evidence.append(f"D DUPLICATE rows=1 duplicate_ack_total={duplicates}")

    # E. OUTAGE: durable across an outage and a restart, then the same id is delivered ---------
    bridge.outage = True
    second = event_payload(
        entered, camera_id=w["camera_a"], stream_instance_id=stream, occurred_at_unix=clock.now
    )
    assert session.outbox.enqueue(second) is True
    assert await edge(session.uploader.deliver_once) == "retry"
    assert session.outbox.depth() == 1 and len(await rows(stack)) == 1
    session.close()  # a restart while the control plane is still down
    session = open_session()
    assert session.config.start_from_cache() is True, "last-known-good survives the restart"
    assert session.current()[1] == revision_2
    assert session.outbox.due(clock.now + 3600)[0][0] == second["event_id"]
    bridge.outage = False
    clock.now += 3600
    assert await edge(session.uploader.deliver_once) == "delivered"
    ids = {str(r.id) for r in await rows(stack)}
    assert ids == {event_id, second["event_id"]} and session.outbox.depth() == 0
    evidence.append("E OUTAGE queued_through_restart=1 delivered_same_id=True queue_depth=0")

    # F. AUTHZ: node A reports camera B -> refused, dead-lettered, no row ----------------------
    foreign = {**second, "event_id": "8d0e7b8a-2a4b-4c55-9e8b-6a1f2e3d4c5b"}
    foreign.update(camera_id=w["camera_b"], portal_id=w["portal_b"])
    assert session.outbox.enqueue(foreign) is True
    assert await edge(session.uploader.deliver_once) == "delivered"
    assert session.outbox.dead_letters() == [(foreign["event_id"], "camera_unavailable")]
    assert len(await rows(stack)) == 2
    all_ids = {str(r.id) for r in await rows(stack)}
    assert foreign["event_id"] not in all_ids
    evidence.append("F AUTHZ camera_b_event=REJECTED(camera_unavailable) db_rows_added=0")

    # G. OPERATOR TIMELINE ----------------------------------------------------------------------
    timeline = await stack["client"].get(
        f"/v1/classrooms/{w['room_a']}/room-transitions", headers=stack["viewer-1"]
    )
    assert timeline.status_code == 200
    sentences = [
        f"Person {'entered' if e['event_type'] == 'ENTERED' else 'exited'} via {e['portal_label']}"
        for e in timeline.json()["events"]
    ]
    assert sentences == ["Person entered via Main Door", "Person entered via Main Door"]
    lowered = timeline.text.lower()
    assert not any(word in lowered for word in ("track", "stream", "staff", "child", "teacher"))
    evidence.append(f"G TIMELINE {sentences}")

    session.close()
    assert not session.threads_running
    print("\n".join(["V1-05B SYNTHETIC E2E", *evidence]))
