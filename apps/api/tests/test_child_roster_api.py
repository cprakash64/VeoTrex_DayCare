"""Child roster and attendance over HTTP and in PostgreSQL (V1-04D).

Reuses the V1-04A two-tenant stack (a tenant owner, a facility admin and a viewer on facility 1,
a viewer on facility 2, another tenant) and the V1-04C staff helpers. Proves the tables' own
invariants, the roster and attendance APIs, attendance-mode source precedence and ratio status,
visitor semantics, isolation, concurrency and audit contents. Every child is a synthetic roster
entry ("Child A" ...); nothing here has a photo, a date of birth or a guardian.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_classroom_api import create_classroom, stack  # noqa: F401 - fixture
from test_classroom_presence_api import classroom_with_policy, report, status
from test_staff_roster_api import act, principal, set_mode, status_at, teacher

from veotrex_api.models import AuditEvent, ChildAttendanceEvent, ChildProfile

ATTENDANCE_MODE = "ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF"
ROSTER_MODE = "ROSTER_STAFF_PLUS_MANUAL_CHILDREN"
MANUAL_MODE = "MANUAL_AGGREGATE"
PROFILES = "child_profiles"
EVENTS = "child_attendance_events"


# ================================================================================ helpers
async def add_child(
    stack: dict[str, Any],  # noqa: F811
    name: str,
    *,
    facility: str = "f1",
    as_: str = "admin-1",
    reference: str | None = None,
) -> Any:
    body: dict[str, Any] = {"display_name": name}
    if reference is not None:
        body["external_reference"] = reference
    return await stack["client"].post(
        f"/v1/facilities/{stack[facility]}/children", json=body, headers=stack[as_]
    )


async def child(stack: dict[str, Any], name: str, **kwargs: Any) -> str:  # noqa: F811
    response = await add_child(stack, name, **kwargs)
    assert response.status_code == 201, response.text
    return str(response.json()["child_id"])


async def attend(
    stack: dict[str, Any],  # noqa: F811
    room: str,
    action: str,
    child_id: str,
    *,
    as_: str = "admin-1",
    **extra: Any,
) -> Any:
    return await stack["client"].post(
        f"/v1/classrooms/{room}/attendance/{action}",
        json={"child_profile_id": child_id, **extra},
        headers=stack[as_],
    )


async def lifecycle(stack: dict[str, Any], child_id: str, verb: str, as_: str = "admin-1") -> Any:  # noqa: F811
    return await stack["client"].post(f"/v1/children/{child_id}/{verb}", headers=stack[as_])


async def attendance_room(stack: dict[str, Any], name: str | None = None) -> str:  # noqa: F811
    room = (
        await classroom_with_policy(stack)
        if name is None
        else str((await create_classroom(stack, name=name))["classroom_id"])
    )
    response = await set_mode(stack, room, ATTENDANCE_MODE)
    assert response.status_code == 200, response.text
    assert response.json()["presence_source_mode"] == ATTENDANCE_MODE
    return room


async def attendance_view(stack: dict[str, Any], room: str, as_: str = "admin-1") -> Any:  # noqa: F811
    response = await stack["client"].get(f"/v1/classrooms/{room}/attendance", headers=stack[as_])
    assert response.status_code == 200, response.text
    return response.json()


def entry(body: dict[str, Any], child_id: str) -> dict[str, Any]:
    return next(item for item in body["children"] if item["child_profile_id"] == child_id)


async def events_of(stack: dict[str, Any], child_id: str) -> list[ChildAttendanceEvent]:  # noqa: F811
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        return list(
            (
                await session.scalars(
                    select(ChildAttendanceEvent)
                    .where(ChildAttendanceEvent.child_profile_id == UUID(child_id))
                    .order_by(ChildAttendanceEvent.sequence)
                )
            ).all()
        )


async def audits(stack: dict[str, Any], target_types: list[str]) -> list[AuditEvent]:  # noqa: F811
    async with stack["admin"]() as session, session.begin():
        return list(
            (
                await session.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.tenant_id == stack["tenant_a"],
                        AuditEvent.target_type.in_(target_types),
                    )
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            ).all()
        )


async def raw_profile(stack: dict[str, Any], **values: Any) -> None:  # noqa: F811
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
            "display_name": "Child Raw",
            "status": "ACTIVE",
            "created_by_actor_id": actor,
        }
        row.update(values)
        session.add(ChildProfile(**row))
        await session.flush()


async def raw_event(stack: dict[str, Any], room: str, child_id: str, **values: Any) -> None:  # noqa: F811
    tenant = stack["tenant_a"]
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
        )
        actor = await session.scalar(
            text("SELECT id FROM actors WHERE tenant_id = :t ORDER BY id LIMIT 1"), {"t": tenant}
        )
        now = datetime.now(UTC)
        row: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": tenant,
            "facility_id": stack["f1"],
            "area_id": UUID(room),
            "child_profile_id": UUID(child_id),
            "sequence": 1,
            "event_type": "CHECKED_IN",
            "source": "ATTENDANCE",
            "occurred_at": now,
            "valid_until": now + timedelta(hours=8),
            "checked_in_at": now,
            "recorded_by_actor_id": actor,
        }
        row.update(values)
        session.add(ChildAttendanceEvent(**row))
        await session.flush()


# ======================================================================= database invariants
@pytest.mark.parametrize(
    "values",
    [
        {"status": "DELETED"},
        {"display_name": ""},
        {"display_name": "x" * 121},
        {"display_name": "Child\tA"},
        {"display_name": " padded"},
        {"display_name": "<b>A</b>"},
        {"external_reference": "has space"},
        {"external_reference": "-leading"},
    ],
)
async def test_the_database_refuses_invalid_profiles(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    with pytest.raises(DBAPIError, match="check constraint|violates|too long"):
        await raw_profile(stack, **values)


@pytest.mark.parametrize(
    "values",
    [
        {"source": "VISION"},
        {"source": "STAFF_RECOGNITION"},
        {"event_type": "SEEN_ON_CAMERA"},
        {"sequence": 0},
        {"valid_until": None},
        {"valid_until": "too_long"},
        {"valid_until": "too_short"},
        {"event_type": "CHECKED_OUT"},
        {"occurred_at": "future"},
    ],
)
async def test_the_database_refuses_invalid_events(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    room = await attendance_room(stack)
    kid = await child(stack, "Child Check")
    now = datetime.now(UTC)
    resolved = dict(values)
    if resolved.get("valid_until") == "too_long":
        resolved.update(valid_until=now + timedelta(hours=13), occurred_at=now, checked_in_at=now)
    elif resolved.get("valid_until") == "too_short":
        resolved.update(valid_until=now + timedelta(minutes=5), occurred_at=now, checked_in_at=now)
    elif resolved.get("occurred_at") == "future":
        later = now + timedelta(minutes=10)
        resolved.update(
            occurred_at=later, checked_in_at=later, valid_until=later + timedelta(hours=1)
        )
    with pytest.raises(DBAPIError, match="check constraint|violates"):
        await raw_event(stack, room, kid, **resolved)


async def test_a_child_cannot_be_stored_in_another_facilitys_classroom(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    elsewhere = str(
        (await create_classroom(stack, "f2", as_="owner-a", name="Far room"))["classroom_id"]
    )
    kid = await child(stack, "Child Facility")
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_event(stack, elsewhere, kid)
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_event(stack, elsewhere, kid, facility_id=stack["f2"])


async def test_one_sequence_position_per_child(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    other = await attendance_room(stack, name="Room 2")
    kid = await child(stack, "Child Sequence")
    await raw_event(stack, room, kid)
    with pytest.raises(DBAPIError, match="uq_child_attendance_events_child_sequence"):
        await raw_event(stack, other, kid)


async def test_runtime_is_append_only_on_events_and_never_deletes(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child Append")
    assert (await attend(stack, room, "check-in", kid)).status_code == 201
    for statement in (
        "UPDATE child_attendance_events SET valid_until = valid_until + interval '1 hour'",
        "DELETE FROM child_attendance_events",
        "DELETE FROM child_profiles",
    ):
        async with stack["runtime"]() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(stack["tenant_a"])},
            )
            with pytest.raises(DBAPIError, match="permission denied"):
                async with session.begin_nested():
                    await session.execute(text(statement))
    assert len(await events_of(stack, kid)) == 1


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        (EVENTS, {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False}),
        (PROFILES, {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": False}),
    ],
)
async def test_runtime_grants_are_minimal_rls_forced_and_public_denied(
    stack: dict[str, Any],  # noqa: F811
    runtime_role_name: str,
    table: str,
    expected: dict[str, bool],
) -> None:
    async with stack["admin"]() as session, session.begin():
        privileges = {
            privilege: await session.scalar(
                text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": runtime_role_name, "table": f"public.{table}", "privilege": privilege},
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
        public_grants = await session.scalar(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE table_name = :table AND grantee = 'PUBLIC'"
            ),
            {"table": table},
        )
        forced = await session.scalar(
            text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = :t"),
            {"t": table},
        )
    assert privileges == {**expected, "TRUNCATE": False}
    assert public_grants == 0 and forced is True


async def test_rls_isolates_tenants_and_fails_closed(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child RLS")
    await attend(stack, room, "check-in", kid)
    for tenant in (None, stack["tenant_b"]):
        async with stack["runtime"]() as session, session.begin():
            if tenant is not None:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
                )
            for table in (PROFILES, EVENTS):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0  # noqa: S608


# =============================================================================== roster API
async def test_an_admin_creates_lists_and_reads_a_child(stack: dict[str, Any]) -> None:  # noqa: F811
    response = await add_child(stack, "  Child   A ", reference="SIS-001")
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["display_name"], body["status"], body["external_reference"]) == (
        "Child A",
        "ACTIVE",
        "SIS-001",
    )
    assert set(body) == {
        "child_id",
        "facility_id",
        "display_name",
        "status",
        "external_reference",
        "can_administer",
        "created_at",
        "updated_at",
    }
    listed = await stack["client"].get(
        f"/v1/facilities/{stack['f1']}/children", headers=stack["viewer-1"]
    )
    assert listed.status_code == 200
    assert [item["child_id"] for item in listed.json()["children"]] == [body["child_id"]]
    assert listed.json()["can_administer"] is False
    one = await stack["client"].get(f"/v1/children/{body['child_id']}", headers=stack["viewer-1"])
    assert one.status_code == 200 and one.json()["display_name"] == "Child A"


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": ""},
        {"display_name": "x" * 121},
        {"display_name": "Child\u0000A"},
        {"display_name": "Child\u202eA"},
        {"display_name": "<img src=x>"},
        {"display_name": "Child A", "external_reference": "has space"},
        {"display_name": "Child A", "date_of_birth": "2022-01-01"},
        {"display_name": "Child A", "photo": "AAAA"},
        {"display_name": "Child A", "guardian_id": str(uuid4())},
        {"display_name": "Child A", "face_embedding": [0.1]},
    ],
)
async def test_invalid_or_excess_child_fields_are_422(
    stack: dict[str, Any],  # noqa: F811
    body: dict[str, Any],
) -> None:
    response = await stack["client"].post(
        f"/v1/facilities/{stack['f1']}/children", json=body, headers=stack["admin-1"]
    )
    assert response.status_code == 422
    assert "Child\u202eA" not in response.text and "<img" not in response.text


async def test_external_reference_is_unique_per_facility_only(stack: dict[str, Any]) -> None:  # noqa: F811
    await child(stack, "Child Ref", reference="SIS-9")
    clash = await add_child(stack, "Child Other", reference="SIS-9")
    assert clash.status_code == 409
    assert clash.json()["detail"]["category"] == "external_reference_exists"
    elsewhere = await add_child(stack, "Child Far", facility="f2", as_="owner-a", reference="SIS-9")
    assert elsewhere.status_code == 201, "another facility may reuse the reference"
    assert (await add_child(stack, "Child NoRef")).status_code == 201
    assert (await add_child(stack, "Child NoRef 2")).status_code == 201, "absent is not unique"


async def test_edit_and_clear_fields(stack: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Edit", reference="SIS-E")
    url = f"/v1/children/{kid}"
    renamed = await stack["client"].patch(
        url, json={"display_name": "Child Edited"}, headers=stack["admin-1"]
    )
    assert renamed.status_code == 200
    assert (renamed.json()["display_name"], renamed.json()["external_reference"]) == (
        "Child Edited",
        "SIS-E",
    )
    cleared = await stack["client"].patch(
        url, json={"external_reference": None}, headers=stack["admin-1"]
    )
    assert cleared.json()["external_reference"] is None
    bad = await stack["client"].patch(url, json={"display_name": "A\tB"}, headers=stack["admin-1"])
    assert bad.status_code == 422


async def test_lifecycle_deactivate_reactivate_archive(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child Life")
    assert (await lifecycle(stack, kid, "deactivate")).json()["status"] == "INACTIVE"
    refused = await attend(stack, room, "check-in", kid)
    assert refused.status_code == 409
    assert refused.json()["detail"]["category"] == "child_not_active"
    assert (await lifecycle(stack, kid, "activate")).json()["status"] == "ACTIVE"
    assert (await attend(stack, room, "check-in", kid)).status_code == 201
    archived = await lifecycle(stack, kid, "archive")
    assert archived.json()["status"] == "ARCHIVED"
    assert (await lifecycle(stack, kid, "archive")).status_code == 200, "idempotent"
    for verb in ("activate", "deactivate"):
        again = await lifecycle(stack, kid, verb)
        assert again.status_code == 409
        assert again.json()["detail"]["category"] == "child_archived"
    edit = await stack["client"].patch(
        f"/v1/children/{kid}", json={"display_name": "Nope"}, headers=stack["admin-1"]
    )
    assert edit.status_code == 409
    view = await attendance_view(stack, room)
    archived_entry = entry(view, kid)
    assert (archived_entry["location"], archived_entry["counted"]) == ("HERE", False)
    assert view["summary"]["count"] == 0 and view["summary"]["present_inactive"] == 1
    assert (await attend(stack, room, "check-out", kid)).status_code == 200
    refused = await attend(stack, room, "check-in", kid)
    assert refused.json()["detail"]["category"] == "child_archived"


async def test_authorization_and_uniform_404(stack: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Scope")
    assert (await add_child(stack, "Child V", as_="viewer-1")).status_code == 403
    assert (await lifecycle(stack, kid, "deactivate", as_="viewer-1")).status_code == 403
    edit = await stack["client"].patch(
        f"/v1/children/{kid}", json={"display_name": "X"}, headers=stack["viewer-1"]
    )
    assert edit.status_code == 403
    for who in ("owner-b", "viewer-2"):
        read = await stack["client"].get(f"/v1/children/{kid}", headers=stack[who])
        assert read.status_code == 404 and read.json() == {"detail": "not found"}
        listing = await stack["client"].get(
            f"/v1/facilities/{stack['f1']}/children", headers=stack[who]
        )
        assert listing.status_code == 404
    unknown = await stack["client"].get(f"/v1/children/{uuid4()}", headers=stack["admin-1"])
    assert unknown.status_code == 404 and unknown.json() == {"detail": "not found"}
    # A facility admin of facility one cannot create a child at facility two.
    assert (await add_child(stack, "Child F2", facility="f2", as_="admin-1")).status_code == 404
    assert (await add_child(stack, "Child F2", facility="f2", as_="viewer-2")).status_code == 403


# ============================================================================ attendance API
async def test_check_in_refresh_and_check_out(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child Bea")
    created = await attend(stack, room, "check-in", kid)
    assert created.status_code == 201, created.text
    mine = entry(created.json(), kid)
    assert (mine["state"], mine["location"], mine["counted"]) == ("PRESENT", "HERE", True)
    lease = datetime.fromisoformat(mine["valid_until"]) - datetime.fromisoformat(
        mine["checked_in_at"]
    )
    assert lease == timedelta(hours=12), "default lease is twelve hours"
    body = created.json()
    assert (body["lease_min_seconds"], body["lease_max_seconds"]) == (1800, 43200)
    refreshed = await attend(stack, room, "refresh", kid, lease_seconds=3600)
    assert refreshed.status_code == 200
    after = entry(refreshed.json(), kid)
    assert after["checked_in_at"] == mine["checked_in_at"]
    out = await attend(stack, room, "check-out", kid)
    assert out.status_code == 200 and entry(out.json(), kid)["state"] == "NOT_CHECKED_IN"
    assert [row.event_type for row in await events_of(stack, kid)] == [
        "CHECKED_IN",
        "REFRESHED",
        "CHECKED_OUT",
    ]


async def test_duplicates_wrong_room_and_moves(stack: dict[str, Any]) -> None:  # noqa: F811
    room_x = await attendance_room(stack)
    room_y = await attendance_room(stack, name="Room 2")
    kid = await child(stack, "Child Mover")
    await attend(stack, room_x, "check-in", kid)
    again = await attend(stack, room_x, "check-in", kid)
    assert again.status_code == 409
    assert again.json()["detail"]["category"] == "child_already_checked_in"
    moved = await attend(stack, room_y, "check-in", kid)
    assert moved.status_code == 201
    rows = await events_of(stack, kid)
    assert [(row.sequence, row.event_type, str(row.area_id)) for row in rows] == [
        (1, "CHECKED_IN", room_x),
        (2, "CHECKED_OUT", room_x),
        (3, "CHECKED_IN", room_y),
    ]
    assert rows[1].occurred_at == rows[2].occurred_at
    wrong = await attend(stack, room_x, "check-out", kid)
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["category"] == "child_in_another_classroom"
    view_x = await attendance_view(stack, room_x)
    assert entry(view_x, kid)["location"] == "OTHER_CLASSROOM"
    assert entry(view_x, kid)["other_classroom_name"] == "Room 2"
    assert view_x["summary"]["count"] == 0
    assert (await attendance_view(stack, room_y))["summary"]["count"] == 1
    assert (await attend(stack, room_y, "check-out", kid)).status_code == 200
    repeat = await attend(stack, room_y, "check-out", kid)
    assert repeat.status_code == 200 and len(await events_of(stack, kid)) == 4


async def test_refresh_after_expiry_and_stale_attendance(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child Stale")
    now = datetime.now(UTC)
    await raw_event(
        stack,
        room,
        kid,
        occurred_at=now - timedelta(minutes=1),
        checked_in_at=now - timedelta(minutes=1),
        valid_until=now - timedelta(minutes=1) + timedelta(minutes=30),
    )
    service = stack["client"]._transport.app.state.child_roster_service
    later = await service.get_attendance(
        await principal(stack), UUID(room), now=now + timedelta(hours=1)
    )
    assert later.summary.count == 0 and later.summary.stale == 1
    stale_status = await status_at(stack, room, now + timedelta(hours=1))
    assert stale_status["sources"]["child_attendance"]["stale"] == 1
    assert stale_status["evaluation"]["child_count"] == 0


@pytest.mark.parametrize("lease", [60, 29 * 60, 12 * 3600 + 1, 18 * 3600])
async def test_leases_outside_bounds_are_422(stack: dict[str, Any], lease: int) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child Lease")
    response = await attend(stack, room, "check-in", kid, lease_seconds=lease)
    assert response.status_code == 422
    assert response.json()["detail"]["category"] == "invalid_lease_seconds"


@pytest.mark.parametrize(
    "extra",
    [
        {"track_id": 7},
        {"camera_id": str(uuid4())},
        {"face_match_id": str(uuid4())},
        {"occurred_at": "2030-01-01T00:00:00Z"},
        {"lease_seconds": "3600"},
        {"image": "AAAA"},
    ],
)
async def test_attendance_takes_an_explicit_child_uuid_only(
    stack: dict[str, Any],  # noqa: F811
    extra: dict[str, Any],
) -> None:
    room = await attendance_room(stack)
    kid = await child(stack, "Child Strict")
    assert (await attend(stack, room, "check-in", kid, **extra)).status_code == 422
    track_only = await stack["client"].post(
        f"/v1/classrooms/{room}/attendance/check-in", json={"track_id": 3}, headers=stack["admin-1"]
    )
    assert track_only.status_code == 422


async def test_inactive_classroom_and_other_facility_children(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    far = await child(stack, "Child Far", facility="f2", as_="owner-a")
    response = await attend(stack, room, "check-in", far, as_="owner-a")
    assert response.status_code == 404 and response.json() == {"detail": "not found"}
    kid = await child(stack, "Child Closed")
    await stack["client"].post(f"/v1/classrooms/{room}/deactivate", headers=stack["admin-1"])
    refused = await attend(stack, room, "check-in", kid)
    assert refused.json()["detail"]["category"] == "classroom_inactive"
    assert (await attend(stack, room, "check-in", str(uuid4()))).status_code == 404


async def test_attendance_scope_and_writes(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child Viewer")
    for verb in ("check-in", "refresh", "check-out"):
        assert (await attend(stack, room, verb, kid, as_="viewer-1")).status_code == 403
    assert (await attendance_view(stack, room, as_="viewer-1"))["can_administer"] is False
    for who in ("owner-b", "viewer-2"):
        read = await stack["client"].get(f"/v1/classrooms/{room}/attendance", headers=stack[who])
        assert read.status_code == 404
        written = await attend(stack, room, "check-in", kid, as_=who)
        assert written.status_code in (403, 404)
        if who == "owner-b":
            assert written.status_code == 404


async def test_another_classrooms_attendance_does_not_count_here(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    other = await attendance_room(stack, name="Room 2")
    kid = await child(stack, "Child Next Door")
    await attend(stack, other, "check-in", kid)
    assert (await attendance_view(stack, room))["summary"]["count"] == 0
    result = await status(stack, room)
    assert result["evaluation"]["child_count"] == 0


# ========================================================================== source mode + ratio
async def test_attendance_mode_ratio_with_provenance(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kids = [await child(stack, f"Child {letter}") for letter in "ABCDEF"]
    teacher_a = await teacher(stack, "Teacher A")
    teacher_b = await teacher(stack, "Teacher B")
    for kid in kids:
        assert (await attend(stack, room, "check-in", kid)).status_code == 201
    await act(stack, room, "check-in", teacher_a)
    result = await status(stack, room)
    evaluation, sources = result["evaluation"], result["sources"]
    assert result["presence_source_mode"] == ATTENDANCE_MODE
    assert (evaluation["child_count"], evaluation["staff_count"]) == (6, 1)
    assert (evaluation["required_staff"], evaluation["staff_deficit"]) == (2, 1)
    assert evaluation["ratio_state"] == "OVER_CONFIGURED_RATIO"
    assert (sources["children"]["source"], sources["qualified_staff"]["source"]) == (
        "ATTENDANCE",
        "STAFF_ROSTER",
    )
    assert sources["child_attendance"]["count"] == 6
    assert result["presence_connected"] is True
    await act(stack, room, "check-in", teacher_b)
    assert (await status(stack, room))["evaluation"]["ratio_state"] == "WITHIN_CONFIGURED_POLICY"
    await attend(stack, room, "check-out", kids[-1])
    five = (await status(stack, room))["evaluation"]
    assert (five["child_count"], five["staff_count"], five["ratio_state"]) == (
        5,
        2,
        "WITHIN_CONFIGURED_POLICY",
    )


@pytest.mark.parametrize(
    ("children", "staff", "state"),
    [
        (5, 1, "WITHIN_CONFIGURED_POLICY"),
        (1, 0, "OVER_CONFIGURED_RATIO"),
        (0, 0, "NO_CHILDREN_PRESENT"),
    ],
)
async def test_attendance_ratio_states(
    stack: dict[str, Any],  # noqa: F811
    children: int,
    staff: int,
    state: str,
) -> None:
    room = await attendance_room(stack)
    for index in range(children):
        await attend(stack, room, "check-in", await child(stack, f"Child {index}"))
    for index in range(staff):
        await act(stack, room, "check-in", await teacher(stack, f"Teacher {index}"))
    assert (await status(stack, room))["evaluation"]["ratio_state"] == state


async def test_manual_counts_are_refused_and_never_added_in_attendance_mode(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await classroom_with_policy(stack)
    await report(stack, room, 9, 3, 1)  # a V1-04B report: 9 children, 3 staff, 1 visitor
    await set_mode(stack, room, ATTENDANCE_MODE)
    kid = await child(stack, "Child Only")
    await attend(stack, room, "check-in", kid)
    result = await status(stack, room)
    assert (result["evaluation"]["child_count"], result["evaluation"]["staff_count"]) == (1, 0)
    assert result["presence"]["child_count"] is None, "the manual 9 is not shown or used"
    assert result["sources"]["visitors"]["count"] == 1
    for body, category in (
        ({"child_count": 4, "visitor_count": 0}, "child_count_comes_from_attendance"),
        ({"qualified_staff_count": 2, "visitor_count": 0}, "staff_count_comes_from_roster"),
    ):
        refused = await stack["client"].post(
            f"/v1/classrooms/{room}/presence/manual", json=body, headers=stack["admin-1"]
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["category"] == category
    missing_visitors = await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual", json={}, headers=stack["admin-1"]
    )
    assert missing_visitors.status_code == 422
    assert missing_visitors.json()["detail"]["category"] == "invalid_visitor_count"
    visitors_only = await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual",
        json={"visitor_count": 2},
        headers=stack["admin-1"],
    )
    assert visitors_only.status_code == 201
    current = visitors_only.json()["current"]
    assert (current["child_count"], current["qualified_staff_count"], current["visitor_count"]) == (
        None,
        None,
        2,
    )


async def test_switching_modes_is_explicit_audited_and_never_copies_counts(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await classroom_with_policy(stack)
    assert (await stack["client"].get(f"/v1/classrooms/{room}", headers=stack["viewer-1"])).json()[
        "presence_source_mode"
    ] == MANUAL_MODE
    await set_mode(stack, room, ATTENDANCE_MODE)
    await attend(stack, room, "check-in", await child(stack, "Child Switch"))
    await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual",
        json={"visitor_count": 0},
        headers=stack["admin-1"],
    )
    await set_mode(stack, room, ROSTER_MODE)
    roster = await status(stack, room)
    assert roster["evaluation"]["ratio_state"] == "INSUFFICIENT_DATA"
    assert "CHILD_COUNT_MISSING" in roster["evaluation"]["explanations"], "attendance not copied"
    await set_mode(stack, room, MANUAL_MODE)
    manual = await status(stack, room)
    assert {"CHILD_COUNT_MISSING", "STAFF_COUNT_MISSING"} <= set(
        manual["evaluation"]["explanations"]
    )
    changes = [
        event.metadata_
        for event in await audits(stack, ["classroom"])
        if event.action == "classroom.presence_source_mode_changed"
        and event.target_id == UUID(room)
    ]
    assert changes == [
        {"from": MANUAL_MODE, "to": ATTENDANCE_MODE},
        {"from": ATTENDANCE_MODE, "to": ROSTER_MODE},
        {"from": ROSTER_MODE, "to": MANUAL_MODE},
    ]


async def test_visitors_never_block_and_are_never_assumed_zero(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    await attend(stack, room, "check-in", await child(stack, "Child Visit"))
    await act(stack, room, "check-in", await teacher(stack, "Teacher Visit"))
    result = await status(stack, room)
    assert result["evaluation"]["ratio_state"] == "WITHIN_CONFIGURED_POLICY"
    assert result["sources"]["visitors"] == {
        "count": None,
        "source": None,
        "freshness": "MISSING",
        "valid_until": None,
    }
    await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual",
        json={"visitor_count": 2},
        headers=stack["admin-1"],
    )
    fresh = await status(stack, room)
    assert fresh["sources"]["visitors"]["count"] == 2
    later = await status_at(stack, room, datetime.now(UTC) + timedelta(minutes=5))
    assert later["sources"]["visitors"]["count"] is None
    assert later["sources"]["visitors"]["freshness"] == "STALE"
    assert later["evaluation"]["ratio_state"] == "WITHIN_CONFIGURED_POLICY"


# ============================================================================== concurrency
async def test_simultaneous_check_ins_leave_one_room_and_a_gap_free_stream(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    rooms = [await attendance_room(stack), await attendance_room(stack, name="Room 2")]
    kid = await child(stack, "Child Race")
    responses = await asyncio.gather(
        *(attend(stack, rooms[index % 2], "check-in", kid) for index in range(10))
    )
    assert all(response.status_code in (201, 409) for response in responses)
    assert any(response.status_code == 201 for response in responses)
    rows = await events_of(stack, kid)
    assert [row.sequence for row in rows] == list(range(1, len(rows) + 1))
    views = [await attendance_view(stack, room) for room in rooms]
    assert [entry(view, kid)["location"] for view in views].count("HERE") == 1
    assert sum(view["summary"]["count"] for view in views) == 1
    open_rooms = 0
    for row in rows:
        open_rooms += 1 if row.event_type == "CHECKED_IN" else -1
        assert 0 <= open_rooms <= 1


# ==================================================================================== audit
async def test_roster_and_attendance_audits_carry_no_child_pii(stack: dict[str, Any]) -> None:  # noqa: F811
    room_x = await attendance_room(stack)
    room_y = await attendance_room(stack, name="Room 2")
    kid = await child(stack, "Child Private Name", reference="SIS-PRIVATE-77")
    await stack["client"].patch(
        f"/v1/children/{kid}",
        json={"display_name": "Child Renamed Secret", "external_reference": "SIS-PRIVATE-78"},
        headers=stack["admin-1"],
    )
    await lifecycle(stack, kid, "deactivate")
    await lifecycle(stack, kid, "activate")
    await attend(stack, room_x, "check-in", kid)
    await attend(stack, room_x, "refresh", kid, lease_seconds=7200)
    await attend(stack, room_y, "check-in", kid)
    await attend(stack, room_y, "check-out", kid)
    await attend(stack, room_y, "check-out", kid)  # idempotent: not audited again
    await lifecycle(stack, kid, "archive")
    events = await audits(stack, ["child_profile", "child_attendance_event"])
    mine = [
        event
        for event in events
        if event.target_id == UUID(kid) or event.metadata_.get("child_profile_id") == kid
    ]
    assert [event.action for event in mine] == [
        "child.created",
        "child.updated",
        "child.deactivated",
        "child.activated",
        "attendance.checked_in",
        "attendance.refreshed",
        "attendance.moved",
        "attendance.checked_out",
        "child.archived",
    ]
    assert mine[0].metadata_["external_reference_present"] is True
    assert mine[1].metadata_["changed_fields"] == ["display_name", "external_reference"]
    moved = mine[6].metadata_
    assert (moved["from_classroom_id"], moved["classroom_id"]) == (room_x, room_y)
    assert mine[5].metadata_["lease_seconds"] == 7200
    rendered = str([event.metadata_ for event in mine]).lower()
    for forbidden in (
        "private name",
        "renamed secret",
        "sis-private",
        "display_name':",
        "photo",
        "face",
        "embedding",
        "track",
        "image",
        "token",
    ):
        assert forbidden not in rendered, forbidden
