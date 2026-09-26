"""Staff enrollment over HTTP, the real runtime role and RLS (V1-02A).

Two tenants, an owner and a viewer, a deterministic fake face backend, a temporary private
media store. Proves the lifecycle (create, photos, readiness, activate/deactivate, delete),
the validation categories, tenant isolation and IDOR safety, the upload body guard, that
templates never appear in any response or log, and that deletion removes bytes and revokes
templates.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import func, select, text

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.face_backend import FakeFaceBackend
from veotrex_api.identity import ExternalIdentity, IdentityVerificationError
from veotrex_api.main import create_app
from veotrex_api.models import AuditEvent, StaffEnrollmentImage, StaffFaceTemplate, StaffProfile
from veotrex_api.staff_media import StaffMediaStore

ISSUER = "https://tenant.auth0.example/"


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
        return SecretStr("staff-test-secret")


class NoRing:
    async def aclose(self) -> None:
        return None


def jpeg(color: tuple[int, int, int], size: tuple[int, int] = (400, 400)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


ONE_FACE = [jpeg((90 + step * 20, 100, 110)) for step in range(6)]
NO_FACE = jpeg((250, 10, 10))
TWO_FACES = jpeg((10, 10, 250))


async def seed_tenant(  # type: ignore[no-untyped-def]
    admin_factory, label: str, organization: str, subjects: dict[str, str]
) -> UUID:
    tenant_id = uuid4()
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
            {"id": tenant_id, "name": f"Staff tenant {label}"},
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
        for subject, role in subjects.items():
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
                    "INSERT INTO role_assignments (id, tenant_id, actor_id, role) "
                    "VALUES (:id, :tenant, :actor, :role)"
                ),
                {"id": uuid4(), "tenant": tenant_id, "actor": actor_id, "role": role},
            )
    return tenant_id


@pytest.fixture
async def stack(settings: Settings, admin_settings: Settings, tmp_path: Path):  # type: ignore[no-untyped-def]
    admin_engine = make_engine(admin_settings)
    admin_factory = make_session_factory(admin_engine)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    org_a, org_b = f"org_{uuid4().hex}", f"org_{uuid4().hex}"
    owner_a, viewer_a, owner_b = (f"auth0|{uuid4().hex}" for _ in range(3))
    tenant_a = await seed_tenant(
        admin_factory, "A", org_a, {owner_a: "TENANT_OWNER", viewer_a: "VIEWER"}
    )
    tenant_b = await seed_tenant(admin_factory, "B", org_b, {owner_b: "TENANT_OWNER"})
    verifier = StubIdentityVerifier(
        {
            "owner-a": ExternalIdentity("auth0", ISSUER, owner_a, org_a),
            "viewer-a": ExternalIdentity("auth0", ISSUER, viewer_a, org_a),
            "owner-b": ExternalIdentity("auth0", ISSUER, owner_b, org_b),
        }
    )
    media = StaffMediaStore(tmp_path / "media")
    app = create_app(
        settings,
        engine,
        verifier,
        factory,
        credential_vault=InMemoryCredentialVault(),
        ring_client=NoRing(),  # type: ignore[arg-type]
        secret_resolver=Secrets(),
        face_backend=FakeFaceBackend(),
        staff_media=media,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {
            "client": client,
            "admin": admin_factory,
            "media": media,
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
            "owner_a": {"Authorization": "Bearer owner-a"},
            "viewer_a": {"Authorization": "Bearer viewer-a"},
            "owner_b": {"Authorization": "Bearer owner-b"},
        }
    await engine.dispose()
    await admin_engine.dispose()


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    staff_id: str,
    data: bytes,
    media_type: str = "image/jpeg",
) -> Any:
    return await client.post(
        f"/v1/staff/{staff_id}/enrollment-images",
        content=data,
        headers={**headers, "content-type": media_type},
    )


async def test_full_lifecycle_readiness_and_deletion(stack: dict[str, Any]) -> None:
    client: AsyncClient = stack["client"]
    owner = stack["owner_a"]
    created = await client.post(
        "/v1/staff", json={"display_name": "  Chandra   Pandey "}, headers=owner
    )
    assert created.status_code == 201, created.text
    staff = created.json()
    staff_id = staff["staff_id"]
    assert staff["display_name"] == "Chandra Pandey"
    assert staff["status"] == "ACTIVE" and staff["enrollment_state"] == "EMPTY"
    assert staff["accepted_images"] == 0 and staff["required_images"] == 3
    assert staff["maximum_images"] == 5 and staff["recognition_ready"] is False

    # Two photos: COLLECTING, not ready. One accidental image can never make a teacher READY.
    for index in range(2):
        response = await upload(client, owner, staff_id, ONE_FACE[index])
        assert response.status_code == 201, response.text
        assert response.json()["template_state"] == "READY"
        assert "template" not in response.text.replace("template_state", "")
    listed = (await client.get(f"/v1/staff/{staff_id}", headers=owner)).json()
    assert listed["enrollment_state"] == "COLLECTING" and listed["accepted_images"] == 2
    assert listed["recognition_ready"] is False

    # Third photo: READY.
    assert (await upload(client, owner, staff_id, ONE_FACE[2])).status_code == 201
    ready = (await client.get(f"/v1/staff/{staff_id}", headers=owner)).json()
    assert ready["enrollment_state"] == "READY" and ready["recognition_ready"] is True

    images = (await client.get(f"/v1/staff/{staff_id}/enrollment-images", headers=owner)).json()
    assert len(images) == 3
    for image in images:
        assert set(image) == {
            "image_id",
            "width",
            "height",
            "byte_size",
            "face_size_px",
            "quality",
            "template_state",
            "created_at",
        }
    # Thumbnail bytes are the canonical JPEG, private, only to the owning tenant.
    content = await client.get(
        f"/v1/staff/{staff_id}/enrollment-images/{images[0]['image_id']}/content", headers=owner
    )
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/jpeg"
    assert content.headers["cache-control"] == "private, no-store"
    assert content.content.startswith(b"\xff\xd8\xff")

    # Deactivate: still READY but not recognition-ready; reactivate restores.
    off = await client.post(f"/v1/staff/{staff_id}/deactivate", headers=owner)
    assert off.json()["status"] == "INACTIVE" and off.json()["recognition_ready"] is False
    assert off.json()["enrollment_state"] == "READY"
    assert (await upload(client, owner, staff_id, ONE_FACE[3])).status_code == 422
    on = await client.post(f"/v1/staff/{staff_id}/activate", headers=owner)
    assert on.json()["status"] == "ACTIVE" and on.json()["recognition_ready"] is True

    # Removing one photo drops below the minimum: COLLECTING again, template revoked.
    removed = await client.delete(
        f"/v1/staff/{staff_id}/enrollment-images/{images[0]['image_id']}", headers=owner
    )
    assert removed.status_code == 200
    assert removed.json()["enrollment_state"] == "COLLECTING"
    assert removed.json()["accepted_images"] == 2 and removed.json()["recognition_ready"] is False
    assert (
        await client.get(
            f"/v1/staff/{staff_id}/enrollment-images/{images[0]['image_id']}/content", headers=owner
        )
    ).status_code == 404

    # Rename, then delete: 404 afterwards, bytes gone, templates revoked, audit trail present.
    renamed = await client.patch(
        f"/v1/staff/{staff_id}", json={"display_name": "C. Pandey"}, headers=owner
    )
    assert renamed.json()["display_name"] == "C. Pandey"
    media: StaffMediaStore = stack["media"]
    tenant_dir = media.root / str(stack["tenant_a"])
    assert len(list(tenant_dir.glob("*.jpg"))) == 2
    deleted = await client.delete(f"/v1/staff/{staff_id}", headers=owner)
    assert deleted.status_code == 204
    assert (await client.get(f"/v1/staff/{staff_id}", headers=owner)).status_code == 404
    assert [row["staff_id"] for row in (await client.get("/v1/staff", headers=owner)).json()] == []
    assert list(tenant_dir.glob("*.jpg")) == []
    async with stack["admin"]() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        profile = await session.get(StaffProfile, UUID(staff_id))
        assert profile is not None and profile.status == "DELETED" and profile.deleted_at
        states = (
            await session.scalars(
                select(StaffFaceTemplate.status).where(
                    StaffFaceTemplate.staff_profile_id == UUID(staff_id)
                )
            )
        ).all()
        assert states and set(states) == {"REVOKED"}
        keys = (
            await session.scalars(
                select(StaffEnrollmentImage.media_key).where(
                    StaffEnrollmentImage.staff_profile_id == UUID(staff_id)
                )
            )
        ).all()
        assert set(keys) == {None}
        actions = (
            await session.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.target_id == UUID(staff_id))
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        ).all()
        assert actions == [
            "staff.created",
            "staff.image_accepted",
            "staff.image_accepted",
            "staff.image_accepted",
            "staff.deactivated",
            "staff.activated",
            "staff.image_removed",
            "staff.renamed",
            "staff.deleted",
        ]


@pytest.mark.parametrize(
    ("data", "media_type", "expected_status", "category"),
    [
        (NO_FACE, "image/jpeg", 422, "no_face_detected"),
        (TWO_FACES, "image/jpeg", 422, "multiple_faces"),
        (jpeg((100, 100, 100), (200, 400)), "image/jpeg", 422, "face_too_small"),
        (b"GIF89a" + b"\x00" * 300, "image/jpeg", 422, "unsupported_type"),
        (ONE_FACE[0][:600], "image/jpeg", 422, "invalid_image"),
        (jpeg((100, 100, 100), (100, 100)), "image/png", 422, "image_too_small"),
        (ONE_FACE[0], "text/plain", 415, None),
        (ONE_FACE[0], "application/json", 415, None),
    ],
)
async def test_upload_rejections_are_bounded_categories(
    stack: dict[str, Any], data: bytes, media_type: str, expected_status: int, category: str | None
) -> None:
    client: AsyncClient = stack["client"]
    owner = stack["owner_a"]
    staff_id = (
        await client.post("/v1/staff", json={"display_name": "Rejects"}, headers=owner)
    ).json()["staff_id"]
    response = await upload(client, owner, staff_id, data, media_type)
    assert response.status_code == expected_status, response.text
    if category:
        assert response.json()["detail"]["category"] == category
    assert (await client.get(f"/v1/staff/{staff_id}", headers=owner)).json()["accepted_images"] == 0


async def test_duplicate_and_limit_rejections(stack: dict[str, Any]) -> None:
    client: AsyncClient = stack["client"]
    owner = stack["owner_a"]
    staff_id = (
        await client.post("/v1/staff", json={"display_name": "Limits"}, headers=owner)
    ).json()["staff_id"]
    assert (await upload(client, owner, staff_id, ONE_FACE[0])).status_code == 201
    duplicate = await upload(client, owner, staff_id, ONE_FACE[0])
    assert (
        duplicate.status_code == 422 and duplicate.json()["detail"]["category"] == "duplicate_image"
    )
    for index in range(1, 5):
        assert (await upload(client, owner, staff_id, ONE_FACE[index])).status_code == 201
    sixth = await upload(client, owner, staff_id, ONE_FACE[5])
    assert sixth.status_code == 422
    assert sixth.json()["detail"]["category"] == "enrollment_limit_reached"
    profile = (await client.get(f"/v1/staff/{staff_id}", headers=owner)).json()
    assert profile["accepted_images"] == 5 and profile["enrollment_state"] == "READY"


async def test_oversized_upload_is_refused_by_the_body_guard(
    stack: dict[str, Any], settings: Settings
) -> None:
    client: AsyncClient = stack["client"]
    owner = stack["owner_a"]
    staff_id = (await client.post("/v1/staff", json={"display_name": "Big"}, headers=owner)).json()[
        "staff_id"
    ]
    too_big = b"\xff\xd8\xff" + b"\x00" * settings.staff_enrollment_image_bytes
    response = await upload(client, owner, staff_id, too_big)
    assert response.status_code == 413
    # The guard applies to the upload route only: listing on the same path is untouched.
    assert (
        await client.get(f"/v1/staff/{staff_id}/enrollment-images", headers=owner)
    ).status_code == 200


async def test_viewer_reads_but_cannot_mutate(stack: dict[str, Any]) -> None:
    client: AsyncClient = stack["client"]
    owner, viewer = stack["owner_a"], stack["viewer_a"]
    staff_id = (
        await client.post("/v1/staff", json={"display_name": "Seen"}, headers=owner)
    ).json()["staff_id"]
    assert (await client.get("/v1/staff", headers=viewer)).status_code == 200
    assert (await client.get(f"/v1/staff/{staff_id}", headers=viewer)).status_code == 200
    assert (
        await client.post("/v1/staff", json={"display_name": "x"}, headers=viewer)
    ).status_code == 403
    assert (
        await client.patch(f"/v1/staff/{staff_id}", json={"display_name": "x"}, headers=viewer)
    ).status_code == 403
    assert (await upload(client, viewer, staff_id, ONE_FACE[0])).status_code == 403
    assert (
        await client.post(f"/v1/staff/{staff_id}/deactivate", headers=viewer)
    ).status_code == 403
    assert (await client.delete(f"/v1/staff/{staff_id}", headers=viewer)).status_code == 403
    assert (await client.get("/v1/staff")).status_code == 401


async def test_cross_tenant_identifiers_are_indistinguishable_from_unknown(
    stack: dict[str, Any],
) -> None:
    client: AsyncClient = stack["client"]
    owner_a, owner_b = stack["owner_a"], stack["owner_b"]
    staff_id = (
        await client.post("/v1/staff", json={"display_name": "Private"}, headers=owner_a)
    ).json()["staff_id"]
    assert (await upload(client, owner_a, staff_id, ONE_FACE[0])).status_code == 201
    image_id = (
        await client.get(f"/v1/staff/{staff_id}/enrollment-images", headers=owner_a)
    ).json()[0]["image_id"]
    assert (await client.get("/v1/staff", headers=owner_b)).json() == []
    unknown = uuid4()
    for method, path in (
        ("GET", "/v1/staff/{id}"),
        ("PATCH", "/v1/staff/{id}"),
        ("POST", "/v1/staff/{id}/activate"),
        ("POST", "/v1/staff/{id}/deactivate"),
        ("DELETE", "/v1/staff/{id}"),
        ("GET", "/v1/staff/{id}/enrollment-images"),
        ("GET", f"/v1/staff/{{id}}/enrollment-images/{image_id}/content"),
        ("DELETE", f"/v1/staff/{{id}}/enrollment-images/{image_id}"),
    ):
        kwargs: dict[str, Any] = {"headers": owner_b}
        if method == "PATCH":
            kwargs["json"] = {"display_name": "hijack"}
        cross = await client.request(method, path.format(id=staff_id), **kwargs)
        missing = await client.request(method, path.format(id=unknown), **kwargs)
        assert cross.status_code == missing.status_code == 404, (method, path, cross.text)
        assert cross.text == missing.text
        assert "Private" not in cross.text
    cross_upload = await upload(client, owner_b, staff_id, ONE_FACE[1])
    assert cross_upload.status_code == 404
    # Tenant A's data is untouched.
    profile = (await client.get(f"/v1/staff/{staff_id}", headers=owner_a)).json()
    assert profile["display_name"] == "Private" and profile["accepted_images"] == 1


async def test_templates_never_leave_the_api_and_logs_carry_no_biometric_material(
    stack: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    client: AsyncClient = stack["client"]
    owner = stack["owner_a"]
    events: list[dict[str, object]] = []

    def sink(_: object, __: str, event: dict[str, object]) -> dict[str, object]:
        events.append(dict(event))
        return event

    previous = structlog.get_config()
    structlog.configure(processors=[sink, structlog.processors.KeyValueRenderer()])
    caplog.set_level(logging.DEBUG)
    try:
        staff_id = (
            await client.post("/v1/staff", json={"display_name": "Quiet"}, headers=owner)
        ).json()["staff_id"]
        for index in range(3):
            assert (await upload(client, owner, staff_id, ONE_FACE[index])).status_code == 201
        bodies = [
            (await client.get("/v1/staff", headers=owner)).text,
            (await client.get(f"/v1/staff/{staff_id}", headers=owner)).text,
            (await client.get(f"/v1/staff/{staff_id}/enrollment-images", headers=owner)).text,
        ]
    finally:
        structlog.configure(**previous)
    async with stack["admin"]() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(stack["tenant_a"])}
        )
        templates = (
            await session.scalars(
                select(StaffFaceTemplate.template).where(
                    StaffFaceTemplate.staff_profile_id == UUID(staff_id)
                )
            )
        ).all()
        count = await session.scalar(
            select(func.count())
            .select_from(StaffFaceTemplate)
            .where(StaffFaceTemplate.staff_profile_id == UUID(staff_id))
        )
    assert count == 3 and all(len(blob) == 512 for blob in templates)
    import base64

    for blob in templates:
        encoded = base64.b64encode(blob).decode()
        for body in bodies:
            assert encoded[:24] not in body and blob.hex()[:24] not in body
            assert '"template"' not in body and "data_base64" not in body
        for event in events:
            rendered = repr(event)
            assert encoded[:24] not in rendered and blob.hex()[:24] not in rendered
    for record in caplog.records:
        message = record.getMessage()
        assert "data_base64" not in message and "\\xff\\xd8" not in message
    # The one enrollment log event carries dimensions only.
    accepted = [
        event for event in events if event.get("event") == "staff_enrollment_image_accepted"
    ]
    assert len(accepted) == 3
    assert set(accepted[0]) == {"event", "width", "height", "byte_size"}
    # No route serves a template: the OpenAPI surface has no template path.
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert not any("template" in path for path in paths)
    # V1-04C adds facility- and classroom-scoped roster routes. They carry designations and
    # check-in events only (no image, template or face data) and are enumerated here so any
    # other staff path outside /v1/staff still fails.
    roster_paths = {
        "/v1/facilities/{facility_id}/staff-ratio-eligibility",
        "/v1/facilities/{facility_id}/staff-ratio-eligibility/{eligibility_id}",
        "/v1/facilities/{facility_id}/staff-ratio-eligibility/{eligibility_id}/deactivate",
        "/v1/classrooms/{classroom_id}/staff-presence",
        "/v1/classrooms/{classroom_id}/staff-presence/check-in",
        "/v1/classrooms/{classroom_id}/staff-presence/refresh",
        "/v1/classrooms/{classroom_id}/staff-presence/check-out",
    }
    assert roster_paths <= set(paths)
    assert all(
        path.startswith("/v1/staff") or path in roster_paths or "staff" not in path
        for path in paths
    )
