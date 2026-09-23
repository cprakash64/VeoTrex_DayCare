"""The edge recognition package (V1-02A contract): tenant-scoped, ACTIVE+READY only, opaque,
model-matched, deterministic, revisioned, no image bytes, and not reachable over HTTP.

V1-02B0 adds a second case proving the same contract carries the evaluation backend's own
identity and template shape, so V1-02B1's recorded-video recogniser can consume a package built
from real templates without a schema change."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import text

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, Role, RoleGrant
from veotrex_api.config import Settings
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.face_backend import FakeFaceBackend
from veotrex_api.face_matching import decode, similarity
from veotrex_api.face_opencv import (
    DIMENSIONS,
    MODEL_ID,
    MODEL_VERSION,
    TEMPLATE_VERSION,
)
from veotrex_api.staff_media import StaffMediaStore
from veotrex_api.staff_package import build_recognition_package
from veotrex_api.staff_service import StaffEnrollmentService


def jpeg(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), color).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


async def seed(admin_factory) -> AuthenticatedPrincipal:  # type: ignore[no-untyped-def]
    tenant_id, actor_id = uuid4(), uuid4()
    async with admin_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenants (id, name, status) VALUES (:id, 'Package', 'ACTIVE')"),
            {"id": tenant_id},
        )
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
        )
        await session.execute(
            text(
                "INSERT INTO actors (id, tenant_id, display_name, status) "
                "VALUES (:actor, :tenant, 'Owner', 'ACTIVE')"
            ),
            {"actor": actor_id, "tenant": tenant_id},
        )
    return AuthenticatedPrincipal(
        issuer="https://tenant.auth0.example/",
        subject=f"auth0|{actor_id.hex}",
        external_organization_id=f"org_{tenant_id.hex}",
        actor_id=actor_id,
        tenant_id=tenant_id,
        display_name="Owner",
        grants=(RoleGrant(Role.TENANT_OWNER, None),),
        permissions=frozenset(Permission),
    )


async def enroll(
    service: StaffEnrollmentService, principal: AuthenticatedPrincipal, name: str, photos: int
) -> UUID:
    staff = await service.create_profile(principal, name, "r")
    for step in range(photos):
        await service.add_image(principal, staff.staff_id, jpeg((90 + step * 25, 100, 110)), "r")
    return staff.staff_id


async def test_package_contains_only_active_ready_staff_of_one_tenant(
    settings: Settings, admin_settings: Settings, tmp_path: Path
) -> None:
    admin_engine = make_engine(admin_settings)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    backend = FakeFaceBackend()
    service = StaffEnrollmentService(
        factory, StaffMediaStore(tmp_path / "m"), backend, max_image_bytes=4_000_000
    )
    try:
        owner_a = await seed(make_session_factory(admin_engine))
        owner_b = await seed(make_session_factory(admin_engine))
        ready = await enroll(service, owner_a, "Ready Teacher", 3)
        collecting = await enroll(service, owner_a, "Collecting Teacher", 2)
        inactive = await enroll(service, owner_a, "Inactive Teacher", 3)
        await service.set_active(owner_a, inactive, False, "r")
        deleted = await enroll(service, owner_a, "Deleted Teacher", 3)
        await service.delete_profile(owner_a, deleted, "r")
        other = await enroll(service, owner_b, "Other Tenant", 3)

        kwargs = {
            "model_id": backend.model_id,
            "model_version": backend.model_version,
            "template_version": backend.template_version,
        }
        package = await build_recognition_package(factory, owner_a.tenant_id, **kwargs)
        assert package["schema_version"] == 1
        assert package["tenant_id"] == str(owner_a.tenant_id)
        assert [entry["staff_id"] for entry in package["staff"]] == [str(ready)]
        entry = package["staff"][0]
        assert entry["display_name"] == "Ready Teacher"
        assert len(entry["templates"]) == 3
        for template in entry["templates"]:
            assert set(template) == {"template_id", "dimensions", "dtype", "quality", "data_base64"}
            assert template["dimensions"] == 128 and template["dtype"] == "float32"
            assert len(base64.b64decode(template["data_base64"])) == 512
        rendered = repr(package)
        for excluded in (str(collecting), str(inactive), str(deleted), str(other), "Other Tenant"):
            assert excluded not in rendered
        assert "media_key" not in rendered and ".jpg" not in rendered
        assert len(package["revision"]) == 64

        # Deterministic: same input, same ordering and revision.
        again = await build_recognition_package(factory, owner_a.tenant_id, **kwargs)
        assert again["revision"] == package["revision"]
        assert [t["template_id"] for t in again["staff"][0]["templates"]] == [
            t["template_id"] for t in entry["templates"]
        ]
        # Tenant B sees only its own; a model mismatch yields an empty package.
        package_b = await build_recognition_package(factory, owner_b.tenant_id, **kwargs)
        assert [entry["staff_id"] for entry in package_b["staff"]] == [str(other)]
        mismatch = await build_recognition_package(
            factory,
            owner_a.tenant_id,
            model_id="other-model",
            model_version="9",
            template_version=1,
        )
        assert mismatch["staff"] == []
        # Revocation propagates: deactivating the ready teacher empties the package and changes
        # the revision; reactivating restores it with a revision again different from empty.
        await service.set_active(owner_a, ready, False, "r")
        after_off = await build_recognition_package(factory, owner_a.tenant_id, **kwargs)
        assert after_off["staff"] == [] and after_off["revision"] != package["revision"]
        await service.set_active(owner_a, ready, True, "r")
        after_on = await build_recognition_package(factory, owner_a.tenant_id, **kwargs)
        assert [e["staff_id"] for e in after_on["staff"]] == [str(ready)]
        assert after_on["revision"] != after_off["revision"]
        # Removing a photo below the minimum removes the teacher from the package.
        images = await service.list_images(owner_a, ready)
        await service.remove_image(owner_a, ready, images[0].image_id, "r")
        below = await build_recognition_package(factory, owner_a.tenant_id, **kwargs)
        assert below["staff"] == []
    finally:
        await engine.dispose()
        await admin_engine.dispose()


class EvaluationIdentityBackend(FakeFaceBackend):
    """The deterministic fake vectors under the evaluation backend's model identity.

    The package is a pure database projection, so what matters for this contract is the
    identity and shape it carries, not which model produced the numbers - and using the fake's
    vectors keeps this test free of a 37 MiB weight.
    """

    model_id = MODEL_ID
    model_version = MODEL_VERSION
    template_version = TEMPLATE_VERSION


async def test_the_package_can_carry_the_evaluation_backend(
    settings: Settings, admin_settings: Settings, tmp_path: Path
) -> None:
    """V1-02B0 section 16: the edge contract must already be able to represent the evaluation
    backend - its composite model identity, 128-float32 templates, up to five per teacher, and
    a revision that changes only when the included rows do."""
    admin_engine = make_engine(admin_settings)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    backend = EvaluationIdentityBackend()
    service = StaffEnrollmentService(
        factory, StaffMediaStore(tmp_path / "m"), backend, max_image_bytes=4_000_000
    )
    try:
        owner = await seed(make_session_factory(admin_engine))
        chandra = await enroll(service, owner, "Chandra", 5)
        kwargs = {
            "model_id": backend.model_id,
            "model_version": backend.model_version,
            "template_version": backend.template_version,
        }
        package = await build_recognition_package(factory, owner.tenant_id, **kwargs)

        assert package["model_id"] == "yunet+sface"
        assert package["model_version"] == "2023mar+2021dec"
        assert package["template_version"] == TEMPLATE_VERSION
        entry = package["staff"][0]
        assert entry["staff_id"] == str(chandra)
        assert len(entry["templates"]) == 5

        # Every template decodes back into a usable comparison vector on the consumer side:
        # right dtype, right dimensions, finite, still normalised. A package the edge cannot
        # decode would fail on the Jetson rather than here.
        vectors = []
        for template in entry["templates"]:
            assert template["dimensions"] == DIMENSIONS and template["dtype"] == "float32"
            data = base64.b64decode(template["data_base64"])
            vectors.append(decode(data, dimensions=template["dimensions"], dtype=template["dtype"]))
        assert len(vectors) == 5
        # And they are the same templates the API's own recognition would compare against, so
        # the edge and the control plane cannot reach different answers from the same rows.
        assert similarity(vectors[0], vectors[0]) == pytest.approx(1.0, abs=1e-6)

        # Deactivating the teacher empties the package: the edge stops being able to recognise
        # them on its next revision, without anything being deleted.
        await service.set_active(owner, chandra, False, "r")
        after = await build_recognition_package(factory, owner.tenant_id, **kwargs)
        assert after["staff"] == []
        assert after["revision"] != package["revision"]
    finally:
        await engine.dispose()
        await admin_engine.dispose()
