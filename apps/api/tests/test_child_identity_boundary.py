"""Children are roster entries, never camera or biometric identities (V1-04D).

Structural and behavioural proofs, not documentation:

* the child tables have no photo, face, embedding, track, camera, date-of-birth, guardian or
  medical column;
* no edge-agent, edge-API, Ring or face-recognition module can name a child profile, and the
  child modules import nothing from them;
* no route serves a child face or photo, no child route lives under the edge or Ring namespaces,
  and attendance requests accept an explicit child UUID - never a track, camera or match;
* a real evaluation-route face MATCH writes no attendance event and changes no child count.

Every child here is synthetic.
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

from veotrex_api.models import ChildAttendanceEvent, ChildProfile

ROOT = Path(__file__).resolve().parents[3]
API_SOURCE = ROOT / "apps" / "api" / "src" / "veotrex_api"
EDGE_SOURCE = ROOT / "services" / "edge-agent" / "src" / "veotrex_edge_agent"
CHILD_MODULES = (
    "child_attendance.py",
    "child_roster_store.py",
    "child_roster_service.py",
    "child_roster_api.py",
)
# Modules that touch cameras, Ring, edge nodes or faces. None may know a child exists.
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
FORBIDDEN_CHILD_COLUMNS = (
    "photo",
    "image",
    "face",
    "embedding",
    "template",
    "biometric",
    "track",
    "camera",
    "birth",
    "dob",
    "age",
    "guardian",
    "parent",
    "pickup",
    "address",
    "medical",
    "allerg",
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
    """Names the code uses - not its docstrings or comments, which may explain the boundary."""
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
@pytest.mark.parametrize("model", [ChildProfile, ChildAttendanceEvent])
def test_child_tables_hold_no_biometric_camera_or_family_data(model: Any) -> None:
    columns = {column.name for column in model.__table__.columns}
    for column in columns:
        for forbidden in FORBIDDEN_CHILD_COLUMNS:
            if forbidden == "age" and column in {"created_at", "updated_at"}:
                continue
            assert forbidden not in column, (model.__tablename__, column)


def test_the_child_profile_is_exactly_the_minimal_roster_entry() -> None:
    assert {column.name for column in ChildProfile.__table__.columns} == {
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


# ============================================================================ edge isolation
def test_the_edge_agent_has_no_child_identity() -> None:
    files = [path for path in EDGE_SOURCE.rglob("*.py") if not path.name.startswith("._")]
    assert files, "edge-agent source not found"
    for path in files:
        source = path.read_text()
        for forbidden in (
            "child_profile",
            "ChildProfile",
            "child_attendance",
            "ChildAttendance",
            "attendance",
            "display_name",
            "/v1/children",
        ):
            assert forbidden not in source, (path.name, forbidden)
        assert not any("child" in name for name in imports_of(path)), path.name


@pytest.mark.parametrize("module", CAMERA_AND_FACE_MODULES)
def test_camera_ring_edge_and_face_modules_cannot_reach_children(module: str) -> None:
    path = API_SOURCE / module
    assert not any("child" in name for name in imports_of(path)), module
    for name in identifiers_of(path):
        assert "child" not in name.lower() and "attendance" not in name.lower(), (module, name)


@pytest.mark.parametrize("module", CHILD_MODULES)
def test_child_modules_touch_no_camera_ring_edge_or_face_code(module: str) -> None:
    path = API_SOURCE / module
    for name in imports_of(path):
        for forbidden in ("face", "ring", "edge", "camera", "recognition", "staff_media"):
            assert forbidden not in name, (module, name)
    for name in identifiers_of(path):
        lowered = name.lower()
        for forbidden in ("face", "embedding", "template", "biometric", "photo", "image", "track"):
            assert forbidden not in lowered, (module, name)


# ================================================================================== routes
@pytest.fixture
def openapi(boundary: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    return boundary["app"].openapi()  # type: ignore[no-any-return]


def test_no_child_route_lives_in_the_edge_or_ring_namespaces(openapi: dict[str, Any]) -> None:
    for path in openapi["paths"]:
        if path.startswith(("/v1/edge", "/v1/integrations")):
            assert "child" not in path and "attendance" not in path, path


def test_no_child_face_or_photo_route_exists(openapi: dict[str, Any]) -> None:
    child_paths = [path for path in openapi["paths"] if "child" in path or "attendance" in path]
    assert child_paths, "the child roster routes are registered"
    for path in child_paths:
        for forbidden in ("face", "photo", "image", "enrollment", "recognition", "template"):
            assert forbidden not in path, path
        for method in openapi["paths"][path].values():
            body = method.get("requestBody", {}).get("content", {})
            assert set(body) <= {"application/json"}, (path, "no image upload")


def test_ring_and_edge_responses_carry_no_child_fields(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]
    for path, methods in openapi["paths"].items():
        if not path.startswith(("/v1/edge", "/v1/integrations")):
            continue
        rendered = str(methods)
        for ref in [part.split("'")[0] for part in rendered.split("#/components/schemas/")[1:]]:
            properties = set(schemas.get(ref, {}).get("properties", {}))
            assert not {name for name in properties if "child" in name}, (path, ref)


def test_attendance_requests_accept_an_explicit_child_uuid_only(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]
    assert set(schemas["AttendanceRequest"]["properties"]) == {"child_profile_id", "lease_seconds"}
    assert set(schemas["AttendanceCheckOutRequest"]["properties"]) == {"child_profile_id"}
    assert schemas["AttendanceRequest"]["properties"]["child_profile_id"]["format"] == "uuid"
    assert set(schemas["ChildCreateRequest"]["properties"]) == {
        "display_name",
        "external_reference",
    }


async def test_an_edge_style_machine_token_cannot_read_the_child_roster(
    boundary: dict[str, Any],  # noqa: F811
) -> None:
    for token in ("vte1_" + uuid4().hex, "edge-node"):
        response = await boundary["client"].get(
            f"/v1/facilities/{boundary['facility']}/children",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert "children" not in response.text


# ==================================================== recognition cannot create attendance
async def test_a_face_match_creates_no_attendance_and_no_child_count(
    boundary: dict[str, Any],  # noqa: F811
) -> None:
    client, owner = boundary["client"], boundary["owner"]
    await enrolled_teacher(boundary, "Teacher Seen", [10, 12, 14])
    room = (
        await client.post(
            "/v1/classrooms",
            json={"facility_id": str(boundary["facility"]), "name": f"Room {uuid4().hex[:6]}"},
            headers=owner,
        )
    ).json()["classroom_id"]
    await client.post(
        f"/v1/classrooms/{room}/presence-source-mode",
        json={"mode": "ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF"},
        headers=owner,
    )
    created = await client.post(
        f"/v1/facilities/{boundary['facility']}/children",
        json={"display_name": "Child Unseen"},
        headers=owner,
    )
    assert created.status_code == 201
    for angle in (11, 190):  # a MATCH and an UNKNOWN
        await recognise(boundary, angle)
    async with boundary["admin"]() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(boundary["tenant"])}
        )
        events = await session.scalar(
            select(func.count())
            .select_from(ChildAttendanceEvent)
            .where(ChildAttendanceEvent.tenant_id == boundary["tenant"])
        )
    assert events == 0
    attendance = (await client.get(f"/v1/classrooms/{room}/attendance", headers=owner)).json()
    assert attendance["summary"]["count"] == 0
    assert [item["state"] for item in attendance["children"]] == ["NOT_CHECKED_IN"]
