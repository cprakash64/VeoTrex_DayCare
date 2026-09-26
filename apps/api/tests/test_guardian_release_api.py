"""Guardian contacts, child associations and authorized release over HTTP and in PostgreSQL
(V1-04E).

Reuses the V1-04A two-tenant stack (a tenant owner, a facility admin and a viewer on facility 1,
a viewer on facility 2, another tenant) and the V1-04D child helpers. Proves the tables' own
invariants, the contact / link / release APIs, pickup-authorization semantics, the atomic release
transaction, concurrency, the administrative check-out distinction, isolation and audit contents.
Every adult and child here is synthetic ("Adult P1", "Child A"); nothing has a photo, a face, an
identity document or a camera track.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_child_roster_api import (
    ATTENDANCE_MODE,
    attend,
    attendance_room,
    attendance_view,
    audits,
    child,
    entry,
    events_of,
    lifecycle,
    raw_event,
)
from test_classroom_api import create_classroom, stack  # noqa: F401 - fixture
from test_classroom_presence_api import status
from test_staff_roster_api import act, teacher

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.classroom_service import ClassroomError
from veotrex_api.guardian_service import GuardianService
from veotrex_api.models import (
    ChildAttendanceEvent,
    ChildGuardianLink,
    ChildReleaseEvent,
    GuardianContact,
)

CONTACTS = "guardian_contacts"
LINKS = "child_guardian_links"
RELEASES = "child_release_events"
PAST = datetime(2026, 1, 1, tzinfo=UTC)


# ================================================================================ helpers
async def add_guardian(
    stack: dict[str, Any],  # noqa: F811
    name: str,
    *,
    facility: str = "f1",
    as_: str = "admin-1",
    **extra: Any,
) -> Any:
    return await stack["client"].post(
        f"/v1/facilities/{stack[facility]}/guardians",
        json={"display_name": name, **extra},
        headers=stack[as_],
    )


async def guardian(stack: dict[str, Any], name: str, **kwargs: Any) -> str:  # noqa: F811
    response = await add_guardian(stack, name, **kwargs)
    assert response.status_code == 201, response.text
    return str(response.json()["guardian_contact_id"])


async def add_link(
    stack: dict[str, Any],  # noqa: F811
    kid: str,
    contact: str,
    label: str = "Mother",
    pickup: Any = True,
    *,
    as_: str = "admin-1",
    **extra: Any,
) -> Any:
    return await stack["client"].post(
        f"/v1/children/{kid}/guardians",
        json={
            "guardian_contact_id": contact,
            "relationship_label": label,
            "pickup_authorized": pickup,
            **extra,
        },
        headers=stack[as_],
    )


def link_of(body: dict[str, Any], contact: str, status_: str = "ACTIVE") -> dict[str, Any]:
    return next(
        item
        for item in body["links"]
        if item["guardian_contact_id"] == contact and item["status"] == status_
    )


async def link(stack: dict[str, Any], kid: str, contact: str, *args: Any, **kwargs: Any) -> str:  # noqa: F811
    response = await add_link(stack, kid, contact, *args, **kwargs)
    assert response.status_code == 201, response.text
    return str(link_of(response.json(), contact)["link_id"])


async def patch_link(
    stack: dict[str, Any],  # noqa: F811
    kid: str,
    link_id: str,
    as_: str = "admin-1",
    **changes: Any,
) -> Any:
    return await stack["client"].patch(
        f"/v1/children/{kid}/guardians/{link_id}", json=changes, headers=stack[as_]
    )


async def child_links(stack: dict[str, Any], kid: str, as_: str = "admin-1") -> Any:  # noqa: F811
    response = await stack["client"].get(f"/v1/children/{kid}/guardians", headers=stack[as_])
    assert response.status_code == 200, response.text
    return response.json()


async def release(
    stack: dict[str, Any],  # noqa: F811
    room: str,
    kid: str,
    contact: str,
    method: str | None = "OPERATOR_CONFIRMED",
    *,
    as_: str = "admin-1",
    **extra: Any,
) -> Any:
    body: dict[str, Any] = {"child_profile_id": kid, "guardian_contact_id": contact, **extra}
    if method is not None:
        body["verification_method"] = method
    return await stack["client"].post(
        f"/v1/classrooms/{room}/attendance/release", json=body, headers=stack[as_]
    )


def category(response: Any) -> str:
    return str(response.json()["detail"]["category"])


async def rows(stack: dict[str, Any], model: Any, **where: Any) -> list[Any]:  # noqa: F811
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        statement = select(model)
        for column, value in where.items():
            statement = statement.where(getattr(model, column) == value)
        return list((await session.scalars(statement)).all())


async def admin_principal(stack: dict[str, Any]) -> AuthenticatedPrincipal:  # noqa: F811
    me = (await stack["client"].get("/v1/me", headers=stack["admin-1"])).json()
    return AuthenticatedPrincipal(
        issuer="",
        subject="",
        external_organization_id="",
        actor_id=UUID(me["actor_id"]),
        tenant_id=UUID(me["tenant_id"]),
        display_name=None,
        grants=(RoleGrant(Role.FACILITY_ADMIN, stack["f1"]),),
        permissions=frozenset({Permission.READ_OPERATIONAL, Permission.ADMINISTER_FACILITY}),
    )


def service_of(stack: dict[str, Any]) -> GuardianService:  # noqa: F811
    return stack["client"]._transport.app.state.guardian_service  # type: ignore[no-any-return]


async def checked_in_child(stack: dict[str, Any], name: str = "Child A") -> tuple[str, str]:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, name)
    assert (await attend(stack, room, "check-in", kid)).status_code == 201
    return room, kid


async def raw_actor(session: Any, tenant: UUID) -> Any:
    return await session.scalar(
        text("SELECT id FROM actors WHERE tenant_id = :t ORDER BY id LIMIT 1"), {"t": tenant}
    )


async def raw_contact(stack: dict[str, Any], **values: Any) -> UUID:  # noqa: F811
    tenant = stack["tenant_a"]
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
        )
        row: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": tenant,
            "facility_id": stack["f1"],
            "display_name": "Adult Raw",
            "status": "ACTIVE",
            "created_by_actor_id": await raw_actor(session, tenant),
        }
        row.update(values)
        session.add(GuardianContact(**row))
        await session.flush()
        return UUID(str(row["id"]))


async def raw_link(stack: dict[str, Any], kid: str, contact: str, **values: Any) -> None:  # noqa: F811
    tenant = stack["tenant_a"]
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
        )
        row: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": tenant,
            "facility_id": stack["f1"],
            "child_profile_id": UUID(kid),
            "guardian_contact_id": UUID(contact),
            "relationship_label": "Guardian",
            "pickup_authorized": True,
            "effective_from": datetime.now(UTC),
            "status": "ACTIVE",
            "revision": 1,
            "created_by_actor_id": await raw_actor(session, tenant),
        }
        row.update(values)
        session.add(ChildGuardianLink(**row))
        await session.flush()


async def raw_release(stack: dict[str, Any], **values: Any) -> None:  # noqa: F811
    tenant = stack["tenant_a"]
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
        )
        row: dict[str, Any] = {
            "id": uuid4(),
            "tenant_id": tenant,
            "authorization_link_revision": 1,
            "verification_method": "OPERATOR_CONFIRMED",
            "attendance_event_type": "CHECKED_OUT",
            "recorded_by_actor_id": await raw_actor(session, tenant),
        }
        row.update(values)
        session.add(ChildReleaseEvent(**row))
        await session.flush()


# ======================================================================= database invariants
@pytest.mark.parametrize(
    "values",
    [
        {"status": "DELETED"},
        {"display_name": ""},
        {"display_name": "x" * 121},
        {"display_name": "Adult\tA"},
        {"display_name": " padded"},
        {"display_name": "<b>A</b>"},
        {"external_reference": "has space"},
    ],
)
async def test_the_database_refuses_invalid_contacts(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    with pytest.raises(DBAPIError, match="check constraint|violates|too long"):
        await raw_contact(stack, **values)


@pytest.mark.parametrize(
    "values",
    [
        {"status": "PENDING"},
        {"relationship_label": ""},
        {"relationship_label": "x" * 65},
        {"relationship_label": "Mother<script>"},
        {"note": "line\nbreak"},
        {"revision": 0},
        {"effective_until": datetime.now(UTC) - timedelta(days=1)},
        {"status": "INACTIVE"},  # deactivated without when/by
    ],
)
async def test_the_database_refuses_invalid_links(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    kid = await child(stack, "Child Invalid")
    contact = await guardian(stack, "Adult Invalid")
    with pytest.raises(DBAPIError, match="check constraint|violates|too long"):
        await raw_link(stack, kid, contact, **values)


async def test_one_active_link_per_pair_and_no_cross_facility_link(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    kid = await child(stack, "Child Pair")
    contact = await guardian(stack, "Adult Pair")
    await raw_link(stack, kid, contact)
    with pytest.raises(DBAPIError, match="uq_child_guardian_links_active"):
        await raw_link(stack, kid, contact)
    far = await guardian(stack, "Adult Far", facility="f2", as_="owner-a")
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_link(stack, kid, far)
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_link(stack, kid, far, facility_id=stack["f2"])


async def test_a_release_row_must_name_its_exact_check_out(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult Exact")
    link_id = await link(stack, kid, contact)
    check_in = (await events_of(stack, kid))[0]
    base = {
        "facility_id": stack["f1"],
        "area_id": UUID(room),
        "child_profile_id": UUID(kid),
        "guardian_contact_id": UUID(contact),
        "authorization_link_id": UUID(link_id),
        "attendance_event_id": check_in.id,
        "released_at": check_in.occurred_at,
    }
    # A CHECKED_IN event cannot be recorded as a release, whatever the type column claims.
    with pytest.raises(DBAPIError, match="check constraint"):
        await raw_release(stack, **base, attendance_event_type="CHECKED_IN")
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_release(stack, **base)
    assert (await attend(stack, room, "check-out", kid)).status_code == 200
    checkout = (await events_of(stack, kid))[-1]
    exact = {**base, "attendance_event_id": checkout.id, "released_at": checkout.occurred_at}
    other_kid = await child(stack, "Child Other")
    for wrong in (
        {"released_at": checkout.occurred_at + timedelta(seconds=1)},
        {"child_profile_id": UUID(other_kid)},
        {"authorization_link_id": uuid4()},
        {"guardian_contact_id": UUID(await guardian(stack, "Adult Unlinked"))},
    ):
        with pytest.raises(DBAPIError, match="foreign key"):
            await raw_release(stack, **{**exact, **wrong})
    with pytest.raises(DBAPIError, match="check constraint"):
        await raw_release(stack, **exact, verification_method="FACE_MATCH")
    await raw_release(stack, **exact)
    with pytest.raises(DBAPIError, match="uq_child_release_events_attendance_event"):
        await raw_release(stack, **exact)


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        (CONTACTS, {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": False}),
        (LINKS, {"SELECT": True, "INSERT": True, "UPDATE": True, "DELETE": False}),
        (RELEASES, {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False}),
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
        public = await session.scalar(
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
    assert public == 0 and forced is True


async def test_release_history_is_append_only_and_nothing_is_deleted_at_runtime(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult Append")
    await link(stack, kid, contact)
    assert (await release(stack, room, kid, contact)).status_code == 201
    for statement in (
        "UPDATE child_release_events SET verification_method = 'KNOWN_TO_STAFF'",
        "DELETE FROM child_release_events",
        "DELETE FROM child_guardian_links",
        "DELETE FROM guardian_contacts",
    ):
        async with stack["runtime"]() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"),
                {"id": str(stack["tenant_a"])},
            )
            with pytest.raises(DBAPIError, match="permission denied"):
                async with session.begin_nested():
                    await session.execute(text(statement))
    stored = await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid))
    assert [row.verification_method for row in stored] == ["OPERATOR_CONFIRMED"]


async def test_rls_isolates_tenants_and_fails_closed(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult RLS")
    await link(stack, kid, contact)
    assert (await release(stack, room, kid, contact)).status_code == 201
    for tenant in (None, stack["tenant_b"]):
        async with stack["runtime"]() as session, session.begin():
            if tenant is not None:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
                )
            for table in (CONTACTS, LINKS, RELEASES):
                assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0  # noqa: S608


# ============================================================================= contacts API
async def test_an_admin_creates_lists_reads_and_edits_a_contact(stack: dict[str, Any]) -> None:  # noqa: F811
    created = await add_guardian(stack, "  Adult   Maya ", external_reference="FAM-01")
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["display_name"], body["status"], body["external_reference"]) == (
        "Adult Maya",
        "ACTIVE",
        "FAM-01",
    )
    assert body["active_link_count"] == 0 and body["can_administer"] is True
    gid = body["guardian_contact_id"]
    listing = await stack["client"].get(
        f"/v1/facilities/{stack['f1']}/guardians", headers=stack["viewer-1"]
    )
    assert listing.status_code == 200
    assert [item["guardian_contact_id"] for item in listing.json()["guardians"]] == [gid]
    assert listing.json()["can_administer"] is False
    assert listing.json()["facility_timezone"] == "America/Phoenix"
    edited = await stack["client"].patch(
        f"/v1/guardians/{gid}",
        json={"display_name": "Adult Maya S", "external_reference": None},
        headers=stack["admin-1"],
    )
    assert edited.status_code == 200
    assert (edited.json()["display_name"], edited.json()["external_reference"]) == (
        "Adult Maya S",
        None,
    )
    read = await stack["client"].get(f"/v1/guardians/{gid}", headers=stack["viewer-1"])
    assert read.status_code == 200 and read.json()["display_name"] == "Adult Maya S"


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": ""},
        {"display_name": "x" * 121},
        {"display_name": "x" * 401},
        {"display_name": "<i>Adult</i>"},
        {"display_name": "Adult A", "external_reference": "has space"},
        {"display_name": "Adult A", "photo": "data:image/png;base64,AAAA"},
        {"display_name": "Adult A", "face_embedding": [0.1, 0.2]},
        {"display_name": "Adult A", "id_document_image": "x"},
        {"display_name": "Adult A", "government_id_number": "123-45-6789"},
        {"display_name": "Adult A", "date_of_birth": "1990-01-01"},
        {"display_name": "Adult A", "phone": "+15555550100"},
        {"display_name": "Adult A", "email": "adult@example.test"},
        {"display_name": "Adult A", "track_id": 7},
    ],
)
async def test_invalid_or_excess_contact_fields_are_422(
    stack: dict[str, Any],  # noqa: F811
    body: dict[str, Any],
) -> None:
    response = await stack["client"].post(
        f"/v1/facilities/{stack['f1']}/guardians", json=body, headers=stack["admin-1"]
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    if isinstance(detail, dict):
        # The service's own refusals carry a bounded category and never echo the input.
        assert set(detail) == {"message", "category"}
        for value in body.values():
            if isinstance(value, str) and len(value) > 3:
                assert value not in response.text


async def test_contact_lifecycle_and_terminal_archive(stack: dict[str, Any]) -> None:  # noqa: F811
    gid = await guardian(stack, "Adult Life", external_reference="FAM-LIFE")
    duplicate = await add_guardian(stack, "Adult Twin", external_reference="FAM-LIFE")
    assert duplicate.status_code == 409 and category(duplicate) == "external_reference_exists"
    # Unique per facility only.
    assert (
        await add_guardian(
            stack, "Adult F2", facility="f2", as_="owner-a", external_reference="FAM-LIFE"
        )
    ).status_code == 201
    client, admin = stack["client"], stack["admin-1"]
    for verb, expected in (
        ("deactivate", "INACTIVE"),
        ("deactivate", "INACTIVE"),
        ("activate", "ACTIVE"),
        ("archive", "ARCHIVED"),
        ("archive", "ARCHIVED"),
    ):
        response = await client.post(f"/v1/guardians/{gid}/{verb}", headers=admin)
        assert response.status_code == 200 and response.json()["status"] == expected
    for verb in ("activate", "deactivate"):
        refused = await client.post(f"/v1/guardians/{gid}/{verb}", headers=admin)
        assert refused.status_code == 409 and category(refused) == "guardian_archived"
    edit = await client.patch(f"/v1/guardians/{gid}", json={"display_name": "X"}, headers=admin)
    assert edit.status_code == 409 and category(edit) == "guardian_archived"
    # Archived, never deleted: still readable.
    assert (await client.get(f"/v1/guardians/{gid}", headers=admin)).json()["status"] == "ARCHIVED"


async def test_contact_authorization_and_uniform_404(stack: dict[str, Any]) -> None:  # noqa: F811
    gid = await guardian(stack, "Adult Scope")
    client = stack["client"]
    assert (await add_guardian(stack, "Adult V", as_="viewer-1")).status_code == 403
    assert (
        await client.post(f"/v1/guardians/{gid}/deactivate", headers=stack["viewer-1"])
    ).status_code == 403
    patched = await client.patch(
        f"/v1/guardians/{gid}", json={"display_name": "X"}, headers=stack["viewer-1"]
    )
    assert patched.status_code == 403
    for who in ("owner-b", "viewer-2"):
        read = await client.get(f"/v1/guardians/{gid}", headers=stack[who])
        assert read.status_code == 404 and read.json() == {"detail": "not found"}
        listing = await client.get(f"/v1/facilities/{stack['f1']}/guardians", headers=stack[who])
        assert listing.status_code == 404
        for verb in ("activate", "archive"):
            response = await client.post(f"/v1/guardians/{gid}/{verb}", headers=stack[who])
            assert response.status_code in (403, 404)
    unknown = await client.get(f"/v1/guardians/{uuid4()}", headers=stack["admin-1"])
    assert unknown.status_code == 404 and unknown.json() == {"detail": "not found"}
    assert (await add_guardian(stack, "Adult F2", facility="f2", as_="admin-1")).status_code == 404
    assert (await add_guardian(stack, "Adult B", facility="fb", as_="admin-1")).status_code == 404


# ================================================================================= links API
async def test_links_carry_a_label_and_an_independent_pickup_flag(stack: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Aarav")
    mother, father, friend, contact = [
        await guardian(stack, name)
        for name in ("Adult Mother", "Adult Father", "Adult Friend", "Adult Contact")
    ]
    await link(stack, kid, mother, "Mother", True)
    await link(stack, kid, father, "Father", False)
    until = datetime.now(UTC) + timedelta(days=3)
    await link(stack, kid, friend, "Family friend", True, effective_until=until.isoformat())
    await link(stack, kid, contact, "Emergency contact", False, note="per enrolment form")
    body = await child_links(stack, kid, as_="viewer-1")
    status_of = {item["guardian_contact_id"]: item for item in body["links"]}
    assert status_of[mother]["pickup_status"] == "AUTHORIZED"
    # A "Father" link without the flag does not authorize pickup: relationship is not authority.
    assert (status_of[father]["relationship_label"], status_of[father]["pickup_status"]) == (
        "Father",
        "PICKUP_NOT_AUTHORIZED",
    )
    assert status_of[friend]["pickup_status"] == "AUTHORIZED"
    assert datetime.fromisoformat(status_of[friend]["effective_until"]) == until
    assert status_of[contact]["note"] == "per enrolment form"
    assert body["can_administer"] is False and body["facility_timezone"] == "America/Phoenix"
    counts = (await stack["client"].get(f"/v1/guardians/{mother}", headers=stack["admin-1"])).json()
    assert counts["active_link_count"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"relationship_label": ""},
        {"relationship_label": "x" * 65},
        {"relationship_label": "Mother<b>"},
        {"relationship_label": "Mo" + chr(0x200B) + "ther"},
        {"pickup_authorized": "yes"},
        {"pickup_authorized": 1},
        {"pickup_authorized": None},
        {"effective_from": "2026-09-26T08:00:00"},  # no offset
        {"effective_until": "2020-01-01T00:00:00Z"},  # before effective_from (now)
        {"note": "x" * 201},
        {"kinship_score": 0.9},
        {"face_match_id": str(uuid4())},
    ],
)
async def test_invalid_links_are_422(stack: dict[str, Any], changes: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Validate")
    contact = await guardian(stack, "Adult Validate")
    body: dict[str, Any] = {
        "guardian_contact_id": contact,
        "relationship_label": "Guardian",
        "pickup_authorized": True,
        **changes,
    }
    response = await stack["client"].post(
        f"/v1/children/{kid}/guardians", json=body, headers=stack["admin-1"]
    )
    assert response.status_code == 422, response.text
    missing = await stack["client"].post(
        f"/v1/children/{kid}/guardians",
        json={"guardian_contact_id": contact, "relationship_label": "Guardian"},
        headers=stack["admin-1"],
    )
    assert missing.status_code == 422  # pickup_authorized is never defaulted


async def test_temporary_authorization_windows(stack: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Window")
    soon, past, today = [await guardian(stack, f"Adult {n}") for n in ("Soon", "Past", "Today")]
    now = datetime.now(UTC)
    await link(
        stack,
        kid,
        soon,
        "Authorized pickup",
        effective_from=(now + timedelta(hours=2)).isoformat(),
        effective_until=(now + timedelta(hours=10)).isoformat(),
    )
    await link(
        stack,
        kid,
        past,
        "Grandparent",
        effective_from=(now - timedelta(days=3)).isoformat(),
        effective_until=(now - timedelta(days=1)).isoformat(),
    )
    await link(
        stack, kid, today, "Babysitter", effective_until=(now + timedelta(hours=8)).isoformat()
    )
    status_of = {
        item["guardian_contact_id"]: item["pickup_status"]
        for item in (await child_links(stack, kid))["links"]
    }
    assert status_of == {
        soon: "AUTHORIZATION_NOT_STARTED",
        past: "AUTHORIZATION_EXPIRED",
        today: "AUTHORIZED",
    }


async def test_link_conflicts_facility_mismatch_and_isolation(stack: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Conflict")
    contact = await guardian(stack, "Adult Conflict")
    await link(stack, kid, contact)
    duplicate = await add_link(stack, kid, contact, "Guardian")
    assert duplicate.status_code == 409 and category(duplicate) == "association_exists"
    far = await guardian(stack, "Adult Far", facility="f2", as_="owner-a")
    mismatch = await add_link(stack, kid, far, as_="owner-a")
    assert mismatch.status_code == 409 and category(mismatch) == "facility_mismatch"
    # A facility-one admin cannot even see the facility-two contact.
    assert (await add_link(stack, kid, far)).status_code == 404
    other_tenant = await guardian(stack, "Adult B", facility="fb", as_="owner-b")
    assert (await add_link(stack, kid, other_tenant)).status_code == 404
    assert (await add_link(stack, kid, contact, as_="owner-b")).status_code == 404
    assert (await add_link(stack, kid, str(uuid4()))).status_code == 404
    assert (await add_link(stack, kid, contact, as_="viewer-1")).status_code == 403
    for who in ("owner-b", "viewer-2"):
        response = await stack["client"].get(f"/v1/children/{kid}/guardians", headers=stack[who])
        assert response.status_code == 404 and response.json() == {"detail": "not found"}


async def test_edit_toggle_and_deactivate_keep_history(stack: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Edit")
    contact = await guardian(stack, "Adult Edit")
    link_id = await link(stack, kid, contact, "Aunt")
    off = await patch_link(stack, kid, link_id, pickup_authorized=False)
    assert off.status_code == 200
    item = link_of(off.json(), contact)
    assert (item["pickup_authorized"], item["pickup_status"], item["revision"]) == (
        False,
        "PICKUP_NOT_AUTHORIZED",
        2,
    )
    until = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    on = await patch_link(
        stack,
        kid,
        link_id,
        pickup_authorized=True,
        effective_until=until,
        relationship_label="Aunt (maternal)",
    )
    item = link_of(on.json(), contact)
    assert (item["pickup_status"], item["relationship_label"], item["revision"]) == (
        "AUTHORIZED",
        "Aunt (maternal)",
        3,
    )
    unchanged = await patch_link(stack, kid, link_id, pickup_authorized=True)
    assert link_of(unchanged.json(), contact)["revision"] == 3
    cleared = await patch_link(stack, kid, link_id, effective_until=None)
    assert link_of(cleared.json(), contact)["effective_until"] is None
    inverted = await patch_link(stack, kid, link_id, effective_until="2020-01-01T00:00:00Z")
    assert inverted.status_code == 422 and category(inverted) == "effective_period_inverted"
    assert (
        await patch_link(stack, kid, link_id, as_="viewer-1", pickup_authorized=False)
    ).status_code == 403
    url = f"/v1/children/{kid}/guardians/{link_id}/deactivate"
    first = await stack["client"].post(url, headers=stack["admin-1"])
    second = await stack["client"].post(url, headers=stack["admin-1"])
    assert first.status_code == second.status_code == 200
    old = link_of(second.json(), contact, "INACTIVE")
    assert (old["pickup_status"], old["revision"]) == ("ASSOCIATION_INACTIVE", 5)
    assert old["deactivated_at"] is not None
    refused = await patch_link(stack, kid, link_id, pickup_authorized=True)
    assert refused.status_code == 409 and category(refused) == "association_inactive"
    # A new association is a new row; the old one stays in history, inactive.
    await link(stack, kid, contact, "Aunt", False)
    body = await child_links(stack, kid)
    assert [item["status"] for item in body["links"] if item["guardian_contact_id"] == contact] == [
        "ACTIVE",
        "INACTIVE",
    ]
    # Another child's link id is unknown under this child.
    other = await child(stack, "Child Else")
    assert (await patch_link(stack, other, link_id, pickup_authorized=False)).status_code == 404


async def test_inactive_parties_are_never_authorized(stack: dict[str, Any]) -> None:  # noqa: F811
    kid = await child(stack, "Child Parties")
    contact = await guardian(stack, "Adult Parties")
    await link(stack, kid, contact)
    await stack["client"].post(f"/v1/guardians/{contact}/deactivate", headers=stack["admin-1"])
    assert link_of(await child_links(stack, kid), contact)["pickup_status"] == (
        "AUTHORIZED_PERSON_INACTIVE"
    )
    await stack["client"].post(f"/v1/guardians/{contact}/activate", headers=stack["admin-1"])
    await lifecycle(stack, kid, "deactivate")
    assert link_of(await child_links(stack, kid), contact)["pickup_status"] == "CHILD_INACTIVE"
    await lifecycle(stack, kid, "archive")
    newcomer = await guardian(stack, "Adult Newcomer")
    refused = await add_link(stack, kid, newcomer)
    assert refused.status_code == 409 and category(refused) == "child_archived"


# ================================================================================== release
async def test_an_authorized_release_checks_out_and_records_the_exact_link(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult P1")
    link_id = await link(stack, kid, contact, "Mother")
    await patch_link(stack, kid, link_id, relationship_label="Mother (primary)")  # revision 2
    released = await release(stack, room, kid, contact, "PHOTO_ID_CHECKED")
    assert released.status_code == 201, released.text
    body = released.json()
    record = body["release"]
    assert (record["authorization_link_id"], record["authorization_link_revision"]) == (link_id, 2)
    assert record["verification_method"] == "PHOTO_ID_CHECKED"
    assert record["guardian_contact_id"] == contact and record["recorded_by_caller"] is True
    assert entry(body["attendance"], kid)["location"] == "NONE"
    assert body["attendance"]["summary"]["count"] == 0
    events = await events_of(stack, kid)
    assert [event.event_type for event in events] == ["CHECKED_IN", "CHECKED_OUT"]
    stored = await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid))
    assert len(stored) == 1
    assert stored[0].attendance_event_id == events[-1].id
    assert stored[0].released_at == events[-1].occurred_at
    assert (str(stored[0].area_id), str(stored[0].authorization_link_id)) == (room, link_id)
    assert record["attendance_event_id"] == str(events[-1].id)
    recent = body["attendance"]["recent_events"]
    assert [(item["event_type"], item["released"]) for item in recent] == [
        ("CHECKED_OUT", True),
        ("CHECKED_IN", False),
    ]
    history = await stack["client"].get(
        f"/v1/children/{kid}/release-history", headers=stack["viewer-1"]
    )
    assert history.status_code == 200
    assert [item["release_id"] for item in history.json()["releases"]] == [record["release_id"]]
    assert history.json()["releases"][0]["recorded_by_caller"] is False


@pytest.mark.parametrize(
    ("method", "extra"),
    [
        (None, {}),
        ("", {}),
        ("FACE_MATCH", {}),
        ("OTHER_MANUAL", {}),
        ("operator_confirmed", {}),
        ("OPERATOR_CONFIRMED", {"track_id": 3}),
        ("OPERATOR_CONFIRMED", {"camera_id": str(uuid4())}),
        ("OPERATOR_CONFIRMED", {"id_number": "D1234567"}),
        ("OPERATOR_CONFIRMED", {"note": "looked like the mother"}),
        ("OPERATOR_CONFIRMED", {"released_at": "2026-09-26T08:00:00Z"}),
        ("OPERATOR_CONFIRMED", {"recognition_result": "MATCH"}),
    ],
)
async def test_release_requires_a_bounded_verification_method_and_nothing_else(
    stack: dict[str, Any],  # noqa: F811
    method: str | None,
    extra: dict[str, Any],
) -> None:
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult Method")
    await link(stack, kid, contact)
    response = await release(stack, room, kid, contact, method, **extra)
    assert response.status_code == 422
    assert entry(await attendance_view(stack, room), kid)["location"] == "HERE"
    assert await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid)) == []


async def test_only_a_currently_authorized_person_can_collect(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack)
    now = datetime.now(UTC)
    stranger = await guardian(stack, "Adult Stranger")
    father = await guardian(stack, "Adult Father")
    grandparent = await guardian(stack, "Adult Grandparent")
    later = await guardian(stack, "Adult Later")
    former = await guardian(stack, "Adult Former")
    await link(stack, kid, father, "Father", False)
    await link(
        stack,
        kid,
        grandparent,
        "Grandparent",
        effective_from=(now - timedelta(days=2)).isoformat(),
        effective_until=(now - timedelta(minutes=5)).isoformat(),
    )
    await link(
        stack, kid, later, "Babysitter", effective_from=(now + timedelta(hours=1)).isoformat()
    )
    former_link = await link(stack, kid, former, "Guardian")
    await stack["client"].post(
        f"/v1/children/{kid}/guardians/{former_link}/deactivate", headers=stack["admin-1"]
    )
    for contact, expected in (
        (stranger, "no_association"),
        (father, "pickup_not_authorized"),
        (grandparent, "authorization_expired"),
        (later, "authorization_not_started"),
        (former, "association_inactive"),
    ):
        refused = await release(stack, room, kid, contact)
        assert refused.status_code == 409 and category(refused) == expected, expected
    assert entry(await attendance_view(stack, room), kid)["location"] == "HERE"
    assert len(await events_of(stack, kid)) == 1
    assert await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid)) == []


async def test_inactive_parties_cannot_release(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult Inactive")
    await link(stack, kid, contact)
    await stack["client"].post(f"/v1/guardians/{contact}/deactivate", headers=stack["admin-1"])
    refused = await release(stack, room, kid, contact)
    assert refused.status_code == 409 and category(refused) == "authorized_person_inactive"
    await stack["client"].post(f"/v1/guardians/{contact}/activate", headers=stack["admin-1"])
    await lifecycle(stack, kid, "deactivate")
    refused = await release(stack, room, kid, contact)
    assert refused.status_code == 409 and category(refused) == "child_inactive"


async def test_the_child_must_be_present_in_this_classroom(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    other_room = await attendance_room(stack, name="Room 2")
    kid = await child(stack, "Child Where")
    contact = await guardian(stack, "Adult Where")
    await link(stack, kid, contact)
    absent = await release(stack, room, kid, contact)
    assert absent.status_code == 409 and category(absent) == "child_not_checked_in"
    await attend(stack, other_room, "check-in", kid)
    wrong = await release(stack, room, kid, contact)
    assert wrong.status_code == 409 and category(wrong) == "child_in_another_classroom"
    first = await release(stack, other_room, kid, contact)
    assert first.status_code == 201
    again = await release(stack, other_room, kid, contact)
    assert again.status_code == 409 and category(again) == "child_not_checked_in"
    assert len(await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid))) == 1


async def test_a_lapsed_stay_is_not_released(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kid = await child(stack, "Child Lapsed")
    contact = await guardian(stack, "Adult Lapsed")
    await link(stack, kid, contact)
    long_ago = datetime.now(UTC) - timedelta(hours=13)
    await raw_event(
        stack,
        room,
        kid,
        occurred_at=long_ago,
        checked_in_at=long_ago,
        valid_until=long_ago + timedelta(hours=12),
    )
    lapsed = await release(stack, room, kid, contact)
    assert lapsed.status_code == 409 and category(lapsed) == "attendance_expired"
    # The administrative check-out closes it, and creates no release.
    assert (await attend(stack, room, "check-out", kid)).status_code == 200
    assert await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid)) == []


async def test_release_scope_and_uniform_404(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult Scope")
    await link(stack, kid, contact)
    # Holding no administer permission at all is refused before any lookup: nothing leaks.
    for who in ("viewer-1", "viewer-2"):
        assert (await release(stack, room, kid, contact, as_=who)).status_code == 403
    denied = await release(stack, room, kid, contact, as_="owner-b")
    assert denied.status_code == 404 and denied.json() == {"detail": "not found"}
    for who in ("owner-b", "viewer-2"):
        history = await stack["client"].get(
            f"/v1/children/{kid}/release-history", headers=stack[who]
        )
        assert history.status_code == 404
    other_tenant = await guardian(stack, "Adult B", facility="fb", as_="owner-b")
    assert (await release(stack, room, kid, other_tenant)).status_code == 404
    assert (await release(stack, room, str(uuid4()), contact)).status_code == 404
    assert (await release(stack, str(uuid4()), kid, contact)).status_code == 404
    # A contact at another facility the owner can read is refused, never matched by name.
    far = await guardian(stack, "Adult P1 Far", facility="f2", as_="owner-a")
    mismatch = await release(stack, room, kid, far, as_="owner-a")
    assert mismatch.status_code == 409 and category(mismatch) == "facility_mismatch"
    assert entry(await attendance_view(stack, room), kid)["location"] == "HERE"


async def test_release_options_offer_only_currently_authorized_people(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room, kid = await checked_in_child(stack)
    out = await child(stack, "Child Home")
    mother, friend, grandparent = [
        await guardian(stack, name) for name in ("Adult Mother", "Adult Friend", "Adult Grand")
    ]
    await link(stack, kid, mother, "Mother", True)
    await link(stack, kid, friend, "Family friend", False)
    now = datetime.now(UTC)
    await link(
        stack,
        kid,
        grandparent,
        "Grandparent",
        effective_from=(now - timedelta(days=2)).isoformat(),
        effective_until=(now - timedelta(days=1)).isoformat(),
    )
    await link(stack, out, mother, "Mother", True)
    response = await stack["client"].get(
        f"/v1/classrooms/{room}/attendance/release-options", headers=stack["viewer-1"]
    )
    assert response.status_code == 200
    body = response.json()
    assert body["can_release"] is False
    assert body["verification_methods"] == [
        "KNOWN_TO_STAFF",
        "OPERATOR_CONFIRMED",
        "PHOTO_ID_CHECKED",
    ]
    assert [item["child_profile_id"] for item in body["children"]] == [kid]  # only present children
    options = body["children"][0]
    assert [(c["guardian_contact_id"], c["relationship_label"]) for c in options["candidates"]] == [
        (mother, "Mother")
    ]
    assert {(u["guardian_contact_id"], u["reason"]) for u in options["unavailable"]} == {
        (friend, "PICKUP_NOT_AUTHORIZED"),
        (grandparent, "AUTHORIZATION_EXPIRED"),
    }
    for who in ("owner-b", "viewer-2"):
        hidden = await stack["client"].get(
            f"/v1/classrooms/{room}/attendance/release-options", headers=stack[who]
        )
        assert hidden.status_code == 404


async def test_administrative_check_out_is_distinguishable_from_release(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room = await attendance_room(stack)
    kid_a, kid_b = await child(stack, "Child Released"), await child(stack, "Child Corrected")
    contact = await guardian(stack, "Adult Admin")
    await link(stack, kid_a, contact)
    await link(stack, kid_b, contact)
    for kid in (kid_a, kid_b):
        await attend(stack, room, "check-in", kid)
    assert (await release(stack, room, kid_a, contact, "KNOWN_TO_STAFF")).status_code == 201
    assert (await attend(stack, room, "check-out", kid_b)).status_code == 200
    assert await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid_b)) == []
    view = await attendance_view(stack, room)
    outs = {
        item["child_profile_id"]: item["released"]
        for item in view["recent_events"]
        if item["event_type"] == "CHECKED_OUT"
    }
    assert outs == {kid_a: True, kid_b: False}
    events = await audits(stack, ["child_attendance_event", "child_release_event"])
    kinds = {
        (
            event.action,
            event.metadata_.get("child_profile_id"),
            event.metadata_.get("checkout_kind"),
        )
        for event in events
        if event.action in {"attendance.checked_out", "child.released"}
    }
    assert kinds == {
        ("child.released", kid_a, "AUTHORIZED_RELEASE"),
        ("attendance.checked_out", kid_b, "ADMINISTRATIVE_CHECKOUT"),
    }
    # The released child's check-out is audited once, as the release - not also as a check-out.
    assert not any(
        event.action == "attendance.checked_out"
        and event.metadata_.get("child_profile_id") == kid_a
        for event in events
    )


async def test_release_updates_the_attendance_count_and_ratio(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await attendance_room(stack)
    kids = [await child(stack, f"Child {letter}") for letter in "ABC"]
    staff = await teacher(stack, "Teacher A")
    for kid in kids:
        await attend(stack, room, "check-in", kid)
    await act(stack, room, "check-in", staff)
    before = (await status(stack, room))["evaluation"]
    contact = await guardian(stack, "Adult Ratio")
    await link(stack, kids[0], contact)
    assert (await release(stack, room, kids[0], contact)).status_code == 201
    after = await status(stack, room)
    assert (before["child_count"], after["evaluation"]["child_count"]) == (3, 2)
    assert after["presence_source_mode"] == ATTENDANCE_MODE
    assert after["sources"]["children"]["source"] == "ATTENDANCE"


# ============================================================================== atomicity
async def _forced_failure(stack: dict[str, Any], monkeypatch: Any, target: str) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult Atomic")
    await link(stack, kid, contact)
    service = service_of(stack)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"forced {target} failure")

    monkeypatch.setattr(service, target, boom)
    with pytest.raises(RuntimeError, match="forced"):
        await service.release_child(
            await admin_principal(stack),
            UUID(room),
            UUID(kid),
            UUID(contact),
            "OPERATOR_CONFIRMED",
            "req-atomic",
        )
    monkeypatch.undo()
    # Nothing survived: the child is still present, no check-out, no release, no audit.
    assert [event.event_type for event in await events_of(stack, kid)] == ["CHECKED_IN"]
    assert await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid)) == []
    assert not [
        event
        for event in await audits(stack, ["child_release_event", "child_attendance_event"])
        if event.request_id == "req-atomic"
    ]
    assert entry(await attendance_view(stack, room), kid)["location"] == "HERE"
    # And the ordinary path still works afterwards.
    assert (await release(stack, room, kid, contact)).status_code == 201


async def test_a_failed_release_record_rolls_back_the_check_out(
    stack: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _forced_failure(stack, monkeypatch, "_release_row")


async def test_a_failed_check_out_creates_no_release(
    stack: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _forced_failure(stack, monkeypatch, "_append")


async def test_a_failed_audit_rolls_back_the_release_and_the_check_out(
    stack: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _forced_failure(stack, monkeypatch, "_audit_release")


async def test_a_database_refusal_of_the_release_row_rolls_back_the_check_out(
    stack: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room, kid = await checked_in_child(stack)
    contact = await guardian(stack, "Adult Refused")
    await link(stack, kid, contact)
    service = service_of(stack)
    original = service._release_row

    def corrupt(*args: Any, **kwargs: Any) -> ChildReleaseEvent:
        row = original(*args, **kwargs)
        row.verification_method = "FACE_MATCH"  # the CHECK refuses it at flush
        return row

    monkeypatch.setattr(service, "_release_row", corrupt)
    with pytest.raises(ClassroomError, match="release_state_changed"):
        await service.release_child(
            await admin_principal(stack),
            UUID(room),
            UUID(kid),
            UUID(contact),
            "OPERATOR_CONFIRMED",
            "r",
        )
    assert [event.event_type for event in await events_of(stack, kid)] == ["CHECKED_IN"]
    assert await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid)) == []


# ============================================================================== concurrency
async def test_simultaneous_releases_release_the_child_exactly_once(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room, kid = await checked_in_child(stack, "Child Race")
    first, second = await guardian(stack, "Adult One"), await guardian(stack, "Adult Two")
    await link(stack, kid, first, "Mother")
    await link(stack, kid, second, "Father")
    responses = await asyncio.gather(
        *(release(stack, room, kid, (first, second)[index % 2]) for index in range(10))
    )
    codes = sorted(response.status_code for response in responses)
    assert codes == [201] + [409] * 9
    assert {category(r) for r in responses if r.status_code == 409} == {"child_not_checked_in"}
    events = await events_of(stack, kid)
    assert [event.event_type for event in events] == ["CHECKED_IN", "CHECKED_OUT"]
    stored = await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid))
    assert len(stored) == 1 and stored[0].attendance_event_id == events[-1].id


async def test_a_release_racing_an_administrative_check_out_writes_one_check_out(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    room, kid = await checked_in_child(stack, "Child Race Two")
    contact = await guardian(stack, "Adult Race")
    await link(stack, kid, contact)
    released, checked_out = await asyncio.gather(
        release(stack, room, kid, contact), attend(stack, room, "check-out", kid)
    )
    assert checked_out.status_code == 200
    events = await events_of(stack, kid)
    assert [event.event_type for event in events] == ["CHECKED_IN", "CHECKED_OUT"]
    stored = await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid))
    if released.status_code == 201:
        assert len(stored) == 1 and stored[0].attendance_event_id == events[-1].id
    else:
        assert released.status_code == 409 and category(released) == "child_not_checked_in"
        assert stored == []


async def test_a_pickup_disabled_while_releasing_is_serialised(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack, "Child Serial")
    contact = await guardian(stack, "Adult Serial")
    link_id = await link(stack, kid, contact)
    released, disabled = await asyncio.gather(
        release(stack, room, kid, contact), patch_link(stack, kid, link_id, pickup_authorized=False)
    )
    assert disabled.status_code == 200
    stored = await rows(stack, ChildReleaseEvent, child_profile_id=UUID(kid))
    if released.status_code == 201:
        # The release committed first, on revision 1 - which authorized it.
        assert [row.authorization_link_revision for row in stored] == [1]
    else:
        assert category(released) == "pickup_not_authorized" and stored == []


# ==================================================================================== audit
async def test_guardian_link_and_release_audits_carry_no_pii(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack, "Child Secret Name")
    contact = await guardian(stack, "Adult Private Person", external_reference="FAM-PRIVATE-9")
    await stack["client"].patch(
        f"/v1/guardians/{contact}",
        json={"display_name": "Adult Renamed Secret"},
        headers=stack["admin-1"],
    )
    await stack["client"].post(f"/v1/guardians/{contact}/deactivate", headers=stack["admin-1"])
    await stack["client"].post(f"/v1/guardians/{contact}/activate", headers=stack["admin-1"])
    link_id = await link(stack, kid, contact, "Secret Relationship", note="Secret Note Text")
    await patch_link(stack, kid, link_id, pickup_authorized=False)
    await patch_link(stack, kid, link_id, pickup_authorized=True)
    await patch_link(stack, kid, link_id, relationship_label="Other Secret Label")
    assert (await release(stack, room, kid, contact, "KNOWN_TO_STAFF")).status_code == 201
    await stack["client"].post(
        f"/v1/children/{kid}/guardians/{link_id}/deactivate", headers=stack["admin-1"]
    )
    await stack["client"].post(f"/v1/guardians/{contact}/archive", headers=stack["admin-1"])
    events = await audits(stack, ["guardian_contact", "child_guardian_link", "child_release_event"])
    assert [event.action for event in events] == [
        "guardian.created",
        "guardian.updated",
        "guardian.deactivated",
        "guardian.activated",
        "child_guardian_link.created",
        "child_guardian_link.pickup_disabled",
        "child_guardian_link.pickup_enabled",
        "child_guardian_link.updated",
        "child.released",
        "child_guardian_link.deactivated",
        "guardian.archived",
    ]
    created_link = events[4].metadata_
    assert created_link["pickup_authorized"] is True and created_link["note_present"] is True
    disabled = events[5].metadata_
    assert (disabled["before"]["pickup_authorized"], disabled["after"]["pickup_authorized"]) == (
        True,
        False,
    )
    assert events[7].metadata_["changed_fields"] == ["relationship_label"]
    released = events[8].metadata_
    assert released["guardian_contact_id"] == contact and released["child_profile_id"] == kid
    assert released["authorization_link_id"] == link_id
    assert released["authorization_link_revision"] == 4
    assert released["verification_method"] == "KNOWN_TO_STAFF"
    assert released["checkout_kind"] == "AUTHORIZED_RELEASE"
    rendered = str([event.metadata_ for event in events]).lower()
    for forbidden in (
        "secret",
        "private person",
        "fam-private",
        "display_name':",
        "relationship_label':",
        "note':",
        "phone",
        "email",
        "photo",
        "face",
        "embedding",
        "track",
        "image",
    ):
        assert forbidden not in rendered, forbidden


async def test_no_release_row_exists_without_its_check_out(stack: dict[str, Any]) -> None:  # noqa: F811
    room, kid = await checked_in_child(stack, "Child Pairing")
    contact = await guardian(stack, "Adult Pairing")
    await link(stack, kid, contact)
    await release(stack, room, kid, contact)
    async with stack["admin"]() as session, session.begin():
        orphans = await session.scalar(
            select(func.count())
            .select_from(ChildReleaseEvent)
            .outerjoin(
                ChildAttendanceEvent,
                ChildAttendanceEvent.id == ChildReleaseEvent.attendance_event_id,
            )
            .where(
                (ChildAttendanceEvent.id.is_(None))
                | (ChildAttendanceEvent.event_type != "CHECKED_OUT")
            )
        )
    assert orphans == 0
