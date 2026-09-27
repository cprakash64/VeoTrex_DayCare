"""Guardian pickup authorization is non-biometric and camera-free (V1-04E).

Structural and behavioural proofs, not documentation:

* the guardian, link and release tables have no photo, face, embedding, voice, identity-document,
  government-id, date-of-birth, address, phone, email, camera or track column;
* no edge-agent, edge-API, Ring or face-recognition module can name a guardian, a link or a
  release, and the guardian modules import nothing from them;
* no guardian route lives under the edge or Ring namespaces, none serves or accepts an image, and
  the release request accepts exactly a child UUID, a contact UUID and a bounded method;
* an edge-style machine token cannot read the guardian roster;
* a real evaluation-route face MATCH releases nobody and creates no check-out;
* there is no kinship, resemblance or facial-relationship logic anywhere in the API.

Every adult and child here is synthetic.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from test_staff_presence_recognition_boundary import (  # noqa: F401 - fixture
    boundary,
    enrolled_teacher,
    recognise,
)

from veotrex_api.models import (
    ChildAttendanceEvent,
    ChildGuardianLink,
    ChildReleaseEvent,
    GuardianContact,
)

ROOT = Path(__file__).resolve().parents[3]
API_SOURCE = ROOT / "apps" / "api" / "src" / "veotrex_api"
EDGE_SOURCE = ROOT / "services" / "edge-agent" / "src" / "veotrex_edge_agent"
GUARDIAN_MODULES = (
    "guardian_release.py",
    "guardian_store.py",
    "guardian_service.py",
    "guardian_api.py",
)
CAMERA_AND_FACE_MODULES = (
    "camera_provider.py",
    "edge_api.py",
    "edge_auth.py",
    "edge_credentials.py",
    "edge_whep.py",
    "face_backend.py",
    "face_matching.py",
    "face_models.py",
    "face_opencv.py",
    "ring_client.py",
    "ring_inventory.py",
    "ring_inventory_service.py",
    "ring_nonce.py",
    "ring_pending_expiry.py",
    "ring_readiness.py",
    "ring_repository.py",
    "ring_service.py",
    "ring_webhook.py",
    "staff_media.py",
    "staff_package.py",
    "staff_recognition.py",
)
FORBIDDEN_COLUMNS = (
    "photo",
    "image",
    "face",
    "embedding",
    "template",
    "biometric",
    "voice",
    "track",
    "camera",
    "document",
    "government",
    "passport",
    "license",
    "ssn",
    "birth",
    "dob",
    "address",
    "phone",
    "email",
    "score",
    "similarity",
    "kinship",
)


def imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def identifiers_of(path: Path) -> set[str]:
    """Names the code uses - not its docstrings or comments, which explain the boundary."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
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


# ======================================================================= data minimisation
@pytest.mark.parametrize(
    "model", [GuardianContact, ChildGuardianLink, ChildReleaseEvent], ids=lambda m: m.__name__
)
def test_guardian_tables_hold_no_biometric_identity_document_or_camera_data(model: Any) -> None:
    for column in {column.name for column in model.__table__.columns}:
        for forbidden in FORBIDDEN_COLUMNS:
            assert forbidden not in column, (model.__tablename__, column)


def test_the_contact_is_exactly_the_minimal_roster_entry() -> None:
    assert {column.name for column in GuardianContact.__table__.columns} == {
        "id",
        "tenant_id",
        "facility_id",
        "display_name",
        "status",
        "external_reference",
        "created_by_actor_id",
        "created_at",
        "updated_at",
    }


def test_the_release_row_duplicates_no_name_or_label() -> None:
    columns = {column.name for column in ChildReleaseEvent.__table__.columns}
    assert not {name for name in columns if "name" in name or "label" in name or "note" in name}
    assert {"authorization_link_id", "attendance_event_id", "verification_method"} <= columns


# ============================================================================ edge isolation
def test_the_edge_agent_has_no_guardian_or_release_identity() -> None:
    files = [path for path in EDGE_SOURCE.rglob("*.py") if not path.name.startswith("._")]
    assert files, "edge-agent source not found"
    for path in files:
        source = path.read_text()
        for forbidden in (
            "guardian",
            "Guardian",
            "pickup",
            "child_release",
            "ChildRelease",
            "/v1/children",
            "kinship",
        ):
            assert forbidden not in source, (path.name, forbidden)
        assert not any("guardian" in name for name in imports_of(path)), path.name


@pytest.mark.parametrize("module", CAMERA_AND_FACE_MODULES)
def test_camera_ring_edge_and_face_modules_cannot_reach_guardians(module: str) -> None:
    path = API_SOURCE / module
    assert not any("guardian" in name for name in imports_of(path)), module
    for name in identifiers_of(path):
        lowered = name.lower()
        for forbidden in ("guardian", "pickup", "kinship"):
            assert forbidden not in lowered, (module, name)


@pytest.mark.parametrize("module", GUARDIAN_MODULES)
def test_guardian_modules_touch_no_camera_ring_edge_or_face_code(module: str) -> None:
    path = API_SOURCE / module
    for name in imports_of(path):
        for forbidden in ("face", "ring", "edge", "camera", "recognition", "staff_media"):
            assert forbidden not in name, (module, name)
    # The one allowed match: the operator's statement that they looked at an identity document.
    # It is an enum value, never an image, a number or a document detail.
    for name in identifiers_of(path) - {"PHOTO_ID_CHECKED"}:
        lowered = name.lower()
        for forbidden in (
            "face",
            "embedding",
            "template",
            "biometric",
            "photo",
            "image",
            "track",
            "voice",
            "kinship",
            "similarity",
            "resemblance",
        ):
            assert forbidden not in lowered, (module, name)


