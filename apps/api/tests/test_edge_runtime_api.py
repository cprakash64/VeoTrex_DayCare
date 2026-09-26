"""V1-05B: edge runtime config distribution and anonymous room-transition ingest, over HTTP.

Reuses the V1-04A two-tenant stack and the V1-DEMO-03B edge fixtures. Every camera, node,
credential and portal is synthetic; there is no network, no image and no person. Proves that the
two machine routes answer only for the authenticated node, that the configuration revision is a
deterministic SHA-256 of exactly what the edge applies, that events are idempotent per event id
and that tenant, facility and classroom always come from server-side rows. Also covers the
operator read route (bounded, classroom-scoped, anonymous) and the table's own invariants.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from structlog.testing import capture_logs
from test_camera_portals_api import add_camera, create, url
from test_classroom_api import create_classroom, stack  # noqa: F401 - fixture
from tests_edge_fixtures import add_credential, add_node, set_tenant

from veotrex_api.edge_runtime import (
    camera_document,
    canonical_json,
    configuration_revision,
    snapshot_version,
)
from veotrex_api.models import CameraAssignment, EdgeNode, RoomTransitionEvent, Zone

CONFIG = "/v1/edge/runtime-config"
EVENTS = "/v1/edge/events/room-transitions"


def bearer(token: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


async def assign(
    stack: dict[str, Any],  # noqa: F811
    camera: str,
    node: UUID,
    tenant: str = "tenant_a",
) -> UUID:
    assignment_id = uuid4()
    async with stack["admin"]() as session, session.begin():
        await set_tenant(session, stack[tenant])
        session.add(
            CameraAssignment(
                id=assignment_id,
                tenant_id=stack[tenant],
                camera_id=UUID(camera),
                edge_node_id=node,
                assigned_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
    return assignment_id


async def world(stack: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    """Node A with Camera A (Classroom A) and a Main Door portal; Node B with Camera B in the
    same tenant; Node X with Camera X in tenant B."""
    a = stack["admin"]
    room_a = str((await create_classroom(stack, name=f"Room A {uuid4().hex[:6]}"))["classroom_id"])
    camera_a = await add_camera(stack, room_a)
    node_a = await add_node(a, stack["tenant_a"], stack["f1"])
    _, token_a = await add_credential(a, stack["tenant_a"], node_a)
    assignment_a = await assign(stack, camera_a, node_a)
    created = await create(stack, room_a, camera_a, label="Main Door")
    assert created.status_code == 201, created.text
    portal_a = created.json()["portals"][0]["portal_id"]

    room_b = str((await create_classroom(stack, name=f"Room B {uuid4().hex[:6]}"))["classroom_id"])
    camera_b = await add_camera(stack, room_b)
    node_b = await add_node(a, stack["tenant_a"], stack["f1"])
    _, token_b = await add_credential(a, stack["tenant_a"], node_b)
    await assign(stack, camera_b, node_b)
    portal_b = (await create(stack, room_b, camera_b, label="Side Door")).json()["portals"][0][
        "portal_id"
    ]

    room_x = str(
        (await create_classroom(stack, "fb", as_="owner-b", name="Room X"))["classroom_id"]
    )
    camera_x = await add_camera(stack, room_x, tenant="tenant_b")
    node_x = await add_node(a, stack["tenant_b"], stack["fb"])
    _, token_x = await add_credential(a, stack["tenant_b"], node_x)
    await assign(stack, camera_x, node_x, tenant="tenant_b")
    portal_x = (await create(stack, room_x, camera_x, as_="owner-b", label="X Door")).json()[
        "portals"
    ][0]["portal_id"]
    return {
        "room_a": room_a,
        "camera_a": camera_a,
        "node_a": node_a,
        "token_a": token_a,
        "assignment_a": assignment_a,
        "portal_a": portal_a,
        "room_b": room_b,
        "camera_b": camera_b,
        "node_b": node_b,
        "token_b": token_b,
        "portal_b": portal_b,
        "room_x": room_x,
        "camera_x": camera_x,
        "token_x": token_x,
        "portal_x": portal_x,
    }


def event(w: dict[str, Any], **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": str(uuid4()),
        "camera_id": w["camera_a"],
        "portal_id": w["portal_a"],
        "event_type": "PERSON_ENTERED_ROOM",
        "occurred_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat(),
        "ephemeral_track_id": 7,
        "stream_instance_id": uuid4().hex,
        "crossing_x": 0.5,
        "crossing_y": 0.6,
        "evidence_observations": 3,
    }
    body.update(changes)
    return body


async def post_events(
    stack: dict[str, Any],  # noqa: F811
    token: Any,
    *events: dict[str, Any],
) -> Any:
    return await stack["client"].post(EVENTS, json={"events": list(events)}, headers=bearer(token))


async def rows(stack: dict[str, Any], tenant: str = "tenant_a") -> list[RoomTransitionEvent]:  # noqa: F811
    async with stack["admin"]() as session, session.begin():
        await set_tenant(session, stack[tenant])
        # The admin identity bypasses RLS, so the tenant is filtered explicitly.
        return list(
            (
                await session.scalars(
                    select(RoomTransitionEvent).where(
                        RoomTransitionEvent.tenant_id == stack[tenant]
                    )
                )
            ).all()
        )


async def config(stack: dict[str, Any], token: Any) -> dict[str, Any]:  # noqa: F811
    response = await stack["client"].get(CONFIG, headers=bearer(token))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()  # type: ignore[no-any-return]


# ====================================================================== machine authorization
async def test_a_node_receives_only_its_own_assigned_camera(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    document = await config(stack, w["token_a"])
    assert set(document) == {"schema_version", "edge_node_id", "config_version", "cameras"}
    assert document["edge_node_id"] == str(w["node_a"])
    assert [c["camera_id"] for c in document["cameras"]] == [w["camera_a"]]
    camera = document["cameras"][0]
    assert set(camera) == {"camera_id", "assignment_id", "portals", "configuration_revision"}
    assert camera["assignment_id"] == str(w["assignment_a"])
    assert [p["portal_id"] for p in camera["portals"]] == [w["portal_a"]]
    # Node B sees only Camera B; the tenant-B node sees only its own.
    other = await config(stack, w["token_b"])
    assert [c["camera_id"] for c in other["cameras"]] == [w["camera_b"]]
    foreign = await config(stack, w["token_x"])
    assert [c["camera_id"] for c in foreign["cameras"]] == [w["camera_x"]]


async def test_no_caller_supplied_node_or_tenant_has_any_authority(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    baseline = await config(stack, w["token_a"])
    for query in (
        f"?edge_node_id={w['node_b']}",
        f"?node_id={w['node_b']}",
        f"?tenant_id={stack['tenant_b']}",
        f"?camera_id={w['camera_b']}",
    ):
        response = await stack["client"].get(CONFIG + query, headers=bearer(w["token_a"]))
        assert response.json() == baseline, query
    response = await stack["client"].get(
        CONFIG,
        headers={**bearer(w["token_a"]), "X-Edge-Node-Id": str(w["node_b"])},
    )
    assert response.json() == baseline


async def test_unauthenticated_and_human_callers_cannot_use_machine_routes(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    w = await world(stack)
    for headers in ({}, stack["owner-a"], {"Authorization": "Bearer vte1.nope"}):
        assert (await stack["client"].get(CONFIG, headers=headers)).status_code == 401
        refused = await stack["client"].post(EVENTS, json={"events": [event(w)]}, headers=headers)
        assert refused.status_code == 401
    assert await rows(stack) == []


@pytest.mark.parametrize(
    "path",
    [
        "/v1/facilities/{f1}/children",
        "/v1/staff",
        "/v1/facilities/{f1}/guardians",
        "/v1/classrooms/{room_a}/attendance",
        "/v1/classrooms/{room_a}/room-transitions",
        "/v1/classrooms/{room_a}/cameras/{camera_a}/portals",
    ],
)
async def test_the_edge_credential_cannot_read_any_human_route(
    stack: dict[str, Any],  # noqa: F811
    path: str,
) -> None:
    w = await world(stack)
    target = path.format(f1=stack["f1"], **w)
    response = await stack["client"].get(target, headers=bearer(w["token_a"]))
    assert response.status_code == 401, (target, response.status_code)


async def test_runtime_config_carries_no_provider_secret_or_identity(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    response = await stack["client"].get(CONFIG, headers=bearer(w["token_a"]))
    body = response.text
    async with stack["admin"]() as session, session.begin():
        await set_tenant(session, stack["tenant_a"])
        provider_ids = (
            await session.execute(
                text("SELECT provider_device_id, provider_connection_id FROM cameras")
            )
        ).all()
    for device_id, connection_id in provider_ids:
        assert device_id not in body and str(connection_id) not in body
    lowered = body.lower()
    for forbidden in (
        "provider",
        "ring",
        "token",
        "secret",
        "account",
        "synthetic indoor",  # the camera's display name
        "room a",  # the classroom's name
        "staff",
        "child",
        "guardian",
        "attendance",
        "face",
        "template",
        "embedding",
        "policy",
        "tenant",
        "facility",
        "email",
    ):
        assert forbidden not in lowered, forbidden
    portal = response.json()["cameras"][0]["portals"][0]
    assert set(portal) == {
        "portal_id",
        "label",
        "x1",
        "y1",
        "x2",
        "y2",
        "inside",
        "enabled",
        "deadband",
        "revision",
    }


async def test_ended_assignment_disabled_node_and_other_facility_camera_get_nothing(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    w = await world(stack)
    # A camera assigned to Node A whose classroom is in the node's OTHER facility: its place in
    # the config stays, but no portal is sent and no event is accepted.
    room_f2 = str(
        (await create_classroom(stack, "f2", as_="owner-a", name="Far room"))["classroom_id"]
    )
    camera_f2 = await add_camera(stack, room_f2)
    await assign(stack, camera_f2, w["node_a"])
    portal_f2 = (await create(stack, room_f2, camera_f2, as_="owner-a")).json()["portals"][0][
        "portal_id"
    ]
    cameras = {c["camera_id"]: c for c in (await config(stack, w["token_a"]))["cameras"]}
    assert cameras[camera_f2]["portals"] == []
    refused = await post_events(
        stack, w["token_a"], event(w, camera_id=camera_f2, portal_id=portal_f2)
    )
    assert refused.json()["results"][0]["category"] == "camera_unavailable"

    async with stack["admin"]() as session, session.begin():
        await set_tenant(session, stack["tenant_a"])
        await session.execute(
            update(CameraAssignment)
            .where(CameraAssignment.id == w["assignment_a"])
            .values(ended_at=datetime.now(UTC))
        )
    assert w["camera_a"] not in {
        c["camera_id"] for c in (await config(stack, w["token_a"]))["cameras"]
    }
    ended = await post_events(stack, w["token_a"], event(w))
    assert ended.json()["results"][0] == {
        "event_id": ended.json()["results"][0]["event_id"],
        "status": "REJECTED",
        "category": "camera_unavailable",
    }
    async with stack["admin"]() as session, session.begin():
        await set_tenant(session, stack["tenant_a"])
        await session.execute(
            update(EdgeNode).where(EdgeNode.id == w["node_b"]).values(status="DISABLED")
        )
    assert (await stack["client"].get(CONFIG, headers=bearer(w["token_b"]))).status_code == 401
    assert await rows(stack) == []


# ============================================================================ event ingest
async def test_entered_and_exited_events_are_persisted_with_server_derived_scope(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    w = await world(stack)
    entered = event(w)
    exited = event(w, event_type="PERSON_EXITED_ROOM", ephemeral_track_id=8, crossing_x=0.45)
    response = await post_events(stack, w["token_a"], entered, exited)
    assert response.status_code == 200, response.text
    assert [r["status"] for r in response.json()["results"]] == ["ACCEPTED", "ACCEPTED"]
    stored = {str(row.id): row for row in await rows(stack)}
    assert set(stored) == {entered["event_id"], exited["event_id"]}
    row = stored[entered["event_id"]]
    assert (row.tenant_id, row.facility_id, row.area_id) == (
        stack["tenant_a"],
        stack["f1"],
        UUID(w["room_a"]),
    )
    assert (str(row.camera_id), str(row.portal_id), row.edge_node_id) == (
        w["camera_a"],
        w["portal_a"],
        w["node_a"],
    )
    assert (row.event_type, stored[exited["event_id"]].event_type) == ("ENTERED", "EXITED")
    assert (row.ephemeral_track_id, row.evidence_observations) == (7, 3)
    assert row.received_at >= row.occurred_at


async def test_a_retried_event_is_one_row_and_acknowledged_as_duplicate(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    w = await world(stack)
    once = event(w)
    first = await post_events(stack, w["token_a"], once)
    second = await post_events(stack, w["token_a"], once)
    both = await post_events(stack, w["token_a"], once, once)
    assert first.json()["results"][0]["status"] == "ACCEPTED"
    assert second.json()["results"][0]["status"] == "DUPLICATE"
    assert [r["status"] for r in both.json()["results"]] == ["DUPLICATE", "DUPLICATE"]
    assert [str(row.id) for row in await rows(stack)] == [once["event_id"]]
    # The same id with different content is not a retry.
    changed = await post_events(stack, w["token_a"], {**once, "ephemeral_track_id": 99})
    assert changed.json()["results"][0]["category"] == "event_id_conflict"
    assert len(await rows(stack)) == 1


async def test_camera_portal_and_tenant_relationships_are_checked_server_side(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    w = await world(stack)
    cases = {
        "camera_unavailable": [
            event(w, camera_id=w["camera_b"], portal_id=w["portal_b"]),  # Node B's camera
            event(w, camera_id=w["camera_x"], portal_id=w["portal_x"]),  # another tenant's
            event(w, camera_id=str(uuid4())),  # does not exist
        ],
        "portal_unavailable": [
            event(w, portal_id=w["portal_b"]),  # a portal of another camera
            event(w, portal_id=w["portal_x"]),  # another tenant's portal
            event(w, portal_id=str(uuid4())),
        ],
    }
    for expected, items in cases.items():
        response = await post_events(stack, w["token_a"], *items)
        assert {r["category"] for r in response.json()["results"]} == {expected}
        assert {r["status"] for r in response.json()["results"]} == {"REJECTED"}
    # Node B cannot report Camera A either.
    response = await post_events(stack, w["token_b"], event(w))
    assert response.json()["results"][0]["category"] == "camera_unavailable"
    assert await rows(stack) == [] and await rows(stack, "tenant_b") == []


async def test_tenant_facility_and_classroom_can_never_be_supplied(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    for extra in (
        {"tenant_id": str(stack["tenant_b"])},
        {"facility_id": str(stack["fb"])},
        {"classroom_id": w["room_x"]},
        {"area_id": w["room_x"]},
        {"edge_node_id": str(w["node_b"])},
        {"person_id": "p"},
        {"image": "x"},
    ):
        response = await post_events(stack, w["token_a"], event(w, **extra))
        assert response.status_code == 422, extra
    assert await rows(stack) == []


@pytest.mark.parametrize(
    ("changes", "category"),
    [
        ({"occurred_at": "FUTURE"}, "occurred_at_out_of_window"),
        ({"occurred_at": "STALE"}, "occurred_at_out_of_window"),
    ],
)
async def test_occurred_at_is_bounded_against_receipt_time(
    stack: dict[str, Any],  # noqa: F811
    changes: dict[str, Any],
    category: str,
) -> None:
    w = await world(stack)
    now = datetime.now(UTC)
    moment = (
        now + timedelta(minutes=5)
        if changes["occurred_at"] == "FUTURE"
        else now - timedelta(days=8)
    )
    response = await post_events(stack, w["token_a"], event(w, occurred_at=moment.isoformat()))
    assert response.json()["results"][0]["category"] == category
    assert await rows(stack) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"crossing_x": 1.5},
        {"crossing_y": -0.1},
        {"crossing_x": "0.5"},
        {"evidence_observations": 0},
        {"evidence_observations": 101},
        {"ephemeral_track_id": 0},
        {"ephemeral_track_id": True},
        {"ephemeral_track_id": 2**31},
        {"stream_instance_id": "Has Spaces"},
        {"stream_instance_id": "x" * 65},
        {"event_type": "PERSON_APPEARED_IN_VIEW"},
        {"event_type": "ENTERED"},
        {"occurred_at": "2026-09-26T10:00:00"},  # naive
        {"event_id": "not-a-uuid"},
    ],
)
async def test_malformed_events_are_refused_whole(
    stack: dict[str, Any],  # noqa: F811
    changes: dict[str, Any],
) -> None:
    w = await world(stack)
    response = await post_events(stack, w["token_a"], event(w), event(w, **changes))
    assert response.status_code == 422, changes
    assert await rows(stack) == []


async def test_non_finite_crossing_points_are_refused(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    for token in ("NaN", "Infinity", "-Infinity"):
        raw = json.dumps({"events": [event(w, crossing_x="__X__")]}).replace('"__X__"', token)
        response = await stack["client"].post(
            EVENTS,
            content=raw,
            headers={**bearer(w["token_a"]), "Content-Type": "application/json"},
        )
        assert response.status_code == 422, token
    assert await rows(stack) == []


async def test_batches_and_bodies_are_bounded(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    too_many = await post_events(stack, w["token_a"], *[event(w) for _ in range(101)])
    assert too_many.status_code == 422
    empty = await stack["client"].post(EVENTS, json={"events": []}, headers=bearer(w["token_a"]))
    assert empty.status_code == 422
    # Oversized: refused before authentication, so even a bad credential gets 413.
    padding = {"events": [event(w)], "pad": "x" * 70_000}
    for headers in (bearer(w["token_a"]), {"Authorization": "Bearer nope"}):
        oversized = await stack["client"].post(EVENTS, json=padding, headers=headers)
        assert oversized.status_code == 413
    wrong_type = await stack["client"].post(
        EVENTS, content=b"events=1", headers={**bearer(w["token_a"]), "Content-Type": "text/plain"}
    )
    assert wrong_type.status_code == 415
    assert await rows(stack) == []


async def test_the_credential_never_reaches_a_log_or_an_error(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    token = w["token_a"].get_secret_value()
    secret = token.rsplit(".", 1)[1]
    with capture_logs() as logs:
        responses = [
            await stack["client"].get(CONFIG, headers=bearer(w["token_a"])),
            await post_events(stack, w["token_a"], event(w)),
            await post_events(stack, w["token_a"], event(w, crossing_x=2.0)),
            await post_events(stack, w["token_a"], event(w, camera_id=w["camera_b"])),
            await stack["client"].get(CONFIG, headers={"Authorization": f"Bearer {token}x"}),
            await stack["client"].get(CONFIG, headers={"Authorization": f"Bearer {token[:-1]}A"}),
        ]
    everything = json.dumps(logs, default=str) + "".join(r.text for r in responses)
    assert secret not in everything and token not in everything


# ======================================================================== config versioning
def test_revision_is_deterministic_and_independent_of_portal_order() -> None:
    portals = [
        {
            "portal_id": str(uuid4()),
            "label": f"Door {i}",
            "x1": 0.1 * i,
            "y1": 0.1,
            "x2": 0.1 * i,
            "y2": 0.9,
            "inside": "RIGHT",
            "enabled": True,
            "deadband": 0.02,
            "revision": 1,
        }
        for i in range(1, 5)
    ]
    camera, assignment = uuid4(), uuid4()
    reference = camera_document(camera, assignment, portals)
    revision = configuration_revision(reference)
    for _ in range(10):
        shuffled = random.sample(portals, len(portals))
        assert configuration_revision(camera_document(camera, assignment, shuffled)) == revision
    assert revision.startswith("sha256:") and len(revision) == 7 + 64
    # Canonical form: sorted keys, no whitespace, ASCII only, no NaN.
    assert canonical_json({"b": 1, "a": [1.0, "é"]}) == b'{"a":[1.0,"\\u00e9"],"b":1}'
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    cameras = [
        {"camera_id": str(uuid4()), "configuration_revision": revision},
        {"camera_id": str(uuid4()), "configuration_revision": revision},
    ]
    assert snapshot_version(cameras) == snapshot_version(list(reversed(cameras)))


async def test_every_runtime_significant_edit_changes_the_revision(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)

    async def revision() -> tuple[str, str]:
        document = await config(stack, w["token_a"])
        return document["cameras"][0]["configuration_revision"], document["config_version"]

    seen = [await revision()]
    assert await revision() == seen[0], "an unchanged configuration keeps its revision"
    portal = url(w["room_a"], w["camera_a"], f"/{w['portal_a']}")
    for change in ({"x1": 0.55, "x2": 0.55}, {"label": "Front Door"}, {"enabled": False}):
        response = await stack["client"].patch(portal, json=change, headers=stack["admin-1"])
        assert response.status_code == 200, response.text
        seen.append(await revision())
    archived = await stack["client"].post(f"{portal}/archive", headers=stack["admin-1"])
    assert archived.status_code == 200
    seen.append(await revision())
    assert (await config(stack, w["token_a"]))["cameras"][0]["portals"] == []
    assert len({camera for camera, _ in seen}) == len(seen), "every edit is a new revision"
    assert len({version for _, version in seen}) == len(seen)


async def test_a_portal_left_in_the_previous_classroom_is_not_sent(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    async with stack["admin"]() as session, session.begin():
        await set_tenant(session, stack["tenant_a"])
        zone = await session.scalar(select(Zone.id).where(Zone.area_id == UUID(w["room_b"])))
        await session.execute(
            text("UPDATE cameras SET zone_id = :zone WHERE id = :camera"),
            {"zone": zone, "camera": w["camera_a"]},
        )
    assert (await config(stack, w["token_a"]))["cameras"][0]["portals"] == []
    refused = await post_events(stack, w["token_a"], event(w))
    assert refused.json()["results"][0]["category"] == "portal_unavailable"


# =============================================================================== read API
async def seed_events(stack: dict[str, Any], w: dict[str, Any], count: int) -> list[str]:  # noqa: F811
    now = datetime.now(UTC)
    items = [
        event(
            w,
            occurred_at=(now - timedelta(minutes=count - i)).isoformat(),
            event_type="PERSON_ENTERED_ROOM" if i % 2 == 0 else "PERSON_EXITED_ROOM",
        )
        for i in range(count)
    ]
    response = await post_events(stack, w["token_a"], *items)
    assert {r["status"] for r in response.json()["results"]} == {"ACCEPTED"}
    return [item["event_id"] for item in reversed(items)]  # newest first


async def test_an_operator_reads_the_classroom_timeline_anonymously(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    newest_first = await seed_events(stack, w, 3)
    response = await stack["client"].get(
        f"/v1/classrooms/{w['room_a']}/room-transitions", headers=stack["viewer-1"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["event_id"] for e in body["events"]] == newest_first
    assert body["next_cursor"] is None
    item = body["events"][0]
    assert set(item) == {
        "event_id",
        "event_type",
        "occurred_at",
        "received_at",
        "camera_id",
        "camera_name",
        "portal_id",
        "portal_label",
    }
    assert (item["portal_label"], item["camera_id"]) == ("Main Door", w["camera_a"])
    lowered = response.text.lower()
    for forbidden in ("track", "stream", "person", "staff", "child", "guardian", "teacher"):
        assert forbidden not in lowered, forbidden


async def test_other_tenants_and_other_facilities_get_a_uniform_404(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    await seed_events(stack, w, 1)
    path = f"/v1/classrooms/{w['room_a']}/room-transitions"
    missing = await stack["client"].get(
        f"/v1/classrooms/{uuid4()}/room-transitions", headers=stack["owner-b"]
    )
    for caller in ("owner-b", "viewer-2"):
        response = await stack["client"].get(path, headers=stack[caller])
        assert (response.status_code, response.json()) == (404, missing.json()), caller


async def test_pages_are_bounded_keyset_and_filterable(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    newest_first = await seed_events(stack, w, 5)
    path = f"/v1/classrooms/{w['room_a']}/room-transitions"
    collected: list[str] = []
    cursor = None
    for _ in range(5):
        query = "?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = (await stack["client"].get(path + query, headers=stack["viewer-1"])).json()
        assert len(page["events"]) <= 2
        collected += [e["event_id"] for e in page["events"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert collected == newest_first
    entered = (
        await stack["client"].get(path + "?event_type=ENTERED", headers=stack["viewer-1"])
    ).json()
    assert {e["event_type"] for e in entered["events"]} == {"ENTERED"}
    assert len(entered["events"]) == 3
    by_portal = (
        await stack["client"].get(path + f"?portal_id={w['portal_b']}", headers=stack["viewer-1"])
    ).json()
    assert by_portal["events"] == []
    for bad in (
        "?limit=0",
        "?limit=201",
        "?event_type=LOITERED",
        "?cursor=not*valid",
        "?cursor=" + "A" * 129,
        "?camera_id=nope",
    ):
        response = await stack["client"].get(path + bad, headers=stack["viewer-1"])
        assert response.status_code == 422, bad
    garbage = await stack["client"].get(path + "?cursor=Zm9v", headers=stack["viewer-1"])
    assert garbage.status_code == 422
    assert garbage.json()["detail"]["category"] == "invalid_cursor"


# ============================================================================= database
async def test_room_transition_events_are_append_only_with_forced_rls(
    stack: dict[str, Any],  # noqa: F811
    runtime_role_name: str,
) -> None:
    async with stack["admin"]() as session, session.begin():
        privileges = {
            privilege: await session.scalar(
                text("SELECT has_table_privilege(:role, 'public.room_transition_events', :p)"),
                {"role": runtime_role_name, "p": privilege},
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
        rls = (
            await session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'room_transition_events'"
                )
            )
        ).one()
        public = await session.scalar(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE table_name = 'room_transition_events' AND grantee = 'PUBLIC'"
            )
        )
        columns = set(
            (
                await session.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'room_transition_events'"
                    )
                )
            ).all()
        )
    assert privileges == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }
    assert tuple(rls) == (True, True) and public == 0
    for forbidden in ("image", "frame", "crop", "face", "embedding", "name", "person", "staff"):
        assert not any(forbidden in column for column in columns), forbidden


async def test_the_runtime_role_cannot_change_or_remove_an_event(stack: dict[str, Any]) -> None:  # noqa: F811
    w = await world(stack)
    await seed_events(stack, w, 1)
    async with stack["runtime"]() as session, session.begin():
        await set_tenant(session, stack["tenant_a"])
        for statement in (
            "UPDATE room_transition_events SET event_type = 'EXITED'",
            "DELETE FROM room_transition_events",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                async with session.begin_nested():
                    await session.execute(text(statement))
    async with stack["runtime"]() as session, session.begin():
        # No tenant context: RLS shows nothing.
        assert (await session.scalar(text("SELECT count(*) FROM room_transition_events"))) == 0
    assert len(await rows(stack)) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"event_type": "LOITERED"},
        {"crossing_x": float("nan")},
        {"crossing_y": float("inf")},
        {"crossing_x": 1.01},
        {"evidence_observations": 0},
        {"ephemeral_track_id": 0},
        {"stream_instance_id": "UPPER"},
        {"occurred_at": "FUTURE"},
        {"occurred_at": "STALE"},
    ],
)
async def test_the_table_refuses_out_of_bounds_rows(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    w = await world(stack)
    now = datetime.now(UTC)
    if values.get("occurred_at") == "FUTURE":
        values = {"occurred_at": now + timedelta(minutes=10)}
    elif values.get("occurred_at") == "STALE":
        values = {"occurred_at": now - timedelta(days=8)}
    row: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": stack["tenant_a"],
        "facility_id": stack["f1"],
        "area_id": UUID(w["room_a"]),
        "camera_id": UUID(w["camera_a"]),
        "edge_node_id": w["node_a"],
        "portal_id": UUID(w["portal_a"]),
        "event_type": "ENTERED",
        "occurred_at": now,
        "ephemeral_track_id": 1,
        "stream_instance_id": "abc",
        "crossing_x": 0.5,
        "crossing_y": 0.5,
        "evidence_observations": 3,
    }
    row.update(values)
    with pytest.raises(DBAPIError, match="check constraint"):
        async with stack["admin"]() as session, session.begin():
            await set_tenant(session, stack["tenant_a"])
            session.add(RoomTransitionEvent(**row))
            await session.flush()
