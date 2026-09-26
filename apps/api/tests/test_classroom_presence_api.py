"""Manual classroom presence over HTTP and in PostgreSQL (V1-04B).

Reuses the V1-04A two-tenant stack (a tenant owner, a facility admin and a viewer on facility 1,
a viewer on facility 2, another tenant). Proves the append-only table's own invariants (CHECKs,
the composite facility/classroom key, the revocation-only trigger, RLS, PUBLIC and DELETE
denial), the submit / read / revoke API, ratio-status integration, audit contents and
isolation. Every count is a synthetic aggregate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_classroom_api import create_classroom, policy_body, stack  # noqa: F401 - fixture

from veotrex_api.models import AuditEvent, ClassroomPresenceSnapshot

TABLE = "classroom_presence_snapshots"


async def classroom_with_policy(stack: dict[str, Any], ratio: int = 5) -> str:  # noqa: F811
    room = await create_classroom(stack)
    response = await stack["client"].post(
        f"/v1/classrooms/{room['classroom_id']}/ratio-policies",
        json=policy_body(max_children_per_staff=ratio, minimum_staff=0, maximum_group_size=None),
        headers=stack["admin-1"],
    )
    assert response.status_code == 201, response.text
    return str(room["classroom_id"])


async def report(
    stack: dict[str, Any],  # noqa: F811
    room: str,
    children: int,
    staff: int,
    visitors: int = 0,
    *,
    as_: str = "admin-1",
    **extra: Any,
) -> Any:
    return await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual",
        json={
            "child_count": children,
            "qualified_staff_count": staff,
            "visitor_count": visitors,
            **extra,
        },
        headers=stack[as_],
    )


async def status(stack: dict[str, Any], room: str, as_: str = "viewer-1") -> dict[str, Any]:  # noqa: F811
    response = await stack["client"].get(f"/v1/classrooms/{room}/ratio-status", headers=stack[as_])
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


async def raw_insert(stack: dict[str, Any], room: str, **values: Any) -> None:  # noqa: F811
    """Insert directly as the admin identity, bypassing the API, to prove the database itself
    enforces the invariants."""
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
            "child_count": 6,
            "qualified_staff_count": 1,
            "visitor_count": 0,
            "source": "MANUAL",
            "observed_at": now,
            "valid_until": now + timedelta(seconds=120),
            "submitted_by_actor_id": actor,
        }
        row.update(values)
        session.add(ClassroomPresenceSnapshot(**row))
        await session.flush()


# ================================================================ database invariants
async def test_a_valid_snapshot_is_stored(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    await raw_insert(stack, room)


@pytest.mark.parametrize(
    "values",
    [
        {"child_count": -1},
        {"qualified_staff_count": -1},
        {"visitor_count": -1},
        {"child_count": 151},
        {"qualified_staff_count": 51},
        {"visitor_count": 51},
        {"source": "VISION"},
        {"valid_until": "observed"},  # valid_until == observed_at
        {"valid_until": "too_long"},
        {"observed_at": "future"},
    ],
)
async def test_the_database_refuses_invalid_rows(
    stack: dict[str, Any],  # noqa: F811
    values: dict[str, Any],
) -> None:
    room = await classroom_with_policy(stack)
    now = datetime.now(UTC)
    resolved = dict(values)
    if resolved.get("valid_until") == "observed":
        resolved.update(observed_at=now, valid_until=now)
    elif resolved.get("valid_until") == "too_long":
        resolved.update(observed_at=now, valid_until=now + timedelta(minutes=16))
    elif resolved.get("observed_at") == "future":
        resolved.update(
            observed_at=now + timedelta(minutes=10),
            valid_until=now + timedelta(minutes=12),
        )
    with pytest.raises(DBAPIError, match="check constraint|violates"):
        await raw_insert(stack, room, **resolved)


async def test_a_facility_classroom_mismatch_cannot_be_stored(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    with pytest.raises(DBAPIError, match="foreign key"):
        await raw_insert(stack, room, facility_id=stack["f2"])


async def test_another_tenants_classroom_cannot_be_referenced(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    with pytest.raises(DBAPIError, match="foreign key|row-level security"):
        await raw_insert(stack, room, tenant_id=stack["tenant_b"], facility_id=stack["fb"])


async def test_rows_are_append_only_except_one_revocation(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    created = (await report(stack, room, 6, 1)).json()
    snapshot_id = created["current"]["snapshot_id"]
    async with stack["runtime"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        with pytest.raises(DBAPIError, match="append-only|revocation"):
            async with session.begin_nested():
                await session.execute(
                    text("UPDATE classroom_presence_snapshots SET child_count = 3 WHERE id = :id"),
                    {"id": snapshot_id},
                )
    revoked = await stack["client"].post(
        f"/v1/classrooms/{room}/presence/{snapshot_id}/revoke", headers=stack["admin-1"]
    )
    assert revoked.status_code == 200
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        with pytest.raises(DBAPIError, match="already revoked"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE classroom_presence_snapshots SET revoked_at = now() WHERE id = :id"
                    ),
                    {"id": snapshot_id},
                )


async def test_rls_isolates_tenants_and_fails_closed(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    await report(stack, room, 6, 1)
    async with stack["runtime"]() as session, session.begin():
        assert await session.scalar(text("SELECT count(*) FROM classroom_presence_snapshots")) == 0
    async with stack["runtime"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_b"])}
        )
        assert await session.scalar(text("SELECT count(*) FROM classroom_presence_snapshots")) == 0


async def test_public_and_delete_are_denied(
    stack: dict[str, Any],  # noqa: F811
    runtime_role_name: str,
) -> None:
    async with stack["admin"]() as session, session.begin():
        privileges = {
            privilege: await session.scalar(
                text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": runtime_role_name, "table": f"public.{TABLE}", "privilege": privilege},
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
        public_grants = await session.scalar(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE table_name = :table AND grantee = 'PUBLIC'"
            ),
            {"table": TABLE},
        )
    assert privileges == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": True,
        "DELETE": False,
        "TRUNCATE": False,
    }
    assert public_grants == 0


# ============================================================================== API: submit
async def test_an_admin_reports_and_the_ratio_is_evaluated(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    response = await report(stack, room, 6, 1)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["availability"] == "PRESENCE_FRESH"
    current = body["current"]
    assert (current["child_count"], current["qualified_staff_count"], current["visitor_count"]) == (
        6,
        1,
        0,
    )
    assert current["source"] == "MANUAL" and current["authoritative"] is True
    lifetime = datetime.fromisoformat(current["valid_until"]) - datetime.fromisoformat(
        current["observed_at"]
    )
    assert lifetime == timedelta(seconds=120), "default validity is two minutes"
    result = await status(stack, room)
    evaluation = result["evaluation"]
    assert evaluation["ratio_state"] == "OVER_CONFIGURED_RATIO"
    assert (evaluation["required_staff"], evaluation["staff_deficit"]) == (2, 1)
    presence = result["presence"]
    assert presence["availability"] == "PRESENCE_FRESH" and presence["source"] == "MANUAL"
    assert (presence["child_count"], presence["qualified_staff_count"]) == (6, 1)
    assert result["presence_connected"] is True
    assert result["reconciliation"]["state"] == "NOT_AVAILABLE", "vision is not connected"


@pytest.mark.parametrize(
    ("children", "staff", "state"),
    [
        (5, 1, "WITHIN_CONFIGURED_POLICY"),
        (6, 2, "WITHIN_CONFIGURED_POLICY"),
        (1, 0, "OVER_CONFIGURED_RATIO"),
        (0, 0, "NO_CHILDREN_PRESENT"),
    ],
)
async def test_ratio_states_from_manual_counts(
    stack: dict[str, Any],  # noqa: F811
    children: int,
    staff: int,
    state: str,
) -> None:
    room = await classroom_with_policy(stack)
    await report(stack, room, children, staff)
    assert (await status(stack, room))["evaluation"]["ratio_state"] == state


@pytest.mark.parametrize(
    ("body", "category"),
    [
        ({"child_count": -1, "qualified_staff_count": 1}, "invalid_child_count"),
        ({"child_count": 6, "qualified_staff_count": -1}, "invalid_qualified_staff_count"),
        (
            {"child_count": 6, "qualified_staff_count": 1, "visitor_count": -1},
            "invalid_visitor_count",
        ),
        ({"child_count": 151, "qualified_staff_count": 1}, "invalid_child_count"),
        (
            {"child_count": 6, "qualified_staff_count": 1, "valid_for_seconds": 10},
            "invalid_validity_seconds",
        ),
        (
            {"child_count": 6, "qualified_staff_count": 1, "valid_for_seconds": 86400},
            "invalid_validity_seconds",
        ),
    ],
)
async def test_invalid_reports_are_422_with_the_rule(
    stack: dict[str, Any],  # noqa: F811
    body: dict[str, Any],
    category: str,
) -> None:
    room = await classroom_with_policy(stack)
    response = await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual", json=body, headers=stack["admin-1"]
    )
    assert response.status_code == 422
    assert response.json()["detail"]["category"] == category


@pytest.mark.parametrize(
    "body",
    [
        {"child_count": "6", "qualified_staff_count": 1},
        {"child_count": 6.0, "qualified_staff_count": 1},
        {"child_count": True, "qualified_staff_count": 1},
        {"child_count": 6},
        {"child_count": 6, "qualified_staff_count": 1, "child_names": ["x"]},
        {"child_count": 6, "qualified_staff_count": 1, "observed_at": "2030-01-01T00:00:00Z"},
        {"child_count": 6, "qualified_staff_count": 1, "unknown_count": 2},
    ],
)
async def test_malformed_or_extra_fields_are_refused(
    stack: dict[str, Any],  # noqa: F811
    body: dict[str, Any],
) -> None:
    room = await classroom_with_policy(stack)
    response = await stack["client"].post(
        f"/v1/classrooms/{room}/presence/manual", json=body, headers=stack["admin-1"]
    )
    assert response.status_code == 422


async def test_viewers_cannot_report_or_revoke(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    assert (await report(stack, room, 3, 1, as_="viewer-1")).status_code == 403
    snapshot = (await report(stack, room, 3, 1)).json()["current"]["snapshot_id"]
    revoke = await stack["client"].post(
        f"/v1/classrooms/{room}/presence/{snapshot}/revoke", headers=stack["viewer-1"]
    )
    assert revoke.status_code == 403
    readable = await stack["client"].get(
        f"/v1/classrooms/{room}/presence", headers=stack["viewer-1"]
    )
    assert readable.status_code == 200


async def test_other_tenants_and_other_facilities_get_404(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    snapshot = (await report(stack, room, 3, 1)).json()["current"]["snapshot_id"]
    client = stack["client"]
    for who in ("owner-b", "viewer-2"):
        for method, url in (
            ("GET", f"/v1/classrooms/{room}/presence"),
            ("POST", f"/v1/classrooms/{room}/presence/manual"),
            ("POST", f"/v1/classrooms/{room}/presence/{snapshot}/revoke"),
        ):
            body = {"child_count": 1, "qualified_staff_count": 1} if method == "POST" else None
            response = await client.request(method, url, json=body, headers=stack[who])
            assert response.status_code in (403, 404), (who, url)
            if who == "owner-b" or method == "GET":
                assert response.status_code == 404, (who, url)


async def test_a_facility_admin_cannot_report_for_another_facility(stack: dict[str, Any]) -> None:  # noqa: F811
    elsewhere = (await create_classroom(stack, "f2", as_="owner-a", name="Room F2"))["classroom_id"]
    response = await report(stack, elsewhere, 3, 1, as_="admin-1")
    assert response.status_code == 404


async def test_an_inactive_classroom_takes_no_report(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    await stack["client"].post(f"/v1/classrooms/{room}/deactivate", headers=stack["admin-1"])
    response = await report(stack, room, 3, 1)
    assert response.status_code == 409
    assert response.json()["detail"]["category"] == "classroom_inactive"


# ================================================================ API: supersede / revoke
async def test_a_new_report_supersedes_and_history_is_kept(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    await report(stack, room, 6, 1)
    body = (await report(stack, room, 6, 2)).json()
    assert body["current"]["qualified_staff_count"] == 2
    assert [item["qualified_staff_count"] for item in body["history"]] == [2, 1]
    assert [item["authoritative"] for item in body["history"]] == [True, False]
    assert [item["freshness"] for item in body["history"]] == ["FRESH", "SUPERSEDED"]
    assert (await status(stack, room))["evaluation"]["ratio_state"] == "WITHIN_CONFIGURED_POLICY"


async def test_revoke_is_idempotent_and_never_resurrects_older(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    await report(stack, room, 5, 1)
    latest = (await report(stack, room, 6, 1)).json()["current"]
    url = f"/v1/classrooms/{room}/presence/{latest['snapshot_id']}/revoke"
    first = await stack["client"].post(url, headers=stack["admin-1"])
    assert first.status_code == 200
    assert first.json()["availability"] == "PRESENCE_REVOKED"
    revoked_at = first.json()["current"]["revoked_at"]
    second = await stack["client"].post(url, headers=stack["owner-a"])
    assert second.status_code == 200
    assert second.json()["current"]["revoked_at"] == revoked_at, "first revocation is kept"
    result = await status(stack, room)
    assert result["evaluation"]["ratio_state"] == "INSUFFICIENT_DATA"
    assert result["presence"]["availability"] == "PRESENCE_REVOKED"
    assert result["presence"]["child_count"] is None, "the earlier 5/1 report does not return"


async def test_an_unknown_snapshot_is_a_uniform_404(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    other_room = (await create_classroom(stack, name="Room 2"))["classroom_id"]
    foreign = (await report(stack, other_room, 2, 1)).json()["current"]["snapshot_id"]
    for snapshot in (str(uuid4()), foreign):
        response = await stack["client"].post(
            f"/v1/classrooms/{room}/presence/{snapshot}/revoke", headers=stack["admin-1"]
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "classroom not found"}


async def test_status_is_insufficient_once_the_report_expires(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    await report(stack, room, 6, 1)
    later = datetime.now(UTC) + timedelta(minutes=3)
    service = stack["client"]._transport.app.state.classroom_service
    principal_body = await stack["client"].get("/v1/me", headers=stack["viewer-1"])
    assert principal_body.status_code == 200
    # Evaluate the stored report three minutes from now through the service itself.
    from veotrex_api.access import AuthenticatedPrincipal
    from veotrex_api.authorization import Permission, Role, RoleGrant

    me = principal_body.json()
    principal = AuthenticatedPrincipal(
        issuer="",
        subject="",
        external_organization_id="",
        actor_id=UUID(me["actor_id"]),
        tenant_id=UUID(me["tenant_id"]),
        display_name=None,
        grants=(RoleGrant(Role.VIEWER, stack["f1"]),),
        permissions=frozenset({Permission.READ_OPERATIONAL}),
    )
    stale = await service.ratio_status(principal, UUID(room), now=later)
    body = stale.as_dict()
    assert body["evaluation"]["ratio_state"] == "INSUFFICIENT_DATA"
    assert "CHILD_COUNT_STALE" in body["evaluation"]["explanations"]
    assert body["presence"]["availability"] == "PRESENCE_STALE"
    assert body["presence"]["child_count"] is None, "stale counts are not shown as current"
    assert body["evaluation"]["required_staff"] is None


# ==================================================================================== audit
async def test_submit_and_revoke_are_audited_without_secrets(stack: dict[str, Any]) -> None:  # noqa: F811
    room = await classroom_with_policy(stack)
    snapshot = (await report(stack, room, 6, 1, visitors=1)).json()["current"]["snapshot_id"]
    await stack["client"].post(
        f"/v1/classrooms/{room}/presence/{snapshot}/revoke", headers=stack["admin-1"]
    )
    await stack["client"].post(
        f"/v1/classrooms/{room}/presence/{snapshot}/revoke", headers=stack["admin-1"]
    )
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        events = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.target_id == UUID(snapshot))
                .order_by(AuditEvent.occurred_at)
            )
        ).all()
    assert [event.action for event in events] == [
        "presence.manual_submitted",
        "presence.manual_revoked",
    ], "a repeated revoke is not audited twice"
    submitted = events[0].metadata_
    assert submitted == {
        "classroom_id": room,
        "source": "MANUAL",
        "child_count": 6,
        "qualified_staff_count": 1,
        "visitor_count": 1,
        "valid_for_seconds": 120,
    }
    rendered = str([event.metadata_ for event in events]).lower()
    for forbidden in ("token", "bearer", "auth0", "secret", "image", "frame", "track", "name"):
        assert forbidden not in rendered, forbidden
