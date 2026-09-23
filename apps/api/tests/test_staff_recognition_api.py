"""Local recognition over HTTP, the real runtime role and RLS (V1-02B0). EVALUATION ONLY.

Two tenants, three teachers, a deterministic scripted backend and a temporary private media
store. The scripted backend places each photo's vector at an angle taken from the image's
green channel, so every expected similarity in this file is a cosine that can be written down:
a test can put a query exactly on top of a teacher, exactly between two, or nowhere near
either, and assert the decision rather than a measurement.

The suite proves the decisions (MATCH, UNKNOWN below threshold, UNKNOWN on ambiguity), every
way a person must stop being recognisable (deactivated, deleted, template revoked, model
changed, different tenant), and the privacy properties: the test image is not persisted, no
template reaches a response or a log, and the route does not exist where it must not.
"""

from __future__ import annotations

import io
import logging
import math
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
from veotrex_api.face_backend import FaceQuery, FaceTemplate, FakeFaceBackend
from veotrex_api.face_matching import encode, normalize
from veotrex_api.identity import ExternalIdentity, IdentityVerificationError
from veotrex_api.main import create_app
from veotrex_api.models import StaffEnrollmentImage, StaffFaceTemplate
from veotrex_api.staff_media import StaffMediaStore

ISSUER = "https://tenant.auth0.example/"
RECOGNITION_PATH = "/v1/staff/recognition-test"
DIMENSIONS = 128
# Distinguishes "use the suite's scripted backend" from "inject nothing at all".
DEFAULT = object()


class ScriptedFaceBackend(FakeFaceBackend):
    """The fake backend's face-count rules, with a vector placed at a chosen angle.

    ``photo(degrees)`` encodes the angle in the green channel; the vector is that angle on a
    circle spanned by the first two dimensions. Two photos therefore have a cosine similarity
    of exactly the cosine of the difference between their angles, which is what makes the
    decisions in this file assertable rather than approximate.
    """

    model_id = "scripted-face"
    model_version = "1"

    def _vector(self, image: Image.Image) -> Any:
        degrees = round(self._mean_rgb(image)[1])
        radians = math.radians(degrees)
        return normalize([math.cos(radians), math.sin(radians)] + [0.0] * (DIMENSIONS - 2))


class RelabelledBackend(ScriptedFaceBackend):
    """The same vectors under a different model version: every stored template becomes foreign
    and must stop being a candidate."""

    model_version = "2"


def photo(degrees: int, size: tuple[int, int] = (400, 400)) -> bytes:
    """A solid-colour JPEG whose green channel carries the angle. Red and blue are held where
    the inherited face-count rules see exactly one face."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (100, degrees, 110)).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


NO_FACE = (250, 10, 10)
TWO_FACES = (10, 10, 250)


def solid(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), color).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


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
        return SecretStr("recognition-test-secret")


class NoRing:
    async def aclose(self) -> None:
        return None


async def seed_tenant(  # type: ignore[no-untyped-def]
    admin_factory, label: str, organization: str, subjects: dict[str, str]
) -> UUID:
    """One tenant, its Auth0 organization binding and its actors. Mirrors the V1-02A staff
    suite's fixture; the recognition tests need the same two-tenant shape to prove isolation."""
    tenant_id = uuid4()
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, :name, 'ACTIVE')"),
            {"id": tenant_id, "name": f"Recognition tenant {label}"},
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

    def application(backend: Any = DEFAULT, **overrides: Any):  # type: ignore[no-untyped-def]
        """An application over the same engine and media store. ``backend=None`` means "inject
        nothing", so ``main`` resolves the backend from settings exactly as a deployment does;
        that is what the route-absence tests need."""
        resolved = settings if not overrides else settings.model_copy(update=overrides)
        return create_app(
            resolved,
            engine,
            verifier,
            factory,
            credential_vault=InMemoryCredentialVault(),
            ring_client=NoRing(),  # type: ignore[arg-type]
            secret_resolver=Secrets(),
            face_backend=ScriptedFaceBackend() if backend is DEFAULT else backend,
            staff_media=media,
        )

    app = application()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {
            "client": client,
            "application": application,
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


async def enroll(client: AsyncClient, headers: dict[str, str], name: str, angles: list[int]) -> str:
    """Create a teacher and give them enrollment photos at the named angles."""
    created = await client.post("/v1/staff", json={"display_name": name}, headers=headers)
    assert created.status_code == 201, created.text
    staff_id = created.json()["staff_id"]
    for angle in angles:
        uploaded = await client.post(
            f"/v1/staff/{staff_id}/enrollment-images",
            content=photo(angle),
            headers={**headers, "content-type": "image/jpeg"},
        )
        assert uploaded.status_code == 201, uploaded.text
    return str(staff_id)


async def ask(
    client: AsyncClient, headers: dict[str, str], data: bytes
) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        RECOGNITION_PATH, content=data, headers={**headers, "content-type": "image/jpeg"}
    )
    body = response.json() if response.content else {}
    return response.status_code, body