def test_no_kinship_or_facial_relationship_logic_exists_anywhere() -> None:
    files = [path for path in API_SOURCE.rglob("*.py") if not path.name.startswith("._")]
    for path in files:
        for name in identifiers_of(path):
            lowered = name.lower()
            for forbidden in ("kinship", "resemblance", "family_match", "parent_match"):
                assert forbidden not in lowered, (path.name, name)


# ================================================================================== routes
@pytest.fixture
def openapi(boundary: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    return boundary["app"].openapi()  # type: ignore[no-any-return]


def guardian_paths(openapi: dict[str, Any]) -> list[str]:
    return [path for path in openapi["paths"] if "guardian" in path or "release" in path]


def test_no_guardian_route_lives_in_the_edge_or_ring_namespaces(openapi: dict[str, Any]) -> None:
    assert guardian_paths(openapi), "the guardian routes are registered"
    for path in openapi["paths"]:
        if path.startswith(("/v1/edge", "/v1/integrations")):
            for forbidden in ("guardian", "release", "pickup", "child"):
                assert forbidden not in path, path


def test_no_guardian_photo_face_or_recognition_route_exists(openapi: dict[str, Any]) -> None:
    for path in guardian_paths(openapi):
        for forbidden in (
            "face",
            "photo",
            "image",
            "enrollment",
            "recognition",
            "template",
            "scan",
        ):
            assert forbidden not in path, path
        for method in openapi["paths"][path].values():
            body = method.get("requestBody", {}).get("content", {})
            assert set(body) <= {"application/json"}, (path, "no image upload")


def test_edge_and_ring_responses_carry_no_guardian_fields(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]
    for path, methods in openapi["paths"].items():
        if not path.startswith(("/v1/edge", "/v1/integrations")):
            continue
        rendered = str(methods)
        for ref in [part.split("'")[0] for part in rendered.split("#/components/schemas/")[1:]]:
            properties = set(schemas.get(ref, {}).get("properties", {}))
            assert not {name for name in properties if "guardian" in name}, (path, ref)


def test_release_and_link_requests_accept_ids_and_bounded_values_only(
    openapi: dict[str, Any],
) -> None:
    schemas = openapi["components"]["schemas"]
    release = schemas["ReleaseRequest"]
    assert set(release["properties"]) == {
        "child_profile_id",
        "guardian_contact_id",
        "verification_method",
    }
    assert set(release["required"]) == set(release["properties"])
    assert release["additionalProperties"] is False
    method = schemas[release["properties"]["verification_method"]["$ref"].split("/")[-1]]
    assert method["enum"] == ["KNOWN_TO_STAFF", "OPERATOR_CONFIRMED", "PHOTO_ID_CHECKED"]
    assert set(schemas["GuardianCreateRequest"]["properties"]) == {
        "display_name",
        "external_reference",
    }
    assert set(schemas["LinkCreateRequest"]["properties"]) == {
        "guardian_contact_id",
        "relationship_label",
        "pickup_authorized",
        "effective_from",
        "effective_until",
        "note",
    }


async def test_an_edge_style_machine_token_cannot_read_guardians(
    boundary: dict[str, Any],  # noqa: F811
) -> None:
    for token in ("vte1_" + uuid4().hex, "edge-node"):
        for path in (
            f"/v1/facilities/{boundary['facility']}/guardians",
            f"/v1/children/{uuid4()}/guardians",
            f"/v1/children/{uuid4()}/release-history",
        ):
            response = await boundary["client"].get(
                path, headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401
            assert "guardian" not in response.text


# ======================================================= recognition cannot release a child
async def test_a_face_match_releases_nobody_and_checks_nobody_out(
    boundary: dict[str, Any],  # noqa: F811
) -> None:
    client, owner, facility = boundary["client"], boundary["owner"], boundary["facility"]
    await enrolled_teacher(boundary, "Teacher Seen", [10, 12, 14])
    room = (
        await client.post(
            "/v1/classrooms",
            json={"facility_id": str(facility), "name": f"Room {uuid4().hex[:6]}"},
            headers=owner,
        )
    ).json()["classroom_id"]
    kid = (
        await client.post(
            f"/v1/facilities/{facility}/children",
            json={"display_name": "Child Unseen"},
            headers=owner,
        )
    ).json()["child_id"]
    contact = (
        await client.post(
            f"/v1/facilities/{facility}/guardians",
            json={"display_name": "Adult Seen"},
            headers=owner,
        )
    ).json()["guardian_contact_id"]
    linked = await client.post(
        f"/v1/children/{kid}/guardians",
        json={
            "guardian_contact_id": contact,
            "relationship_label": "Mother",
            "pickup_authorized": True,
        },
        headers=owner,
    )
    assert linked.status_code == 201
    checked_in = await client.post(
        f"/v1/classrooms/{room}/attendance/check-in",
        json={"child_profile_id": kid},
        headers=owner,
    )
    assert checked_in.status_code == 201
    for angle in (11, 190):  # a MATCH and an UNKNOWN
        await recognise(boundary, angle)
    async with boundary["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(boundary["tenant"])}
        )
        releases = await session.scalar(
            select(func.count())
            .select_from(ChildReleaseEvent)
            .where(ChildReleaseEvent.tenant_id == boundary["tenant"])
        )
        events = (
            await session.scalars(
                select(ChildAttendanceEvent.event_type).where(
                    ChildAttendanceEvent.tenant_id == boundary["tenant"]
                )
            )
        ).all()
    assert releases == 0
    assert list(events) == ["CHECKED_IN"]
    attendance = (await client.get(f"/v1/classrooms/{room}/attendance", headers=owner)).json()
    assert [item["location"] for item in attendance["children"]] == ["HERE"]
