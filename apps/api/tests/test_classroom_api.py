"""Classrooms and configured ratio policies over HTTP, the real runtime role and RLS (V1-04A).

Two tenants. In tenant A: two facilities, a tenant owner, a facility admin and a viewer scoped
to facility 1, and a viewer scoped to facility 2. Proves the classroom and policy lifecycle,
validation, tenant and facility isolation (uniform 404), role enforcement, camera association
through the existing zone relation, ratio status without any presence source, audit, RLS
fail-closed behaviour and the runtime grants. All data is synthetic; no child is modelled.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.face_backend import FakeFaceBackend
from veotrex_api.identity import ExternalIdentity, IdentityVerificationError
from veotrex_api.main import create_app
from veotrex_api.models import (
    AuditEvent,
    Camera,
    CameraProviderConnection,
    ClassroomRatioPolicy,
    Zone,
)
from veotrex_api.staff_media import StaffMediaStore

ISSUER = "https://tenant.auth0.example/"
TODAY = datetime.now(UTC).date()


class StubIdentityVerifier:
    def __init__(self, identities: dict[str, ExternalIdentity]) -> None:
        self.identities = identities

    async def verify(self, token: str) -> ExternalIdentity:
        try:
            return self.identities[token]
        except KeyError as exc:
            raise IdentityVerificationError("rejected") from exc


class Secrets:
    def resolve(self, secret_ref: str) -> SecretStr:
        return SecretStr("classroom-test-secret")


class NoRing:
    async def aclose(self) -> None:
        return None


async def seed_tenant(  # type: ignore[no-untyped-def]
    admin_factory,
    label: str,
    organization: str,
    facilities: dict[str, str],
    subjects: dict[str, tuple[str, str | None]],
) -> tuple[UUID, dict[str, UUID]]:
    tenant_id = uuid4()
    facility_ids = {key: uuid4() for key in facilities}
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
            {"id": tenant_id, "name": f"Classroom tenant {label}"},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_identity_bindings "
                "(id, tenant_id, provider, issuer, external_organization_id) "
                "VALUES (:id, :tenant_id, 'auth0', :issuer, :organization)"
            ),
            {"id": uuid4(), "tenant_id": tenant_id, "issuer": ISSUER, "organization": organization},
        )
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
        )
        for key, timezone in facilities.items():
            await session.execute(
                text(
                    "INSERT INTO facilities (id, tenant_id, name, jurisdiction, timezone, status) "
                    "VALUES (:id, :tenant, :name, 'US-XX', :tz, 'ACTIVE')"
                ),
                {
                    "id": facility_ids[key],
                    "tenant": tenant_id,
                    "name": f"Site {key}",
                    "tz": timezone,
                },
            )
        for subject, (role, facility_key) in subjects.items():
            actor_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO actors (id, tenant_id, display_name, status) "
                    "VALUES (:actor, :tenant, :name, 'ACTIVE')"
                ),
                {"actor": actor_id, "tenant": tenant_id, "name": role.title()},
            )
            await session.execute(
                text(
                    "INSERT INTO actor_identities "
                    "(id, tenant_id, actor_id, provider, issuer, subject) "
                    "VALUES (:id, :tenant, :actor, 'auth0', :issuer, :subject)"
                ),
                {
                    "id": uuid4(),
                    "tenant": tenant_id,
                    "actor": actor_id,
                    "issuer": ISSUER,
                    "subject": subject,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO role_assignments (id, tenant_id, actor_id, role, facility_id) "
                    "VALUES (:id, :tenant, :actor, :role, :facility)"
                ),
                {
                    "id": uuid4(),
                    "tenant": tenant_id,
                    "actor": actor_id,
                    "role": role,
                    "facility": None if facility_key is None else facility_ids[facility_key],
                },
            )
    return tenant_id, facility_ids


@pytest.fixture
async def stack(settings: Settings, admin_settings: Settings, tmp_path):  # type: ignore[no-untyped-def]
    admin_engine = make_engine(admin_settings)
    admin_factory = make_session_factory(admin_engine)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    org_a, org_b = f"org_{uuid4().hex}", f"org_{uuid4().hex}"
    owner_a, admin_1, viewer_1, viewer_2, owner_b = (f"auth0|{uuid4().hex}" for _ in range(5))
    tenant_a, facilities_a = await seed_tenant(
        admin_factory,
        "A",
        org_a,
        {"one": "America/Phoenix", "two": "America/New_York"},
        {
            owner_a: ("TENANT_OWNER", None),
            admin_1: ("FACILITY_ADMIN", "one"),
            viewer_1: ("VIEWER", "one"),
            viewer_2: ("VIEWER", "two"),
        },
    )
    tenant_b, facilities_b = await seed_tenant(
        admin_factory, "B", org_b, {"b": "UTC"}, {owner_b: ("TENANT_OWNER", None)}
    )
    verifier = StubIdentityVerifier(
        {
            "owner-a": ExternalIdentity("auth0", ISSUER, owner_a, org_a),
            "admin-1": ExternalIdentity("auth0", ISSUER, admin_1, org_a),
            "viewer-1": ExternalIdentity("auth0", ISSUER, viewer_1, org_a),
            "viewer-2": ExternalIdentity("auth0", ISSUER, viewer_2, org_a),
            "owner-b": ExternalIdentity("auth0", ISSUER, owner_b, org_b),
        }
    )
    app = create_app(
        settings,
        engine,
        verifier,
        factory,
        credential_vault=InMemoryCredentialVault(),
        ring_client=NoRing(),  # type: ignore[arg-type]
        secret_resolver=Secrets(),
        face_backend=FakeFaceBackend(),
        staff_media=StaffMediaStore(tmp_path / "media"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {
            "client": client,
            "admin": admin_factory,
            "runtime": factory,
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
            "f1": facilities_a["one"],
            "f2": facilities_a["two"],
            "fb": facilities_b["b"],
            **{
                name: {"Authorization": f"Bearer {name}"}
                for name in ("owner-a", "admin-1", "viewer-1", "viewer-2", "owner-b")
            },
        }
    await engine.dispose()
    await admin_engine.dispose()


def policy_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "label": "Configured toddler policy",
        "age_band_label": "Toddler",
        "max_children_per_staff": 5,
        "minimum_staff": 1,
        "maximum_group_size": 10,
        "effective_from_date": (TODAY - timedelta(days=1)).isoformat(),
        "effective_through_date": None,
        "source_reference": "Owner-provided classroom policy, revision 1",
    }
    body.update(changes)
    return body


async def create_classroom(
    stack: dict[str, Any],
    facility: str = "f1",
    *,
    as_: str = "admin-1",
    name: str = "Room 1",
    age_band: str | None = "Toddler",
) -> dict[str, Any]:
    response = await stack["client"].post(
        "/v1/classrooms",
        json={"facility_id": str(stack[facility]), "name": name, "age_band_label": age_band},
        headers=stack[as_],
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# ================================================================== classroom lifecycle
async def test_a_facility_admin_creates_and_lists_a_classroom(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    assert room["facility_id"] == str(stack["f1"])
    assert room["facility_timezone"] == "America/Phoenix"
    assert room["age_band_label"] == "Toddler"
    assert room["status"] == "ACTIVE"
    assert room["policies"] == [] and room["cameras"] == []
    assert room["policy_basis"] == "CONFIGURED_CLASSROOM_POLICY"
    listed = await stack["client"].get("/v1/classrooms", headers=stack["viewer-1"])
    assert [item["classroom_id"] for item in listed.json()] == [room["classroom_id"]]
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        kind = await session.scalar(
            text("SELECT kind FROM areas WHERE id = :id"), {"id": room["classroom_id"]}
        )
    assert kind == "CLASSROOM", "a classroom is an Area, not a new table"


async def test_classroom_names_and_labels_are_validated(stack: dict[str, Any]) -> None:
    await create_classroom(stack, name="Room 1")
    duplicate = await stack["client"].post(
        "/v1/classrooms",
        json={"facility_id": str(stack["f1"]), "name": " Room   1 "},
        headers=stack["admin-1"],
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["category"] == "classroom_name_exists"
    for bad in ("<b>Pre-K</b>", "x" * 65):
        response = await stack["client"].post(
            "/v1/classrooms",
            json={"facility_id": str(stack["f1"]), "name": "Room 9", "age_band_label": bad},
            headers=stack["admin-1"],
        )
        assert response.status_code == 422


async def test_rename_relabel_and_clear_the_age_band(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    url = f"/v1/classrooms/{room['classroom_id']}"
    renamed = await stack["client"].patch(
        url, json={"name": "Blue room", "age_band_label": "Pre-K"}, headers=stack["admin-1"]
    )
    assert renamed.json()["name"] == "Blue room" and renamed.json()["age_band_label"] == "Pre-K"
    kept = await stack["client"].patch(url, json={"name": "Blue room 2"}, headers=stack["admin-1"])
    assert kept.json()["age_band_label"] == "Pre-K", "absent means unchanged"
    cleared = await stack["client"].patch(
        url, json={"age_band_label": None}, headers=stack["admin-1"]
    )
    assert cleared.json()["age_band_label"] is None, "explicit null clears"


async def test_an_inactive_classroom_takes_no_policy_and_is_not_evaluated(
    stack: dict[str, Any],
) -> None:
    room = await create_classroom(stack)
    base = f"/v1/classrooms/{room['classroom_id']}"
    await stack["client"].post(
        f"{base}/ratio-policies", json=policy_body(), headers=stack["admin-1"]
    )
    off = await stack["client"].post(f"{base}/deactivate", headers=stack["admin-1"])
    assert off.json()["status"] == "ARCHIVED"
    refused = await stack["client"].post(
        f"{base}/ratio-policies",
        json=policy_body(effective_from_date=(TODAY + timedelta(days=30)).isoformat()),
        headers=stack["admin-1"],
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["category"] == "classroom_inactive"
    status = (await stack["client"].get(f"{base}/ratio-status", headers=stack["viewer-1"])).json()
    assert status["evaluation"]["ratio_state"] == "NOT_CONFIGURED"
    assert status["evaluation"]["explanations"] == ["CLASSROOM_INACTIVE"]
    on = await stack["client"].post(f"{base}/activate", headers=stack["admin-1"])
    assert on.json()["status"] == "ACTIVE"


# ============================================================================= policies
async def test_a_valid_policy_is_stored_in_facility_time(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    base = f"/v1/classrooms/{room['classroom_id']}"
    start = date(2026, 9, 1)
    response = await stack["client"].post(
        f"{base}/ratio-policies",
        json=policy_body(
            effective_from_date=start.isoformat(), effective_through_date="2026-12-31"
        ),
        headers=stack["admin-1"],
    )
    assert response.status_code == 201, response.text
    stored = response.json()["policies"][0]
    # Phoenix is UTC-7 all year: local midnight is 07:00 UTC; "through 31 Dec" ends at the
    # start of 1 Jan local.
    assert stored["effective_from"] == "2026-09-01T07:00:00+00:00"
    assert stored["effective_until"] == "2027-01-01T07:00:00+00:00"
    assert stored["effective_from_date"] == "2026-09-01"
    assert stored["effective_through_date"] == "2026-12-31"
    assert (stored["max_children_per_staff"], stored["minimum_staff"]) == (5, 1)
    assert stored["maximum_group_size"] == 10 and stored["revision"] == 1
    assert stored["source_reference"] == "Owner-provided classroom policy, revision 1"


@pytest.mark.parametrize(
    ("changes", "category"),
    [
        ({"max_children_per_staff": 0}, "invalid_max_children_per_staff"),
        ({"max_children_per_staff": -2}, "invalid_max_children_per_staff"),
        ({"minimum_staff": -1}, "invalid_minimum_staff"),
        ({"maximum_group_size": 0}, "invalid_maximum_group_size"),
        (
            {"effective_from_date": "2026-10-10", "effective_through_date": "2026-10-09"},
            "effective_period_inverted",
        ),
        ({"label": "<script>"}, "invalid_policy_label"),
    ],
)
async def test_invalid_policies_are_rejected_with_the_rule(
    stack: dict[str, Any], changes: dict[str, Any], category: str
) -> None:
    room = await create_classroom(stack)
    response = await stack["client"].post(
        f"/v1/classrooms/{room['classroom_id']}/ratio-policies",
        json=policy_body(**changes),
        headers=stack["admin-1"],
    )
    assert response.status_code == 422
    assert response.json()["detail"]["category"] == category


@pytest.mark.parametrize(
    "changes",
    [{"max_children_per_staff": True}, {"max_children_per_staff": "5"}, {"extra": 1}],
)
async def test_malformed_policy_bodies_are_refused(
    stack: dict[str, Any], changes: dict[str, Any]
) -> None:
    room = await create_classroom(stack)
    response = await stack["client"].post(
        f"/v1/classrooms/{room['classroom_id']}/ratio-policies",
        json=policy_body(**changes),
        headers=stack["admin-1"],
    )
    assert response.status_code == 422


async def test_overlapping_active_policies_are_refused_adjacent_ones_accepted(
    stack: dict[str, Any],
) -> None:
    room = await create_classroom(stack)
    url = f"/v1/classrooms/{room['classroom_id']}/ratio-policies"
    first = await stack["client"].post(
        url,
        json=policy_body(effective_from_date="2026-01-01", effective_through_date="2026-06-30"),
        headers=stack["admin-1"],
    )
    assert first.status_code == 201
    overlap = await stack["client"].post(
        url, json=policy_body(effective_from_date="2026-06-30"), headers=stack["admin-1"]
    )
    assert overlap.status_code == 409
    assert overlap.json()["detail"]["category"] == "policy_period_overlaps"
    adjacent = await stack["client"].post(
        url, json=policy_body(effective_from_date="2026-07-01"), headers=stack["admin-1"]
    )
    assert adjacent.status_code == 201


async def test_edit_increments_revision_and_deactivation_frees_the_period(
    stack: dict[str, Any],
) -> None:
    room = await create_classroom(stack)
    base = f"/v1/classrooms/{room['classroom_id']}/ratio-policies"
    created = (
        await stack["client"].post(base, json=policy_body(), headers=stack["admin-1"])
    ).json()
    policy_id = created["policies"][0]["policy_id"]
    edited = await stack["client"].patch(
        f"{base}/{policy_id}", json=policy_body(max_children_per_staff=4), headers=stack["admin-1"]
    )
    assert edited.status_code == 200
    policy = edited.json()["policies"][0]
    assert policy["revision"] == 2 and policy["max_children_per_staff"] == 4
    blocked = await stack["client"].post(base, json=policy_body(), headers=stack["admin-1"])
    assert blocked.status_code == 409
    off = await stack["client"].post(f"{base}/{policy_id}/deactivate", headers=stack["admin-1"])
    assert off.json()["policies"][0]["status"] == "INACTIVE"
    assert off.json()["current_policy_id"] is None, "an inactive policy is never selected"
    stale_edit = await stack["client"].patch(
        f"{base}/{policy_id}", json=policy_body(), headers=stack["admin-1"]
    )
    assert stale_edit.status_code == 409
    assert stale_edit.json()["detail"]["category"] == "policy_inactive"
    replacement = await stack["client"].post(base, json=policy_body(), headers=stack["admin-1"])
    assert replacement.status_code == 201


async def test_future_and_expired_policies_are_not_in_effect(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    url = f"/v1/classrooms/{room['classroom_id']}/ratio-policies"
    await stack["client"].post(
        url,
        json=policy_body(
            effective_from_date=(TODAY - timedelta(days=60)).isoformat(),
            effective_through_date=(TODAY - timedelta(days=30)).isoformat(),
        ),
        headers=stack["admin-1"],
    )
    result = await stack["client"].post(
        url,
        json=policy_body(effective_from_date=(TODAY + timedelta(days=30)).isoformat()),
        headers=stack["admin-1"],
    )
    body = result.json()
    assert body["current_policy_id"] is None
    assert [policy["in_effect"] for policy in body["policies"]] == [False, False]


# ========================================================================= ratio status
async def test_ratio_status_without_presence_sources_is_insufficient_not_guessed(
    stack: dict[str, Any],
) -> None:
    room = await create_classroom(stack)
    base = f"/v1/classrooms/{room['classroom_id']}"
    unconfigured = (
        await stack["client"].get(f"{base}/ratio-status", headers=stack["viewer-1"])
    ).json()
    assert unconfigured["evaluation"]["ratio_state"] == "NOT_CONFIGURED"
    await stack["client"].post(
        f"{base}/ratio-policies", json=policy_body(), headers=stack["admin-1"]
    )
    status = (await stack["client"].get(f"{base}/ratio-status", headers=stack["viewer-1"])).json()
    evaluation = status["evaluation"]
    assert evaluation["ratio_state"] == "INSUFFICIENT_DATA"
    assert set(evaluation["explanations"]) == {"CHILD_COUNT_MISSING", "STAFF_COUNT_MISSING"}
    assert evaluation["child_count"] is None and evaluation["staff_count"] is None
    assert status["presence_connected"] is False and status["vision_connected"] is False
    assert status["reconciliation"]["state"] == "NOT_AVAILABLE"
    assert status["policy_basis"] == "CONFIGURED_CLASSROOM_POLICY"
    rendered = str(status).lower()
    for claim in ("compliant", "compliance", "legal", "law", "arizona", "certified"):
        assert claim not in rendered, claim


# =========================================================================== isolation
async def test_other_tenants_get_a_uniform_404(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    base = f"/v1/classrooms/{room['classroom_id']}"
    client, other = stack["client"], stack["owner-b"]
    unknown = f"/v1/classrooms/{uuid4()}"
    for method, url, body in (
        ("GET", base, None),
        ("GET", f"{base}/ratio-status", None),
        ("PATCH", base, {"name": "taken over"}),
        ("POST", f"{base}/ratio-policies", policy_body()),
        ("POST", f"{base}/deactivate", None),
        ("GET", unknown, None),
    ):
        response = await client.request(method, url, json=body, headers=other)
        assert response.status_code == 404, (method, url)
        assert response.json() == {"detail": "classroom not found"}
    listed = await client.get("/v1/classrooms", headers=other)
    assert listed.json() == []
    cross = await client.post(
        "/v1/classrooms",
        json={"facility_id": str(stack["f1"]), "name": "Planted"},
        headers=other,
    )
    assert cross.status_code == 404, "another tenant's facility id is not probeable"


async def test_facility_scoped_principals_see_only_their_facility(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    other_room = await create_classroom(stack, "f2", as_="owner-a", name="Room 2")
    client = stack["client"]
    visible = await client.get("/v1/classrooms", headers=stack["viewer-2"])
    assert [item["classroom_id"] for item in visible.json()] == [other_room["classroom_id"]]
    hidden = await client.get(f"/v1/classrooms/{room['classroom_id']}", headers=stack["viewer-2"])
    assert hidden.status_code == 404
    facilities = await client.get("/v1/facilities", headers=stack["viewer-2"])
    assert [item["facility_id"] for item in facilities.json()] == [str(stack["f2"])]
    # A facility admin of facility 1 can neither see nor create in facility 2.
    create_elsewhere = await client.post(
        "/v1/classrooms",
        json={"facility_id": str(stack["f2"]), "name": "Room X"},
        headers=stack["admin-1"],
    )
    assert create_elsewhere.status_code == 404
    edit_elsewhere = await client.patch(
        f"/v1/classrooms/{other_room['classroom_id']}",
        json={"name": "nope"},
        headers=stack["admin-1"],
    )
    assert edit_elsewhere.status_code == 404


async def test_viewers_read_but_cannot_change(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    base = f"/v1/classrooms/{room['classroom_id']}"
    client, viewer = stack["client"], stack["viewer-1"]
    assert (await client.get(base, headers=viewer)).status_code == 200
    assert (await client.get(f"{base}/ratio-status", headers=viewer)).status_code == 200
    for method, url, body in (
        ("POST", "/v1/classrooms", {"facility_id": str(stack["f1"]), "name": "V"}),
        ("PATCH", base, {"name": "V"}),
        ("POST", f"{base}/ratio-policies", policy_body()),
        ("POST", f"{base}/deactivate", None),
    ):
        response = await client.request(method, url, json=body, headers=viewer)
        assert response.status_code == 403, (method, url)
    facilities = (await client.get("/v1/facilities", headers=viewer)).json()
    assert facilities[0]["can_administer"] is False
    assert (await client.get(base, headers=viewer)).json()["can_administer"] is False


# ====================================================================== camera relation
async def test_cameras_are_associated_through_the_existing_zone_relation(
    stack: dict[str, Any],
) -> None:
    room = await create_classroom(stack)
    tenant = stack["tenant_a"]
    camera_id = uuid4()
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant)}
        )
        connection = CameraProviderConnection(
            id=uuid4(),
            tenant_id=tenant,
            name="Synthetic provider",
            provider_type="RING",
            status="ACTIVE",
            integration_state="ACTIVE",
        )
        zone = Zone(
            id=uuid4(),
            tenant_id=tenant,
            area_id=UUID(room["classroom_id"]),
            name="Whole room",
            status="ACTIVE",
        )
        session.add_all([connection, zone])
        await session.flush()
        session.add(
            Camera(
                id=camera_id,
                tenant_id=tenant,
                zone_id=zone.id,
                provider_connection_id=connection.id,
                provider_device_id="synthetic-device",
                provider_component_key="__single__",
                name="Testing Indoor",
                status="ACTIVE",
            )
        )
    body = (
        await stack["client"].get(
            f"/v1/classrooms/{room['classroom_id']}", headers=stack["viewer-1"]
        )
    ).json()
    assert body["cameras"] == [
        {
            "camera_id": str(camera_id),
            "name": "Testing Indoor",
            "status": "ACTIVE",
            "zone_name": "Whole room",
        }
    ]
    assert "synthetic-device" not in str(body), "provider identifiers never reach the response"


# ========================================================================= audit / RLS
async def test_policy_changes_are_audited_without_personal_data(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    base = f"/v1/classrooms/{room['classroom_id']}/ratio-policies"
    created = (
        await stack["client"].post(base, json=policy_body(), headers=stack["admin-1"])
    ).json()
    policy_id = created["policies"][0]["policy_id"]
    await stack["client"].patch(
        f"{base}/{policy_id}", json=policy_body(minimum_staff=2), headers=stack["admin-1"]
    )
    async with stack["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"),
            {"id": str(stack["tenant_a"])},
        )
        events = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.target_id.in_([UUID(policy_id), UUID(room["classroom_id"])]))
                .order_by(AuditEvent.occurred_at)
            )
        ).all()
    actions = [event.action for event in events]
    assert actions == ["classroom.created", "ratio_policy.created", "ratio_policy.updated"]
    updated = events[-1].metadata_
    assert updated["before"]["minimum_staff"] == 1 and updated["after"]["minimum_staff"] == 2


async def test_rls_fails_closed_and_isolates_tenants(stack: dict[str, Any]) -> None:
    room = await create_classroom(stack)
    await stack["client"].post(
        f"/v1/classrooms/{room['classroom_id']}/ratio-policies",
        json=policy_body(),
        headers=stack["admin-1"],
    )
    async with stack["runtime"]() as session, session.begin():
        no_context = await session.scalar(text("SELECT count(*) FROM classroom_ratio_policies"))
        assert no_context == 0, "no tenant context, no rows"
    async with stack["runtime"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_b"])}
        )
        assert await session.scalar(text("SELECT count(*) FROM classroom_ratio_policies")) == 0
        with pytest.raises(Exception, match="row-level security|violates"):
            session.add(
                ClassroomRatioPolicy(
                    id=uuid4(),
                    tenant_id=stack["tenant_a"],
                    area_id=UUID(room["classroom_id"]),
                    label="planted",
                    max_children_per_staff=5,
                    minimum_staff=0,
                    effective_from=datetime.now(UTC),
                    status="ACTIVE",
                    revision=1,
                )
            )
            await session.flush()


async def test_the_runtime_role_is_restricted_on_classroom_tables(
    stack: dict[str, Any], runtime_role_name: str
) -> None:
    expected = {
        "facilities": {"SELECT"},
        "zones": {"SELECT"},
        "areas": {"SELECT", "INSERT", "UPDATE"},
        "classroom_ratio_policies": {"SELECT", "INSERT", "UPDATE"},
    }
    async with stack["admin"]() as session, session.begin():
        for table, allowed in expected.items():
            granted = {
                privilege
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if await session.scalar(
                    text("SELECT has_table_privilege(:role, :table, :privilege)"),
                    {"role": runtime_role_name, "table": f"public.{table}", "privilege": privilege},
                )
            }
            assert granted == allowed, (table, granted)