# --------------------------------------------------------------------------------- decisions
async def test_a_new_photo_of_an_enrolled_teacher_is_matched(stack: dict[str, Any]) -> None:
    client, owner = stack["client"], stack["owner_a"]
    chandra = await enroll(client, owner, "Chandra", [10, 12, 14])
    await enroll(client, owner, "Friend A", [98, 100, 102])

    status, body = await ask(client, owner, photo(11))
    assert status == 200, body
    assert body["decision"] == "MATCH"
    assert body["staff_id"] == chandra
    assert body["display_name"] == "Chandra"
    assert body["score"] > 0.99
    assert body["evaluation_only"] is True
    assert body["candidates"] == 2
    assert body["reason"] is None


async def test_an_unenrolled_person_is_unknown(stack: dict[str, Any]) -> None:
    client, owner = stack["client"], stack["owner_a"]
    await enroll(client, owner, "Chandra", [10, 12, 14])
    await enroll(client, owner, "Friend A", [98, 100, 102])

    status, body = await ask(client, owner, photo(190))
    assert status == 200
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "below_threshold"
    assert body["staff_id"] is None
    assert body["display_name"] is None


async def test_a_person_resembling_two_teachers_is_unknown_not_the_closer_one(
    stack: dict[str, Any],
) -> None:
    """The false-identity case the margin exists for. Both teachers score well above the
    threshold; naming the marginal winner would be exactly the wrong answer."""
    client, owner = stack["client"], stack["owner_a"]
    await enroll(client, owner, "Chandra", [10, 12, 14])
    await enroll(client, owner, "Friend A", [98, 100, 102])

    status, body = await ask(client, owner, photo(56))
    assert status == 200
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "ambiguous"
    assert body["score"] > body["threshold"]
    assert body["staff_id"] is None


async def test_with_nobody_enrolled_every_photo_is_unknown(stack: dict[str, Any]) -> None:
    status, body = await ask(stack["client"], stack["owner_a"], photo(10))
    assert status == 200
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "no_candidates"
    assert body["candidates"] == 0


async def test_a_half_enrolled_teacher_cannot_be_matched(stack: dict[str, Any]) -> None:
    """Two photos is COLLECTING, not READY. A person who has not finished enrolling must not
    be nameable, however close the photo is."""
    client, owner = stack["client"], stack["owner_a"]
    await enroll(client, owner, "Chandra", [10, 12])

    status, body = await ask(client, owner, photo(10))
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "no_candidates"


