"""Staff ratio eligibility and staff check-in/out over HTTP and in PostgreSQL (V1-04C).

Reuses the V1-04A two-tenant stack (a tenant owner, a facility admin and a viewer on facility 1,
a viewer on facility 2, another tenant). Proves the tables' own invariants (CHECKs, composite
keys, the one-active-designation index, the per-person sequence key, append-only grants, RLS,
PUBLIC and DELETE denial), the eligibility and presence APIs, the composite ratio status with its
source provenance, concurrency, and audit contents. Every person is a synthetic adult staff
profile; children appear only as an aggregate manual count.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_classroom_api import TODAY, create_classroom, stack  # noqa: F401 - fixture
from test_classroom_presence_api import classroom_with_policy, report, status

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.models import AuditEvent, StaffPresenceEvent, StaffRatioEligibility

ROSTER = "ROSTER_STAFF_PLUS_MANUAL_CHILDREN"
MANUAL = "MANUAL_AGGREGATE"
EVENTS = "staff_presence_events"
ELIGIBILITY = "staff_ratio_eligibility"


# ================================================================================ helpers
async def create_staff(stack: dict[str, Any], name: str, as_: str = "owner-a") -> str:  # noqa: F811
    response = await stack["client"].post(
        "/v1/staff", json={"display_name": name}, headers=stack[as_]
    )
    assert response.status_code == 201, response.text
    return str(response.json()["staff_id"])


async def designate(
    stack: dict[str, Any],  # noqa: F811
    staff_id: str,
    counts: bool = True,
    *,
    facility: str = "f1",
    as_: str = "admin-1",
    **extra: Any,
) -> Any:
    return await stack["client"].post(
        f"/v1/facilities/{stack[facility]}/staff-ratio-eligibility",
        json={"staff_profile_id": staff_id, "counts_toward_ratio": counts, **extra},
        headers=stack[as_],
    )


async def act(
    stack: dict[str, Any],  # noqa: F811
    room: str,
    action: str,
    staff_id: str,
    *,
    as_: str = "admin-1",
    **extra: Any,
) -> Any:
    return await stack["client"].post(
        f"/v1/classrooms/{room}/staff-presence/{action}",
        json={"staff_profile_id": staff_id, **extra},
        headers=stack[as_],
    )


async def set_mode(stack: dict[str, Any], room: str, mode: str, as_: str = "admin-1") -> Any:  # noqa: F811
    return await stack["client"].post(
        f"/v1/classrooms/{room}/presence-source-mode", json={"mode": mode}, headers=stack[as_]
    )


async def roster_room(stack: dict[str, Any], ratio: int = 5, name: str | None = None) -> str:  # noqa: F811
    if name is None:
        room = await classroom_with_policy(stack, ratio)
    else:
        room = str((await create_classroom(stack, name=name))["classroom_id"])
    response = await set_mode(stack, room, ROSTER)
    assert response.status_code == 200, response.text
    assert response.json()["presence_source_mode"] == ROSTER
    return room


async def children(stack: dict[str, Any], room: str, count: int, visitors: int = 0) -> Any:  # noqa: F811
    """A roster-mode manual report: children and visitors only, no staff number."""
    response = await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual",
        json={"child_count": count, "visitor_count": visitors},
        headers=stack["admin-1"],
    )
    assert response.status_code == 201, response.text
    return response.json()


async def teacher(
    stack: dict[str, Any],  # noqa: F811
    name: str,
    counts: bool = True,
) -> str:
    staff_id = await create_staff(stack, name)
    response = await designate(stack, staff_id, counts)
    assert response.status_code == 201, response.text
    return staff_id


async def principal(stack: dict[str, Any], who: str = "viewer-1") -> AuthenticatedPrincipal:  # noqa: F811
    me = (await stack["client"].get("/v1/me", headers=stack[who])).json()
    return AuthenticatedPrincipal(
        issuer="",
        subject="",
        external_organization_id="",
        actor_id=UUID(me["actor_id"]),
        tenant_id=UUID(me["tenant_id"]),
        display_name=None,
        grants=(RoleGrant(Role.VIEWER, stack["f1"]),),
        permissions=frozenset({Permission.READ_OPERATIONAL}),
    )


async def status_at(stack: dict[str, Any], room: str, moment: datetime) -> dict[str, Any]:  # noqa: F811
    service = stack["client"]._transport.app.state.classroom_service
    result = await service.ratio_status(await principal(stack), UUID(room), now=moment)
    return result.as_dict()  # type: ignore[no-any-return]


async def events_of(stack: dict[str, Any], staff_id: str) -> list[StaffPresenceEvent]:  # noqa: F811
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        return list(
            (
                await session.scalars(
                    select(StaffPresenceEvent)
                    .where(StaffPresenceEvent.staff_profile_id == UUID(staff_id))
                    .order_by(StaffPresenceEvent.sequence)
                )
            ).all()
        )


def entry(body: dict[str, Any], staff_id: str) -> dict[str, Any]:
    return next(item for item in body["staff"] if item["staff_profile_id"] == staff_id)


async def raw_event(stack: dict[str, Any], room: str, staff_id: str, **values: Any) -> None:  # noqa: F811
    """Insert directly as the admin identity to prove the database enforces the invariants."""
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
            "staff_profile_id": UUID(staff_id),
            "sequence": 1,
            "event_type": "CHECKED_IN",
            "source": "STAFF_ROSTER",
            "occurred_at": now,
            "valid_until": now + timedelta(minutes=15),
            "checked_in_at": now,
            "recorded_by_actor_id": actor,
        }
        row.update(values)
        session.add(StaffPresenceEvent(**row))
        await session.flush()


# ======================================================================= database invariants
async def test_a_valid_event_row_is_stored(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    await raw_event(stack, room, await create_staff(stack, "Teacher Raw"))


@pytest.mark.parametrize(
    "values",
    [
        {"source": "STAFF_RECOGNITION"},
        {"source": "VISION"},
        {"event_type": "SEEN_ON_CAMERA"},
        {"sequence": 0},
        {"valid_until": None},
        {"valid_until": "too_long"},
        {"valid_until": "too_short"},
        {"event_type": "CHECKED_OUT"},  # a check-out cannot carry a lease
        {"occurred_at": "future"},
        {"checked_in_at": "after"},
    ],
)
async def test_the_database_refuses_invalid_events(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    room = await roster_room(stack)
    staff = await create_staff(stack, "Teacher Check")
    now = datetime.now(UTC)
    resolved = dict(values)
    if resolved.get("valid_until") == "too_long":
        resolved.update(occurred_at=now, checked_in_at=now, valid_until=now + timedelta(hours=5))
    elif resolved.get("valid_until") == "too_short":
        resolved.update(occurred_at=now, checked_in_at=now, valid_until=now + timedelta(seconds=5))
    elif resolved.get("occurred_at") == "future":
        later = now + timedelta(minutes=10)
        resolved.update(
            occurred_at=later, checked_in_at=later, valid_until=later + timedelta(minutes=5)
        )
    elif resolved.get("checked_in_at") == "after":
        resolved.update(checked_in_at=now + timedelta(minutes=1))
    with pytest.raises(DBAPIError, match="check constraint|violates"):
        await raw_event(stack, room, staff, **resolved)


async def test_a_facility_classroom_mismatch_cannot_be_stored(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await create_staff(stack, "Teacher Mismatch")
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_event(stack, room, staff, facility_id=stack["f2"])


async def test_another_tenants_staff_or_room_cannot_be_referenced(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await create_staff(stack, "Teacher Tenant")
    with pytest.raises(DBAPIError, match="foreign key|row-level security"):
        await raw_event(stack, room, staff, tenant_id=stack["tenant_b"], facility_id=stack["fb"])
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_event(stack, room, str(uuid4()))


async def test_one_sequence_position_per_person_is_the_concurrency_guarantee(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await roster_room(stack)
    other = await roster_room(stack, name="Room 2")
    staff = await create_staff(stack, "Teacher Sequence")
    await raw_event(stack, room, staff, sequence=1)
    with pytest.raises(DBAPIError, match="uq_staff_presence_events_staff_sequence"):
        await raw_event(stack, other, staff, sequence=1)


async def test_only_one_active_designation_per_person_and_facility(stack: dict[str, Any]) -> None:  # noqa: F811
    staff = await teacher(stack, "Teacher Unique")
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        actor = await session.scalar(text("SELECT id FROM actors ORDER BY id LIMIT 1"))
        session.add(
            StaffRatioEligibility(
                id=uuid4(),
                tenant_id=stack["tenant_a"],
                facility_id=stack["f1"],
                staff_profile_id=UUID(staff),
                status="ACTIVE",
                counts_toward_ratio=False,
                effective_from=datetime.now(UTC),
                revision=1,
                created_by_actor_id=actor,
            )
        )
        with pytest.raises(DBAPIError, match="uq_staff_ratio_eligibility_active"):
            await session.flush()


async def test_events_are_append_only_for_the_runtime_role(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Append")
    assert (await act(stack, room, "check-in", staff)).status_code == 201
    for statement in (
        "UPDATE staff_presence_events SET valid_until = valid_until + interval '1 hour'",
        "DELETE FROM staff_presence_events",
        "DELETE FROM staff_ratio_eligibility",
    ):
        async with stack["runtime"]() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(stack["tenant_a"])},
            )
            with pytest.raises(DBAPIError, match="permission denied"):
                async with session.begin_nested():
                    await session.execute(text(statement))
    assert len(await events_of(stack, staff)) == 1


async def test_rls_isolates_tenants_and_fails_closed(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher RLS")
    await act(stack, room, "check-in", staff)
    for tenant in (None, stack["tenant_b"]):
        async with stack["runtime"]() as session, session.begin():
            if tenant is not None:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
                )
            for table in (EVENTS, ELIGIBILITY):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0  # noqa: S608


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        (EVENTS, {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False}),
        (ELIGIBILITY, {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": False}),
    ],
)
async def test_runtime_grants_are_minimal_and_public_has_none(
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
    assert public_grants == 0
    assert forced is True


# ============================================================================ eligibility API
async def test_an_admin_designates_an_active_staff_member(stack: dict[str, Any]) -> None:  # noqa: F811
    staff = await create_staff(stack, "Teacher Alice")
    response = await designate(stack, staff, True, note="Owner staffing plan, rev 2")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["counts_toward_ratio"] is True and body["status"] == "ACTIVE"
    assert body["in_effect"] is True and body["revision"] == 1
    assert body["eligibility_basis"] == "OPERATOR_DESIGNATED"
    assert body["effective_from_date"] == TODAY.isoformat()
    assert body["staff_display_name"] == "Teacher Alice"
    listed = await stack["client"].get(
        f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility", headers=stack["viewer-1"]
    )
    assert listed.status_code == 200
    assert [item["eligibility_id"] for item in listed.json()["assignments"]] == [
        body["eligibility_id"]
    ]
    assert listed.json()["can_administer"] is False
    filtered = await stack["client"].get(
        f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility",
        params={"staff_profile_id": str(uuid4())},
        headers=stack["viewer-1"],
    )
    assert filtered.json()["assignments"] == []


async def test_inactive_and_deleted_staff_cannot_be_designated(stack: dict[str, Any]) -> None:  # noqa: F811
    inactive = await create_staff(stack, "Teacher Inactive")
    await stack["client"].post(f"/v1/staff/{inactive}/deactivate", headers=stack["owner-a"])
    response = await designate(stack, inactive)
    assert response.status_code == 409
    assert response.json()["detail"]["category"] == "staff_not_active"
    deleted = await create_staff(stack, "Teacher Deleted")
    await stack["client"].delete(f"/v1/staff/{deleted}", headers=stack["owner-a"])
    assert (await designate(stack, deleted)).status_code == 404
    assert (await designate(stack, str(uuid4()))).status_code == 404


async def test_cross_tenant_and_other_facility_designations_are_404(stack: dict[str, Any]) -> None:  # noqa: F811
    staff = await create_staff(stack, "Teacher Scope")
    # Another tenant's owner, at its own facility, naming this tenant's staff profile.
    foreign = await designate(stack, staff, facility="fb", as_="owner-b")
    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "not found"}
    # This tenant's owner naming another tenant's facility.
    assert (await designate(stack, staff, facility="fb", as_="owner-a")).status_code == 404
    # A facility admin of facility one cannot see facility two at all.
    assert (await designate(stack, staff, facility="f2", as_="admin-1")).status_code == 404
    # A viewer of facility two can see it but may not change it.
    assert (await designate(stack, staff, facility="f2", as_="viewer-2")).status_code == 403
    listed = await stack["client"].get(
        f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility", headers=stack["owner-b"]
    )
    assert listed.status_code == 404


async def test_a_second_active_designation_is_refused(stack: dict[str, Any]) -> None:  # noqa: F811
    staff = await teacher(stack, "Teacher Twice")
    again = await designate(stack, staff, False)
    assert again.status_code == 409
    assert again.json()["detail"]["category"] == "eligibility_exists"


async def test_update_and_deactivate_keep_history(stack: dict[str, Any]) -> None:  # noqa: F811
    staff = await create_staff(stack, "Teacher History")
    created = (await designate(stack, staff, True)).json()
    url = f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility/{created['eligibility_id']}"
    changed = await stack["client"].patch(
        url, json={"counts_toward_ratio": False}, headers=stack["admin-1"]
    )
    assert changed.status_code == 200
    assert (changed.json()["counts_toward_ratio"], changed.json()["revision"]) == (False, 2)
    unchanged = await stack["client"].patch(
        url, json={"counts_toward_ratio": False}, headers=stack["admin-1"]
    )
    assert unchanged.json()["revision"] == 2, "a no-op change is not a revision"
    first = await stack["client"].post(f"{url}/deactivate", headers=stack["admin-1"])
    second = await stack["client"].post(f"{url}/deactivate", headers=stack["admin-1"])
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "INACTIVE" and first.json()["in_effect"] is False
    assert second.json()["revision"] == first.json()["revision"] == 3
    refused = await stack["client"].patch(
        url, json={"counts_toward_ratio": True}, headers=stack["admin-1"]
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["category"] == "eligibility_inactive"
    # A new designation is a new row; the old one stays.
    assert (await designate(stack, staff, True)).status_code == 201
    listed = await stack["client"].get(
        f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility", headers=stack["admin-1"]
    )
    assert sorted(item["status"] for item in listed.json()["assignments"]) == [
        "ACTIVE",
        "INACTIVE",
    ]


@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        ({"counts_toward_ratio": "yes"}, 422),
        ({"counts_toward_ratio": 1}, 422),
        ({"note": "<script>"}, 422),
        ({"legally_qualified": True}, 422),
        ({"licence_number": "X"}, 422),
    ],
)
async def test_malformed_designations_are_refused(
    stack: dict[str, Any],  # noqa: F811
    body: dict[str, Any],
    status_code: int,
) -> None:
    staff = await create_staff(stack, "Teacher Malformed")
    payload = {"staff_profile_id": staff, "counts_toward_ratio": True, **body}
    response = await stack["client"].post(
        f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility",
        json=payload,
        headers=stack["admin-1"],
    )
    assert response.status_code == status_code


async def test_viewers_cannot_designate_or_check_in(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Viewer")
    assert (await designate(stack, staff, as_="viewer-1")).status_code == 403
    for action in ("check-in", "refresh", "check-out"):
        assert (await act(stack, room, action, staff, as_="viewer-1")).status_code == 403
    assert (await set_mode(stack, room, MANUAL, as_="viewer-1")).status_code == 403
    readable = await stack["client"].get(
        f"/v1/classrooms/{room}/staff-presence", headers=stack["viewer-1"]
    )
    assert readable.status_code == 200


# =============================================================================== presence API
async def test_check_in_and_check_out(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Bea")
    response = await act(stack, room, "check-in", staff)
    assert response.status_code == 201, response.text
    body = response.json()
    mine = entry(body, staff)
    assert (mine["state"], mine["location"], mine["counted"]) == ("PRESENT", "HERE", True)
    lease = datetime.fromisoformat(mine["valid_until"]) - datetime.fromisoformat(
        mine["checked_in_at"]
    )
    assert lease == timedelta(minutes=15), "default lease is fifteen minutes"
    assert body["summary"]["count"] == 1 and body["summary"]["source"] == "STAFF_ROSTER"
    out = await act(stack, room, "check-out", staff)
    assert out.status_code == 200
    assert entry(out.json(), staff)["state"] == "NOT_CHECKED_IN"
    assert out.json()["summary"]["count"] == 0
    assert [row.event_type for row in await events_of(stack, staff)] == [
        "CHECKED_IN",
        "CHECKED_OUT",
    ]


async def test_duplicates_are_deterministic(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Dup")
    assert (await act(stack, room, "check-in", staff)).status_code == 201
    again = await act(stack, room, "check-in", staff)
    assert again.status_code == 409
    assert again.json()["detail"]["category"] == "staff_already_checked_in"
    assert (await act(stack, room, "check-out", staff)).status_code == 200
    repeat = await act(stack, room, "check-out", staff)
    assert repeat.status_code == 200, "a repeated check-out is idempotent"
    assert len(await events_of(stack, staff)) == 2, "and appends nothing"


async def test_room_transition_is_atomic_and_leaves_one_current_room(stack: dict[str, Any]) -> None:  # noqa: F811
    room_a = await roster_room(stack)
    room_b = await roster_room(stack, name="Room 2")
    staff = await teacher(stack, "Teacher Mover")
    await act(stack, room_a, "check-in", staff)
    moved = await act(stack, room_b, "check-in", staff)
    assert moved.status_code == 201
    rows = await events_of(stack, staff)
    assert [(row.sequence, row.event_type, str(row.area_id)) for row in rows] == [
        (1, "CHECKED_IN", room_a),
        (2, "CHECKED_OUT", room_a),
        (3, "CHECKED_IN", room_b),
    ]
    assert rows[1].occurred_at == rows[2].occurred_at, "one transaction, one moment"
    view_a = (
        await stack["client"].get(
            f"/v1/classrooms/{room_a}/staff-presence", headers=stack["admin-1"]
        )
    ).json()
    elsewhere = entry(view_a, staff)
    assert (elsewhere["location"], elsewhere["other_classroom_name"]) == (
        "OTHER_CLASSROOM",
        "Room 2",
    )
    assert view_a["summary"]["count"] == 0
    assert entry(moved.json(), staff)["location"] == "HERE"
    wrong_room = await act(stack, room_a, "check-out", staff)
    assert wrong_room.status_code == 409
    assert wrong_room.json()["detail"]["category"] == "staff_in_another_classroom"


async def test_a_present_stay_in_another_facility_blocks_check_in(stack: dict[str, Any]) -> None:  # noqa: F811
    here = await roster_room(stack)
    there = str(
        (await create_classroom(stack, "f2", as_="owner-a", name="Far room"))["classroom_id"]
    )
    staff = await teacher(stack, "Teacher Two Sites")
    assert (await designate(stack, staff, True, facility="f2", as_="owner-a")).status_code == 201
    assert (await act(stack, there, "check-in", staff, as_="owner-a")).status_code == 201
    refused = await act(stack, here, "check-in", staff, as_="owner-a")
    assert refused.status_code == 409
    assert refused.json()["detail"]["category"] == "staff_checked_in_elsewhere"
    view = (
        await stack["client"].get(f"/v1/classrooms/{here}/staff-presence", headers=stack["admin-1"])
    ).json()
    other = entry(view, staff)
    assert other["location"] == "OTHER_FACILITY"
    assert other["other_classroom_id"] is None and other["valid_until"] is None


async def test_check_in_requires_an_active_profile_designation_and_room(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await roster_room(stack)
    unassigned = await create_staff(stack, "Teacher Unassigned")
    refused = await act(stack, room, "check-in", unassigned)
    assert refused.json()["detail"]["category"] == "staff_not_assigned_to_facility"
    future = await create_staff(stack, "Teacher Future")
    await designate(stack, future, effective_from_date=(TODAY + timedelta(days=2)).isoformat())
    refused = await act(stack, room, "check-in", future)
    assert refused.json()["detail"]["category"] == "staff_not_assigned_to_facility"
    lapsed = await create_staff(stack, "Teacher Lapsed")
    await designate(
        stack,
        lapsed,
        effective_from_date=(TODAY - timedelta(days=5)).isoformat(),
        effective_through_date=(TODAY - timedelta(days=2)).isoformat(),
    )
    refused = await act(stack, room, "check-in", lapsed)
    assert refused.json()["detail"]["category"] == "staff_not_assigned_to_facility"
    inactive = await teacher(stack, "Teacher Paused")
    await stack["client"].post(f"/v1/staff/{inactive}/deactivate", headers=stack["owner-a"])
    refused = await act(stack, room, "check-in", inactive)
    assert refused.status_code == 409
    assert refused.json()["detail"]["category"] == "staff_not_active"
    await stack["client"].post(f"/v1/classrooms/{room}/deactivate", headers=stack["admin-1"])
    active = await teacher(stack, "Teacher Closed Room")
    refused = await act(stack, room, "check-in", active)
    assert refused.json()["detail"]["category"] == "classroom_inactive"
    assert (await act(stack, room, "check-in", str(uuid4()))).status_code == 404


@pytest.mark.parametrize("lease", [30, 4 * 3600 + 1, 86400])
async def test_excessive_or_short_leases_are_422(stack: dict[str, Any], lease: int) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Lease")
    response = await act(stack, room, "check-in", staff, lease_seconds=lease)
    assert response.status_code == 422
    assert response.json()["detail"]["category"] == "invalid_lease_seconds"


@pytest.mark.parametrize(
    "extra",
    [
        {"lease_seconds": "900"},
        {"occurred_at": "2030-01-01T00:00:00Z"},
        {"face_match_id": "x"},
        {"track_id": 3},
        {"image": "AAAA"},
    ],
)
async def test_presence_requests_accept_no_time_image_or_track(
    stack: dict[str, Any],  # noqa: F811
    extra: dict[str, Any],
) -> None:
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Strict")
    assert (await act(stack, room, "check-in", staff, **extra)).status_code == 422


async def test_refresh_extends_the_lease_and_keeps_the_start(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Refresh")
    first = entry((await act(stack, room, "check-in", staff, lease_seconds=300)).json(), staff)
    refreshed = await act(stack, room, "refresh", staff, lease_seconds=3600)
    assert refreshed.status_code == 200
    after = entry(refreshed.json(), staff)
    assert after["checked_in_at"] == first["checked_in_at"]
    assert datetime.fromisoformat(after["valid_until"]) > datetime.fromisoformat(
        first["valid_until"]
    )
    assert [row.event_type for row in await events_of(stack, staff)] == ["CHECKED_IN", "REFRESHED"]
    assert (await act(stack, room, "refresh", staff, lease_seconds=99999)).status_code == 422
    nobody = await teacher(stack, "Teacher Not Here")
    refused = await act(stack, room, "refresh", nobody)
    assert refused.json()["detail"]["category"] == "staff_not_checked_in"


async def test_other_tenants_and_unreadable_facilities_get_404(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Hidden")
    for who in ("owner-b", "viewer-2"):
        read = await stack["client"].get(
            f"/v1/classrooms/{room}/staff-presence", headers=stack[who]
        )
        assert read.status_code == 404
        for action in ("check-in", "refresh", "check-out"):
            response = await act(stack, room, action, staff, as_=who)
            assert response.status_code in (403, 404)
            if who == "owner-b":
                assert response.status_code == 404
    # A tenant-B room with a tenant-A staff id.
    other = str((await create_classroom(stack, "fb", as_="owner-b", name="B room"))["classroom_id"])
    assert (await act(stack, other, "check-in", staff, as_="owner-b")).status_code == 404


# ============================================================================= ratio status
async def test_default_mode_is_manual_and_the_switch_is_explicit_and_audited(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await classroom_with_policy(stack)
    detail = (await stack["client"].get(f"/v1/classrooms/{room}", headers=stack["viewer-1"])).json()
    assert detail["presence_source_mode"] == MANUAL
    staff = await teacher(stack, "Teacher Mode")
    await act(stack, room, "check-in", staff)
    await report(stack, room, 6, 0)
    result = await status(stack, room)
    assert result["presence_source_mode"] == MANUAL
    assert result["evaluation"]["staff_count"] == 0, "manual mode ignores the roster"
    assert result["sources"]["qualified_staff"]["source"] == "MANUAL"
    assert (await set_mode(stack, room, "VISION")).status_code == 422
    assert (await set_mode(stack, room, ROSTER)).status_code == 200
    assert (await set_mode(stack, room, ROSTER)).status_code == 200  # idempotent
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        changes = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.target_id == UUID(room),
                    AuditEvent.action == "classroom.presence_source_mode_changed",
                )
            )
        ).all()
    assert [event.metadata_ for event in changes] == [{"from": MANUAL, "to": ROSTER}]


async def test_six_children_one_then_two_roster_staff(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    alice = await teacher(stack, "Teacher A")
    bob = await teacher(stack, "Teacher B")
    carol = await teacher(stack, "Assistant C", counts=False)
    await children(stack, room, 6)
    await act(stack, room, "check-in", alice)
    await act(stack, room, "check-in", carol)
    result = await status(stack, room)
    evaluation = result["evaluation"]
    assert evaluation["ratio_state"] == "OVER_CONFIGURED_RATIO"
    assert (evaluation["child_count"], evaluation["staff_count"]) == (6, 1)
    assert (evaluation["required_staff"], evaluation["staff_deficit"]) == (2, 1)
    sources = result["sources"]
    assert sources["mode"] == ROSTER
    assert (sources["children"]["count"], sources["children"]["source"]) == (6, "MANUAL")
    assert (sources["qualified_staff"]["count"], sources["qualified_staff"]["source"]) == (
        1,
        "STAFF_ROSTER",
    )
    assert sources["visitors"]["source"] == "MANUAL"
    assert sources["staff_roster"]["present"] == 2
    assert sources["staff_roster"]["present_ratio_ineligible"] == 1
    await act(stack, room, "check-in", bob)
    result = await status(stack, room)
    assert result["evaluation"]["ratio_state"] == "WITHIN_CONFIGURED_POLICY"
    assert (result["evaluation"]["staff_count"], result["evaluation"]["staff_deficit"]) == (2, 0)


async def test_no_roster_staff_is_over_ratio_and_stale_children_insufficient(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await roster_room(stack)
    missing = await status(stack, room)
    assert missing["evaluation"]["ratio_state"] == "INSUFFICIENT_DATA"
    assert "CHILD_COUNT_MISSING" in missing["evaluation"]["explanations"]
    await children(stack, room, 3)
    zero = await status(stack, room)
    assert zero["evaluation"]["ratio_state"] == "OVER_CONFIGURED_RATIO"
    assert zero["evaluation"]["staff_count"] == 0
    assert "NO_QUALIFIED_STAFF_PRESENT" in zero["evaluation"]["explanations"]
    staff = await teacher(stack, "Teacher Stale Kids")
    await act(stack, room, "check-in", staff, lease_seconds=3600)
    later = await status_at(stack, room, datetime.now(UTC) + timedelta(minutes=5))
    assert later["evaluation"]["ratio_state"] == "INSUFFICIENT_DATA"
    assert "CHILD_COUNT_STALE" in later["evaluation"]["explanations"]
    assert later["evaluation"]["required_staff"] is None, "no previous safe state carried"
    assert later["sources"]["children"]["count"] is None


async def test_stale_or_checked_out_roster_staff_do_not_count(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    staff = await teacher(stack, "Teacher Lapse")
    await act(stack, room, "check-in", staff, lease_seconds=60)
    await children(stack, room, 2)
    assert (await status(stack, room))["evaluation"]["staff_count"] == 1
    later = await status_at(stack, room, datetime.now(UTC) + timedelta(seconds=90))
    assert later["sources"]["staff_roster"]["count"] == 0
    assert later["sources"]["staff_roster"]["stale"] == 1
    roster_service = stack["client"]._transport.app.state.staff_roster_service
    view = await roster_service.get_staff_presence(
        await principal(stack), UUID(room), now=datetime.now(UTC) + timedelta(seconds=90)
    )
    assert [item.state for item in view.staff] == ["STALE"]
    await act(stack, room, "check-out", staff)
    assert (await status(stack, room))["evaluation"]["staff_count"] == 0


async def test_deactivating_a_profile_or_designation_stops_counting(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    first = await teacher(stack, "Teacher Deactivated")
    second = await create_staff(stack, "Teacher Undesignated")
    designation = (await designate(stack, second, True)).json()
    await act(stack, room, "check-in", first)
    await act(stack, room, "check-in", second)
    await children(stack, room, 2)
    assert (await status(stack, room))["evaluation"]["staff_count"] == 2
    await stack["client"].post(f"/v1/staff/{first}/deactivate", headers=stack["owner-a"])
    await stack["client"].post(
        f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility/"
        f"{designation['eligibility_id']}/deactivate",
        headers=stack["admin-1"],
    )
    result = await status(stack, room)
    assert result["evaluation"]["staff_count"] == 0
    assert result["sources"]["staff_roster"]["present_inactive"] == 1
    assert result["sources"]["staff_roster"]["present_ratio_ineligible"] == 1


async def test_another_classrooms_staff_do_not_count_here(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await roster_room(stack)
    other = await roster_room(stack, name="Room 2")
    staff = await teacher(stack, "Teacher Next Door")
    await act(stack, other, "check-in", staff)
    await children(stack, room, 1)
    assert (await status(stack, room))["evaluation"]["staff_count"] == 0


async def test_roster_mode_refuses_a_manual_staff_number_and_manual_mode_requires_one(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await roster_room(stack)
    refused = await report(stack, room, 6, 2)
    assert refused.status_code == 409
    assert refused.json()["detail"]["category"] == "staff_count_comes_from_roster"
    manual_room = str((await create_classroom(stack, name="Manual room"))["classroom_id"])
    missing = await stack["client"].post(
        f"/v1/classrooms/{manual_room}/presence/manual",
        json={"child_count": 6},
        headers=stack["admin-1"],
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["category"] == "invalid_qualified_staff_count"


async def test_a_legacy_manual_staff_number_is_never_added_to_the_roster(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await classroom_with_policy(stack)
    await report(stack, room, 6, 4)  # made in manual mode, carries 4 staff
    assert (await status(stack, room))["evaluation"]["staff_count"] == 4
    await set_mode(stack, room, ROSTER)
    staff = await teacher(stack, "Teacher Only One")
    await act(stack, room, "check-in", staff)
    result = await status(stack, room)
    assert result["evaluation"]["staff_count"] == 1, "roster only: not 4 and not 5"
    assert result["presence"]["qualified_staff_count"] is None, "the manual number is not shown"
    assert result["presence"]["child_count"] == 6
    # Back to manual: the latest report still says 4; nothing from the roster is borrowed.
    await set_mode(stack, room, MANUAL)
    assert (await status(stack, room))["evaluation"]["staff_count"] == 4
    # A roster-mode report has no staff number, so manual mode then reads it as missing.
    await set_mode(stack, room, ROSTER)
    await children(stack, room, 6)
    await set_mode(stack, room, MANUAL)
    result = await status(stack, room)
    assert result["evaluation"]["ratio_state"] == "INSUFFICIENT_DATA"
    assert "STAFF_COUNT_MISSING" in result["evaluation"]["explanations"]


# ============================================================================== concurrency
async def test_simultaneous_check_ins_never_leave_a_person_in_two_rooms(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    rooms = [await roster_room(stack), await roster_room(stack, name="Room 2")]
    staff = await teacher(stack, "Teacher Race")
    responses = await asyncio.gather(
        *(act(stack, rooms[index % 2], "check-in", staff) for index in range(8))
    )
    assert all(response.status_code in (201, 409) for response in responses)
    rows = await events_of(stack, staff)
    assert [row.sequence for row in rows] == list(range(1, len(rows) + 1)), "no gaps, no forks"
    views = [
        (
            await stack["client"].get(
                f"/v1/classrooms/{room}/staff-presence", headers=stack["admin-1"]
            )
        ).json()
        for room in rooms
    ]
    here = [entry(view, staff)["location"] == "HERE" for view in views]
    assert here.count(True) == 1, "exactly one current room"
    assert sum(view["summary"]["count"] for view in views) == 1
    # Every open stay was closed before the next began.
    open_rooms = 0
    for row in rows:
        open_rooms = open_rooms + 1 if row.event_type == "CHECKED_IN" else open_rooms - 1
        assert open_rooms <= 1
        open_rooms = max(open_rooms, 0)


# ==================================================================================== audit
async def test_roster_actions_are_audited_with_ids_only(stack: dict[str, Any]) -> None:  # noqa: F811
    room_a = await roster_room(stack)
    room_b = await roster_room(stack, name="Room 2")
    staff = await create_staff(stack, "Teacher Audited Person")
    designation = (await designate(stack, staff, True, note="Private operator note")).json()
    url = f"/v1/facilities/{stack['f1']}/staff-ratio-eligibility/{designation['eligibility_id']}"
    await stack["client"].patch(url, json={"note": None}, headers=stack["admin-1"])
    await act(stack, room_a, "check-in", staff)
    await act(stack, room_a, "refresh", staff)
    await act(stack, room_b, "check-in", staff)
    await act(stack, room_b, "check-out", staff)
    await act(stack, room_b, "check-out", staff)  # idempotent: not audited again
    await stack["client"].post(f"{url}/deactivate", headers=stack["admin-1"])
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        events = (
            await session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.target_type.in_(["staff_ratio_eligibility", "staff_presence_event"])
                )
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        ).all()
    mine = [
        event
        for event in events
        if event.metadata_.get("staff_profile_id") == staff
        or event.metadata_.get("before", {}).get("staff_profile_id") == staff
    ]
    assert [event.action for event in mine] == [
        "staff_eligibility.created",
        "staff_eligibility.updated",
        "staff_presence.checked_in",
        "staff_presence.refreshed",
        "staff_presence.moved",
        "staff_presence.checked_out",
        "staff_eligibility.deactivated",
    ]
    moved = mine[4].metadata_
    assert (moved["from_classroom_id"], moved["classroom_id"]) == (room_a, room_b)
    assert moved["source"] == "STAFF_ROSTER" and len(moved["event_ids"]) == 2
    checked_in = mine[2].metadata_
    assert checked_in["lease_seconds"] == 900 and checked_in["counts_toward_ratio"] is True
    assert mine[1].metadata_["before"]["note_present"] is True
    assert mine[1].metadata_["after"]["note_present"] is False
    rendered = str([event.metadata_ for event in mine]).lower()
    for forbidden in (
        "teacher audited person",
        "private operator note",
        "token",
        "bearer",
        "auth0",
        "image",
        "frame",
        "track",
        "embedding",
        "template",
        "face",
    ):
        assert forbidden not in rendered, forbidden
