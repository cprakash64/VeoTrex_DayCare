"""Face recognition can never become authoritative staff presence (V1-04C).

Identity, eligibility and presence are separate; recognition is none of them. This suite runs the
real evaluation-only recognition route (scripted, deterministic backend, test environment) next
to the staff roster and proves that a MATCH or an UNKNOWN changes nothing: no presence event is
written, no count moves, and only an explicit operator check-in does either. It also re-asserts
that the gate keeping the real recognition backend out of staging and production still holds,
and that no code path connects the two modules.

Every person is a synthetic adult staff profile photographed as a solid-colour test image.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, text
from test_classroom_api import (
    ISSUER,
    NoRing,
    Secrets,
    StubIdentityVerifier,
    policy_body,
    seed_tenant,
)
from test_face_environment_gate import REFUSED_ENVIRONMENTS
from test_face_environment_gate import settings as gate_settings
from test_staff_recognition_api import RECOGNITION_PATH, ScriptedFaceBackend, photo

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.face_backend import UnavailableFaceBackend
from veotrex_api.identity import ExternalIdentity
from veotrex_api.main import create_app
from veotrex_api.models import StaffPresenceEvent
from veotrex_api.staff_media import StaffMediaStore

SOURCE = Path(__file__).resolve().parents[1] / "src" / "veotrex_api"
RECOGNITION_MODULES = (
    "face_backend.py",
    "face_matching.py",
    "face_models.py",
    "face_opencv.py",
    "staff_recognition.py",
    "staff_package.py",
)
ROSTER_MODULES = (
    "staff_presence.py",
    "staff_roster_store.py",
    "staff_roster_service.py",
    "staff_roster_api.py",
)


@pytest.fixture
async def boundary(settings: Settings, admin_settings: Settings, tmp_path: Path):  # type: ignore[no-untyped-def]
    admin_engine = make_engine(admin_settings)
    admin_factory = make_session_factory(admin_engine)
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    organization, owner = f"org_{uuid4().hex}", f"auth0|{uuid4().hex}"
    tenant, facilities = await seed_tenant(
        admin_factory, "Boundary", organization, {"one": "UTC"}, {owner: ("TENANT_OWNER", None)}
    )
    identity = StubIdentityVerifier(
        {"owner": ExternalIdentity("auth0", ISSUER, owner, organization)}
    )

    def application(backend: Any) -> Any:
        return create_app(
            settings,
            engine,
            identity,
            factory,
            credential_vault=InMemoryCredentialVault(),
            ring_client=NoRing(),  # type: ignore[arg-type]
            secret_resolver=Secrets(),
            face_backend=backend,
            staff_media=StaffMediaStore(tmp_path / "media"),
        )

    app = application(ScriptedFaceBackend())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {
            "client": client,
            "app": app,
            "application": application,
            "admin": admin_factory,
            "tenant": tenant,
            "facility": facilities["one"],
            "owner": {"Authorization": "Bearer owner"},
        }
    await engine.dispose()
    await admin_engine.dispose()


async def enrolled_teacher(boundary: dict[str, Any], name: str, angles: list[int]) -> str:
    client, owner = boundary["client"], boundary["owner"]
    created = await client.post("/v1/staff", json={"display_name": name}, headers=owner)
    staff_id = str(created.json()["staff_id"])
    for angle in angles:
        uploaded = await client.post(
            f"/v1/staff/{staff_id}/enrollment-images",
            content=photo(angle),
            headers={**owner, "content-type": "image/jpeg"},
        )
        assert uploaded.status_code == 201, uploaded.text
    designated = await client.post(
        f"/v1/facilities/{boundary['facility']}/staff-ratio-eligibility",
        json={"staff_profile_id": staff_id, "counts_toward_ratio": True},
        headers=owner,
    )
    assert designated.status_code == 201, designated.text
    return staff_id


async def roster_room(boundary: dict[str, Any], children: int = 6) -> str:
    client, owner = boundary["client"], boundary["owner"]
    room = (
        await client.post(
            "/v1/classrooms",
            json={"facility_id": str(boundary["facility"]), "name": f"Room {uuid4().hex[:6]}"},
            headers=owner,
        )
    ).json()["classroom_id"]
    await client.post(
        f"/v1/classrooms/{room}/ratio-policies",
        json=policy_body(max_children_per_staff=5, minimum_staff=0, maximum_group_size=None),
        headers=owner,
    )
    await client.post(
        f"/v1/classrooms/{room}/presence-source-mode",
        json={"mode": "ROSTER_STAFF_PLUS_MANUAL_CHILDREN"},
        headers=owner,
    )
    await client.post(
        f"/v1/classrooms/{room}/presence/manual", json={"child_count": children}, headers=owner
    )
    return str(room)


async def event_count(boundary: dict[str, Any]) -> int:
    async with boundary["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(boundary["tenant"])}
        )
        # The admin identity bypasses RLS, so the tenant is named explicitly.
        return int(
            await session.scalar(
                select(func.count())
                .select_from(StaffPresenceEvent)
                .where(StaffPresenceEvent.tenant_id == boundary["tenant"])
            )
            or 0
        )


async def staff_count(boundary: dict[str, Any], room: str) -> int:
    response = await boundary["client"].get(
        f"/v1/classrooms/{room}/ratio-status", headers=boundary["owner"]
    )
    return int(response.json()["evaluation"]["staff_count"])


async def recognise(boundary: dict[str, Any], angle: int) -> dict[str, Any]:
    response = await boundary["client"].post(
        RECOGNITION_PATH,
        content=photo(angle),
        headers={**boundary["owner"], "content-type": "image/jpeg"},
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


# ===================================================================== the gate still holds
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_staging_and_production_still_refuse_the_evaluation_backend(environment: str) -> None:
    assert environment in REFUSED_ENVIRONMENTS
    with pytest.raises(ValidationError, match="permitted only in"):
        gate_settings(
            environment=environment, staff_face_backend="opencv_eval", staff_face_model_dir="/m"
        )
    assert gate_settings(environment=environment).staff_face_backend == "unavailable"


async def test_the_recognition_route_is_evaluation_only_and_the_roster_does_not_need_it(
    boundary: dict[str, Any],
) -> None:
    body = await recognise(boundary, 10)
    assert body["evaluation_only"] is True
    # With the fail-closed backend there is no recognition route at all, and the roster still
    # works: presence never depended on recognition.
    unavailable = boundary["application"](UnavailableFaceBackend())
    paths = {route.path for route in unavailable.routes}
    assert RECOGNITION_PATH not in paths
    assert "/v1/classrooms/{classroom_id}/staff-presence/check-in" in paths
    staff = await enrolled_teacher(boundary, "Teacher Without Faces", [])
    room = await roster_room(boundary)
    async with AsyncClient(transport=ASGITransport(app=unavailable), base_url="http://t") as client:
        checked_in = await client.post(
            f"/v1/classrooms/{room}/staff-presence/check-in",
            json={"staff_profile_id": staff},
            headers=boundary["owner"],
        )
    assert checked_in.status_code == 201
    assert await staff_count(boundary, room) == 1


# ================================================================= recognition changes nothing
async def test_a_recognition_match_neither_checks_in_nor_counts(boundary: dict[str, Any]) -> None:
    teacher = await enrolled_teacher(boundary, "Teacher Matched", [10, 12, 14])
    room = await roster_room(boundary)
    body = await recognise(boundary, 11)
    assert (body["decision"], body["staff_id"]) == ("MATCH", teacher)
    assert await event_count(boundary) == 0, "a MATCH writes no presence event"
    assert await staff_count(boundary, room) == 0, "a MATCH changes no count"
    view = (
        await boundary["client"].get(
            f"/v1/classrooms/{room}/staff-presence", headers=boundary["owner"]
        )
    ).json()
    assert [(item["staff_profile_id"], item["state"]) for item in view["staff"]] == [
        (teacher, "NOT_CHECKED_IN")
    ]
    # Only the explicit operator event makes the person count.
    await boundary["client"].post(
        f"/v1/classrooms/{room}/staff-presence/check-in",
        json={"staff_profile_id": teacher},
        headers=boundary["owner"],
    )
    assert await staff_count(boundary, room) == 1
    # And a later match does not alter or extend that event.
    before = await event_count(boundary)
    await recognise(boundary, 12)
    assert await event_count(boundary) == before
    assert await staff_count(boundary, room) == 1


async def test_a_recognition_unknown_changes_nothing(boundary: dict[str, Any]) -> None:
    await enrolled_teacher(boundary, "Teacher Known", [10, 12, 14])
    room = await roster_room(boundary)
    body = await recognise(boundary, 190)
    assert body["decision"] == "UNKNOWN" and body["staff_id"] is None
    assert await event_count(boundary) == 0
    assert await staff_count(boundary, room) == 0


# ============================================================== no code path connects the two
def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def identifiers(path: Path) -> set[str]:
    """Every name the code uses - not its docstrings or comments, which may explain the
    boundary in words."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