# --------------------------------------------------------------- ways to stop being recognised
async def test_a_deactivated_teacher_cannot_be_matched(stack: dict[str, Any]) -> None:
    client, owner = stack["client"], stack["owner_a"]
    chandra = await enroll(client, owner, "Chandra", [10, 12, 14])
    assert (await ask(client, owner, photo(11)))[1]["decision"] == "MATCH"

    deactivated = await client.post(f"/v1/staff/{chandra}/deactivate", headers=owner)
    assert deactivated.status_code == 200

    status, body = await ask(client, owner, photo(11))
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "no_candidates"

    # Reactivating restores recognition: deactivation is a switch, not a deletion.
    await client.post(f"/v1/staff/{chandra}/activate", headers=owner)
    assert (await ask(client, owner, photo(11)))[1]["decision"] == "MATCH"


async def test_a_deleted_teacher_cannot_be_matched(stack: dict[str, Any]) -> None:
    client, owner = stack["client"], stack["owner_a"]
    chandra = await enroll(client, owner, "Chandra", [10, 12, 14])
    assert (await ask(client, owner, photo(11)))[1]["decision"] == "MATCH"

    deleted = await client.delete(f"/v1/staff/{chandra}", headers=owner)
    assert deleted.status_code == 204

    status, body = await ask(client, owner, photo(11))
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "no_candidates"


async def test_a_revoked_template_stops_contributing_to_its_teachers_score(
    stack: dict[str, Any],
) -> None:
    """Removing one photo of four leaves the teacher READY, so the profile is still a
    candidate. The revoked template must nevertheless stop being compared: here it is the only
    one near the query, and removing it must turn a MATCH into UNKNOWN."""
    client, owner = stack["client"], stack["owner_a"]
    chandra = await enroll(client, owner, "Chandra", [10, 12, 14, 80])
    assert (await ask(client, owner, photo(80)))[1]["decision"] == "MATCH"

    images = (await client.get(f"/v1/staff/{chandra}/enrollment-images", headers=owner)).json()
    assert len(images) == 4
    removed = await client.delete(
        f"/v1/staff/{chandra}/enrollment-images/{images[3]['image_id']}", headers=owner
    )
    assert removed.status_code == 200
    assert removed.json()["enrollment_state"] == "READY"

    status, body = await ask(client, owner, photo(80))
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "below_threshold"
    assert body["candidates"] == 1


async def test_templates_from_a_different_model_version_are_never_compared(
    stack: dict[str, Any],
) -> None:
    """Changing the model invalidates every stored template. Comparing a new model's vector
    against an old model's template is meaningless, so they must not meet at all."""
    client, owner = stack["client"], stack["owner_a"]
    await enroll(client, owner, "Chandra", [10, 12, 14])
    assert (await ask(client, owner, photo(11)))[1]["decision"] == "MATCH"

    relabelled = stack["application"](RelabelledBackend())
    async with AsyncClient(
        transport=ASGITransport(app=relabelled), base_url="http://test"
    ) as other:
        status, body = await ask(other, owner, photo(11))
    assert body["decision"] == "UNKNOWN"
    assert body["reason"] == "no_candidates"
    assert body["candidates"] == 0
    assert body["model_version"] == "2"


async def test_one_tenants_teachers_are_invisible_to_another(stack: dict[str, Any]) -> None:
    client = stack["client"]
    await enroll(client, stack["owner_a"], "Chandra", [10, 12, 14])
    await enroll(client, stack["owner_b"], "Someone Else", [200, 202, 204])

    # Tenant B asks with a photo of tenant A's teacher and is told nothing about them.
    status, body = await ask(client, stack["owner_b"], photo(11))
    assert status == 200
    assert body["decision"] == "UNKNOWN"
    assert body["candidates"] == 1  # B's own teacher, never A's
    assert body["display_name"] is None

    # And tenant A's own answer is unaffected by B's roster.
    assert (await ask(client, stack["owner_a"], photo(11)))[1]["display_name"] == "Chandra"


