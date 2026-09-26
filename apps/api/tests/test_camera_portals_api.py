"""Camera portal (doorway line) configuration over HTTP and in PostgreSQL (V1-05A).

Reuses the V1-04A two-tenant stack. Every camera is a synthetic row attached to a classroom
through a zone; nothing here has an image, a person or a track. Proves the table's own
invariants, the API's validation (the same rules the edge applies), scoping, bounded counts,
archive-not-delete and audit contents. Distribution to the edge is tested in
test_edge_runtime_api.py (V1-05B).
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_classroom_api import create_classroom, stack  # noqa: F401 - fixture

from veotrex_api import camera_portal
from veotrex_api.models import (
    AuditEvent,
    Camera,
    CameraPortal,
    CameraProviderConnection,
    Zone,
)

ROOT = Path(__file__).resolve().parents[3]
EDGE_GEOMETRY = ROOT / "services/edge-agent/src/veotrex_edge_agent/live/portal_geometry.py"
DOOR = {
    "label": "Main door",
    "x1": 0.5,
    "y1": 0.05,
    "x2": 0.5,
    "y2": 0.95,
    "inside_side": "RIGHT",
}


async def add_camera(
    stack: dict[str, Any],  # noqa: F811
    room: str,
    *,
    tenant: str = "tenant_a",
    status: str = "ACTIVE",
) -> str:
    tenant_id = stack[tenant]
    camera_id = uuid4()
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
        )
        connection = CameraProviderConnection(
            id=uuid4(),
            tenant_id=tenant_id,
            name=f"Synthetic provider {uuid4().hex[:6]}",
            provider_type="RING",
            status="ACTIVE",
            integration_state="ACTIVE",
        )
        zone = Zone(
            id=uuid4(),
            tenant_id=tenant_id,
            area_id=UUID(room),
            name=f"Zone {uuid4().hex[:6]}",
            status="ACTIVE",
        )
        session.add_all([connection, zone])
        await session.flush()
        session.add(
            Camera(
                id=camera_id,
                tenant_id=tenant_id,
                zone_id=zone.id,
                provider_connection_id=connection.id,
                provider_device_id=f"synthetic-{uuid4().hex}",
                provider_component_key="__single__",
                name="Synthetic Indoor",
                status=status,
            )
        )
    return str(camera_id)


async def classroom_camera(stack: dict[str, Any], name: str | None = None) -> tuple[str, str]:  # noqa: F811
    room = str(
        (await create_classroom(stack, name=name or f"Room {uuid4().hex[:6]}"))["classroom_id"]
    )
    return room, await add_camera(stack, room)


def url(room: str, camera: str, suffix: str = "") -> str:
    return f"/v1/classrooms/{room}/cameras/{camera}/portals{suffix}"


async def create(
    stack: dict[str, Any],  # noqa: F811
    room: str,
    camera: str,
    as_: str = "admin-1",
    **changes: Any,
) -> Any:
    body = {**DOOR, **changes}
    return await stack["client"].post(url(room, camera), json=body, headers=stack[as_])


def category(response: Any) -> str:
    return str(response.json()["detail"]["category"])


async def raw_portal(stack: dict[str, Any], room: str, camera: str, **values: Any) -> None:  # noqa: F811
    tenant = stack["tenant_a"]
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
        )
        actor = await session.scalar(
            text("SELECT id FROM actors WHERE tenant_id = :t ORDER BY id LIMIT 1"), {"t": tenant}
        )
        row: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": tenant,
            "facility_id": stack["f1"],
            "area_id": UUID(room),
            "camera_id": UUID(camera),
            "label": "Raw door",
            "x1": 0.5,
            "y1": 0.1,
            "x2": 0.5,
            "y2": 0.9,
            "inside_side": "RIGHT",
            "deadband": 0.02,
            "enabled": True,
            "status": "ACTIVE",
            "revision": 1,
            "created_by_actor_id": actor,
        }
        row.update(values)
        session.add(CameraPortal(**row))
        await session.flush()


# ======================================================================= database invariants
@pytest.mark.parametrize(
    "values",
    [
        {"x1": math.nan},
        {"y2": math.inf},
        {"x2": 1.5},
        {"y1": -0.1},
        {"x2": 0.5, "y2": 0.105},  # shorter than 1% of the frame
        {"inside_side": "INWARD"},
        {"deadband": 0.5},
        {"label": ""},
        {"label": "<b>door</b>"},
        {"label": " padded"},
        {"status": "DELETED"},
        {"status": "ARCHIVED"},  # archived without when/by
        {"revision": 0},
    ],
)
async def test_the_database_refuses_invalid_portals(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    room, camera = await classroom_camera(stack)
    with pytest.raises(DBAPIError, match="check constraint|violates|too long"):
        await raw_portal(stack, room, camera, **values)


async def test_labels_are_unique_per_camera_among_active_portals(stack: dict[str, Any]) -> None:  # noqa: F811
    room, camera = await classroom_camera(stack)
    await raw_portal(stack, room, camera, label="Door")
    with pytest.raises(DBAPIError, match="uq_camera_portals_active_label"):
        await raw_portal(stack, room, camera, label="door")


async def test_runtime_grants_are_minimal_rls_forced_and_public_denied(
    stack: dict[str, Any],  # noqa: F811
    runtime_role_name: str,
) -> None:
    async with stack["admin"]() as session, session.begin():
        privileges = {
            privilege: await session.scalar(
                text("SELECT has_table_privilege(:role, 'public.camera_portals', :privilege)"),
                {"role": runtime_role_name, "privilege": privilege},
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
        public = await session.scalar(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE table_name = 'camera_portals' AND grantee = 'PUBLIC'"
            )
        )
        forced = await session.scalar(
            text(
                "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                "WHERE relname = 'camera_portals'"
            )
        )
    assert privileges == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": True,
        "DELETE": False,
        "TRUNCATE": False,
    }
    assert public == 0 and forced is True


async def test_rls_isolates_tenants_and_runtime_cannot_delete(stack: dict[str, Any]) -> None:  # noqa: F811
    room, camera = await classroom_camera(stack)
    assert (await create(stack, room, camera)).status_code == 201
    for tenant in (None, stack["tenant_b"]):
        async with stack["runtime"]() as session, session.begin():
            if tenant is not None:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
                )
            assert await session.scalar(text("SELECT count(*) FROM camera_portals")) == 0
    async with stack["runtime"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            async with session.begin_nested():
                await session.execute(text("DELETE FROM camera_portals"))


# ===================================================================================== API
async def test_create_list_edit_disable_and_archive(stack: dict[str, Any]) -> None:  # noqa: F811
    room, camera = await classroom_camera(stack)
    created = await create(stack, room, camera)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["edge_distribution"], body["max_portals"], body["can_configure"]) == (
        "NOT_CONNECTED",
        4,
        True,
    )
    portal = body["portals"][0]
    portal_id = portal["portal_id"]
    assert portal["inside_normal"] == [1.0, 0.0]
    assert portal["edge_flag"] == f"{portal_id}:0.5,0.05,0.5,0.95,RIGHT,Main door"
    assert (portal["status"], portal["enabled"], portal["revision"]) == ("ACTIVE", True, 1)
    listing = await stack["client"].get(url(room, camera), headers=stack["viewer-1"])
    assert listing.status_code == 200 and listing.json()["can_configure"] is False
    edited = await stack["client"].patch(
        url(room, camera, f"/{portal_id}"),
        json={"x1": 0.55, "x2": 0.55, "deadband": 0.04, "enabled": False},
        headers=stack["admin-1"],
    )
    assert edited.status_code == 200
    portal = edited.json()["portals"][0]
    assert (portal["x1"], portal["deadband"], portal["enabled"], portal["revision"]) == (
        0.55,
        0.04,
        False,
        2,
    )
    assert portal["edge_flag"].endswith(",deadband=0.04")
    same = await stack["client"].patch(
        url(room, camera, f"/{portal_id}"), json={"enabled": False}, headers=stack["admin-1"]
    )
    assert same.json()["portals"][0]["revision"] == 2
    for _ in range(2):
        archived = await stack["client"].post(
            url(room, camera, f"/{portal_id}/archive"), headers=stack["admin-1"]
        )
        assert archived.status_code == 200
    portal = archived.json()["portals"][0]
    assert (portal["status"], portal["enabled"], portal["revision"]) == ("ARCHIVED", False, 3)
    assert portal["archived_at"] is not None
    refused = await stack["client"].patch(
        url(room, camera, f"/{portal_id}"), json={"enabled": True}, headers=stack["admin-1"]
    )
    assert refused.status_code == 409 and category(refused) == "portal_archived"
    # The label is free again once archived; history is kept.
    assert (await create(stack, room, camera)).status_code == 201
    assert (
        len(
            (await stack["client"].get(url(room, camera), headers=stack["admin-1"])).json()[
                "portals"
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"x1": 0.5, "y1": 0.5, "x2": 0.5, "y2": 0.5}, "portal_too_short"),
        ({"y2": 0.055}, "portal_too_short"),
        ({"x2": 1.2}, "invalid_portal_coordinates"),
        ({"y1": -0.01}, "invalid_portal_coordinates"),
        ({"inside_side": "ABOVE"}, "portal_inside_ambiguous"),
        (
            {"x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5, "inside_side": "LEFT"},
            "portal_inside_ambiguous",
        ),
        ({"inside_side": "INWARD"}, "invalid_portal_inside"),
        ({"deadband": 0.2}, "invalid_portal_deadband"),
        ({"label": "<script>"}, "invalid_portal_label"),
        ({"label": "   "}, "invalid_portal_label"),
    ],
)
async def test_invalid_geometry_is_422_with_the_edge_rule(
    stack: dict[str, Any],  # noqa: F811
    changes: dict[str, Any],
    expected: str,
) -> None:
    room, camera = await classroom_camera(stack)
    response = await create(stack, room, camera, **changes)
    assert response.status_code == 422 and category(response) == expected


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
async def test_nan_and_infinity_are_422(stack: dict[str, Any], token: str) -> None:  # noqa: F811
    room, camera = await classroom_camera(stack)
    raw = json.dumps({**DOOR, "x1": 0.25}).replace("0.25", token)
    response = await stack["client"].post(
        url(room, camera),
        content=raw,
        headers={**stack["admin-1"], "content-type": "application/json"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [{"x1": "0.5"}, {"enabled": "yes"}, {"track_id": 1}, {"image": "x"}, {"face": [1]}],
)
async def test_only_numbers_a_side_and_a_label_are_accepted(
    stack: dict[str, Any],  # noqa: F811
    extra: dict[str, Any],
) -> None:
    room, camera = await classroom_camera(stack)
    assert (await create(stack, room, camera, **extra)).status_code == 422


async def test_at_most_four_active_portals_per_camera(stack: dict[str, Any]) -> None:  # noqa: F811
    room, camera = await classroom_camera(stack)
    for index in range(4):
        response = await create(
            stack, room, camera, label=f"Door {index}", x1=0.2 + index * 0.1, x2=0.2 + index * 0.1
        )
        assert response.status_code == 201, response.text
    refused = await create(stack, room, camera, label="Door 5")
    assert refused.status_code == 409 and category(refused) == "portal_limit_reached"
    duplicate_room, duplicate_camera = await classroom_camera(stack)
    await create(stack, duplicate_room, duplicate_camera)
    again = await create(stack, duplicate_room, duplicate_camera, label="main DOOR")
    assert again.status_code == 409 and category(again) == "portal_label_exists"


async def test_scoping_uniform_404_and_403(stack: dict[str, Any]) -> None:  # noqa: F811
    room, camera = await classroom_camera(stack)
    other_room = str((await create_classroom(stack, name="Room 2"))["classroom_id"])
    # A camera that exists but belongs to another classroom is unknown here.
    assert (await create(stack, other_room, camera)).status_code == 404
    assert (await create(stack, room, str(uuid4()))).status_code == 404
    assert (await create(stack, room, camera, as_="viewer-1")).status_code == 403
    assert (await create(stack, room, camera, as_="owner-b")).status_code == 404
    for who in ("owner-b", "viewer-2"):
        hidden = await stack["client"].get(url(room, camera), headers=stack[who])
        assert hidden.status_code == 404 and hidden.json() == {"detail": "not found"}
    archived_camera = await add_camera(stack, room, status="ARCHIVED")
    assert (await create(stack, room, archived_camera)).status_code == 404
    disabled_camera = await add_camera(stack, room, status="DISABLED")
    refused = await create(stack, room, disabled_camera)
    assert refused.status_code == 409 and category(refused) == "camera_inactive"


async def test_portal_audits_carry_geometry_but_no_label(stack: dict[str, Any]) -> None:  # noqa: F811
    room, camera = await classroom_camera(stack)
    portal_id = (await create(stack, room, camera, label="Secret Label Text")).json()["portals"][0][
        "portal_id"
    ]
    await stack["client"].patch(
        url(room, camera, f"/{portal_id}"),
        json={"label": "Other Secret Text", "inside_side": "LEFT"},
        headers=stack["admin-1"],
    )
    await stack["client"].post(url(room, camera, f"/{portal_id}/archive"), headers=stack["admin-1"])
    async with stack["admin"]() as session, session.begin():
        events = list(
            (
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.target_id == UUID(portal_id))
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            ).all()
        )
    assert [event.action for event in events] == [
        "camera_portal.created",
        "camera_portal.updated",
        "camera_portal.archived",
    ]
    assert events[1].metadata_["changed_fields"] == ["label", "inside_side"]
    assert events[1].metadata_["after"]["inside_side"] == "LEFT"
    rendered = str([event.metadata_ for event in events]).lower()
    for forbidden in ("secret", "label':", "track", "face", "image", "person"):
        assert forbidden not in rendered, forbidden


async def test_no_portal_route_lives_in_the_edge_namespace(stack: dict[str, Any]) -> None:  # noqa: F811
    paths = stack["client"]._transport.app.openapi()["paths"]
    portal_paths = [path for path in paths if "portal" in path]
    assert portal_paths
    assert all(path.startswith("/v1/classrooms/") for path in portal_paths)
    assert not [path for path in paths if path.startswith("/v1/edge") and "portal" in path]
    response = await stack["client"].get(
        f"/v1/classrooms/{uuid4()}/cameras/{uuid4()}/portals",
        headers={"Authorization": "Bearer vte1_" + uuid4().hex},
    )
    assert response.status_code == 401


# ============================================================================ edge parity
def _edge_constants() -> dict[str, Any]:
    """Read the edge module's constants from its source; the API never imports edge code."""
    values: dict[str, Any] = {}
    for node in ast.parse(EDGE_GEOMETRY.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return values


def test_the_control_plane_applies_exactly_the_edge_geometry_rules() -> None:
    edge = _edge_constants()
    for name in (
        "MAX_PORTALS",
        "MIN_PORTAL_LENGTH",
        "DEFAULT_DEADBAND",
        "MAX_DEADBAND",
        "MIN_SIDE_ALIGNMENT",
        "MAX_LABEL_LENGTH",
    ):
        assert getattr(camera_portal, name) == edge[name], name
    edge_source = EDGE_GEOMETRY.read_text(encoding="utf-8")
    assert camera_portal.LABEL_PATTERN.pattern in edge_source
    assert [str(side) for side in camera_portal.InsideSide] == ["LEFT", "RIGHT", "ABOVE", "BELOW"]