@pytest.mark.parametrize("module", RECOGNITION_MODULES)
def test_recognition_code_cannot_reach_the_roster(module: str) -> None:
    imports = imported_modules(SOURCE / module)
    for forbidden in ("staff_presence", "staff_roster", "classroom_service", "classroom_ratio"):
        assert not any(forbidden in name for name in imports), (module, forbidden)
    names = identifiers(SOURCE / module)
    assert not {"StaffPresenceEvent", "StaffRatioEligibility", "ClassroomPresenceSnapshot"} & names
    assert "staff_presence_events" not in (SOURCE / module).read_text()


@pytest.mark.parametrize("module", ROSTER_MODULES)
def test_roster_code_reads_no_face_material(module: str) -> None:
    imports = imported_modules(SOURCE / module)
    for forbidden in ("face_", "staff_recognition", "staff_package", "staff_media"):
        assert not any(forbidden in name for name in imports), (module, forbidden)
    for name in identifiers(SOURCE / module):
        lowered = name.lower()
        for forbidden in ("face", "template", "embedding", "enrollment", "image", "track"):
            assert forbidden not in lowered, (module, name)


def test_the_resolver_accepts_no_recognition_input() -> None:
    """There is no argument through which a match could enter the staff count."""
    from inspect import signature

    from veotrex_api.staff_presence import resolve_staff_count

    assert set(signature(resolve_staff_count).parameters) == {
        "classroom_id",
        "facility_id",
        "now",
        "members",
        "events",
        "assignments",
    }


def test_unknown_staff_ids_never_count() -> None:
    """A recognition result naming a profile that is not on the roster changes nothing."""
    from datetime import UTC, datetime

    from veotrex_api.staff_presence import resolve_staff_count

    result = resolve_staff_count(
        classroom_id=uuid4(),
        facility_id=uuid4(),
        now=datetime.now(UTC),
        members=[],
        events=[],
        assignments=[],
    )
    assert result.count == 0 and result.present == 0
    assert isinstance(result.classroom_id, UUID)