# ------------------------------------------------------------------------------ input handling
async def test_the_same_image_rules_apply_to_a_test_photo(stack: dict[str, Any]) -> None:
    client, owner = stack["client"], stack["owner_a"]
    await enroll(client, owner, "Chandra", [10, 12, 14])

    for data, category in (
        (solid(NO_FACE), "no_face_detected"),
        (solid(TWO_FACES), "multiple_faces"),
        (b"", "empty_upload"),
        (b"not an image at all", "unsupported_type"),
    ):
        status, body = await ask(client, owner, data)
        assert status == 422, (category, status, body)
        assert body["detail"]["category"] == category


async def test_an_oversized_test_photo_is_refused_by_the_body_guard(
    stack: dict[str, Any],
) -> None:
    """The recognition route carries a raw image body, so the same middleware bound applies to
    it; the bytes must be refused before anything reads them."""
    client, owner = stack["client"], stack["owner_a"]
    response = await client.post(
        RECOGNITION_PATH,
        content=b"\xff\xd8\xff" + b"\x00" * 9_000_000,
        headers={**owner, "content-type": "image/jpeg"},
    )
    assert response.status_code == 413


async def test_an_unsupported_media_type_is_refused(stack: dict[str, Any]) -> None:
    response = await stack["client"].post(
        RECOGNITION_PATH,
        content=b"{}",
        headers={**stack["owner_a"], "content-type": "application/json"},
    )
    assert response.status_code == 415


async def test_a_viewer_cannot_run_a_recognition_test(stack: dict[str, Any]) -> None:
    """Recognition is an operator power. A read-only role can see the roster but must not be
    able to submit new biometric material for comparison."""
    client = stack["client"]
    await enroll(client, stack["owner_a"], "Chandra", [10, 12, 14])
    response = await client.post(
        RECOGNITION_PATH,
        content=photo(11),
        headers={**stack["viewer_a"], "content-type": "image/jpeg"},
    )
    assert response.status_code == 403


async def test_an_unauthenticated_request_is_refused(stack: dict[str, Any]) -> None:
    response = await stack["client"].post(
        RECOGNITION_PATH, content=photo(11), headers={"content-type": "image/jpeg"}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------------- the gate
async def test_the_route_does_not_exist_where_recognition_is_not_permitted(
    stack: dict[str, Any], settings: Settings
) -> None:
    """In production the route is not registered at all, so no recognition can be performed.

    The response is 405 rather than 404 only because the literal path still matches the
    ``/v1/staff/{staff_id}`` template, which has no POST. That is not special handling: an
    arbitrary unregistered name under the same prefix answers identically, which is what the
    second request here establishes.
    """
    production = settings.model_copy(
        update={"environment": "production", "staff_face_backend": "unavailable"}
    )
    assert not production.face_evaluation_permitted
    app = stack["application"](None, environment="production", staff_face_backend="unavailable")
    assert RECOGNITION_PATH not in {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert app.state.staff_recognition_service is None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            RECOGNITION_PATH,
            content=photo(11),
            headers={**stack["owner_a"], "content-type": "image/jpeg"},
        )
        unrelated = await client.post(
            "/v1/staff/no-such-route",
            content=photo(11),
            headers={**stack["owner_a"], "content-type": "image/jpeg"},
        )
    assert refused.status_code == unrelated.status_code == 405
    assert "decision" not in refused.text


async def test_the_route_is_absent_when_the_backend_cannot_recognise(
    stack: dict[str, Any],
) -> None:
    """A permitted environment is not enough on its own: with the fail-closed backend there is
    nothing to recognise with, so no recognition service and no route are created."""
    app = stack["application"](None, staff_face_backend="unavailable")
    assert RECOGNITION_PATH not in {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert app.state.staff_recognition_service is None


# ------------------------------------------------------------------------------ privacy
async def test_the_test_photo_is_never_persisted(stack: dict[str, Any]) -> None:
    """Nothing about the query may survive the request: no row, no template, no file."""
    client, owner, admin = stack["client"], stack["owner_a"], stack["admin"]
    await enroll(client, owner, "Chandra", [10, 12, 14])
    media_root: Path = stack["media"].root

    async def counts() -> tuple[int, int]:
        async with admin() as session, session.begin():
            images = await session.scalar(select(func.count()).select_from(StaffEnrollmentImage))
            templates = await session.scalar(select(func.count()).select_from(StaffFaceTemplate))
        return int(images or 0), int(templates or 0)

    before_rows = await counts()
    before_files = sorted(path.name for path in media_root.rglob("*") if path.is_file())

    for angle in (11, 56, 190):
        assert (await ask(client, owner, photo(angle)))[0] == 200

    assert await counts() == before_rows
    assert sorted(path.name for path in media_root.rglob("*") if path.is_file()) == before_files


async def test_no_template_material_reaches_the_response(stack: dict[str, Any]) -> None:
    client, owner = stack["client"], stack["owner_a"]
    await enroll(client, owner, "Chandra", [10, 12, 14])
    response = await client.post(
        RECOGNITION_PATH, content=photo(11), headers={**owner, "content-type": "image/jpeg"}
    )
    body = response.json()
    assert set(body) == {
        "decision",
        "staff_id",
        "display_name",
        "score",
        "runner_up_score",
        "reason",
        "candidates",
        "model_id",
        "model_version",
        "threshold",
        "margin",
        "evaluation_only",
    }
    # A 128-float template is 512 bytes; nothing in the response is remotely that long.
    assert all(len(str(value)) < 64 for value in body.values())
    assert "template" not in response.text.lower()
    assert "embedding" not in response.text.lower()


async def test_no_template_material_reaches_the_logs(
    stack: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The evaluation log records the decision and the score, and deliberately not who was
    recognised: an evaluation must not accumulate a record of who was seen when."""
    client, owner = stack["client"], stack["owner_a"]
    await enroll(client, owner, "Chandra", [10, 12, 14])

    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    try:
        with caplog.at_level(logging.DEBUG):
            assert (await ask(client, owner, photo(11)))[1]["decision"] == "MATCH"
    finally:
        structlog.reset_defaults()

    recorded = "\n".join(record.getMessage() for record in caplog.records)
    assert "staff_recognition_test" in recorded
    assert "Chandra" not in recorded
    for marker in ("template", "embedding", "vector", "data_base64"):
        assert marker not in recorded.lower()


async def test_an_unusable_stored_template_drops_that_photo_and_not_the_person(
    stack: dict[str, Any],
) -> None:
    """A corrupt row must neither be compared against nor make its owner unrecognisable. Here
    the teacher's other two templates still identify them."""
    client, owner, admin = stack["client"], stack["owner_a"], stack["admin"]
    chandra = await enroll(client, owner, "Chandra", [10, 12, 14])
    async with admin() as session, session.begin():
        row = await session.scalar(
            select(StaffFaceTemplate)
            .where(StaffFaceTemplate.staff_profile_id == UUID(chandra))
            .order_by(StaffFaceTemplate.id)
            .limit(1)
        )
        assert row is not None
        row.template = b"\x00" * (DIMENSIONS * 4)  # decodes, but is not a unit vector

    status, body = await ask(client, owner, photo(11))
    assert status == 200
    assert body["decision"] == "MATCH"
    assert body["display_name"] == "Chandra"


async def test_the_fake_backend_still_produces_a_usable_template(
    stack: dict[str, Any],
) -> None:
    """V1-02A's fake backend gained a normalised vector in this stage; the enrollment contract
    it satisfies - dimensions, dtype, finite, decodable - must be unchanged."""
    backend = FakeFaceBackend()
    image = Image.open(io.BytesIO(photo(42)))
    template: FaceTemplate = backend.extract_template(image)
    assert template.dimensions == DIMENSIONS
    assert template.dtype == "float32"
    assert len(template.data) == DIMENSIONS * 4
    assert "REDACTED" in repr(template)

    query: FaceQuery = backend.extract_query(image)
    assert len(query.vector.values) == DIMENSIONS
    assert encode(query.vector) == template.data
