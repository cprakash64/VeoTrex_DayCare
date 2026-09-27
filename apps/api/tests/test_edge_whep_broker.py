"""V1-DEMO-03B: brokered Ring WHEP for authenticated edge nodes, end to end through the API.

Everything below runs the real app, the real ``RingLinkService`` (so refresh and rotation are the
production lifecycle), the in-memory vault and a scripted fake Ring behind ``httpx.MockTransport``.
There is no network, no real credential and no media. Each test asserts both what the edge
receives and what Ring receives, because the security property is about both directions:
the edge gets an SDP answer and an opaque lease and nothing else; Ring gets the Ring token and
never the edge credential.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs
from tests_edge_fixtures import (
    ANSWER,
    OFFER,
    RING_ACCESS,
    RING_ACCESS_REFRESHED,
    RING_REFRESH,
    RING_REFRESH_ROTATED,
    EdgeWorld,
    FakeRing,
    Scripted,
    Secrets,
    add_camera,
    add_credential,
    add_node,
    seed_world,
    set_tenant,
)

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.edge_whep import EdgeWhepBroker, WhepLeaseRegistry
from veotrex_api.main import create_app
from veotrex_api.models import (
    AuditEvent,
    Camera,
    CameraAssignment,
    CameraProviderComponent,
    CameraProviderConnection,
    CameraProviderDevice,
)
from veotrex_api.ring_client import RingClient

LEASE_LOCATION = re.compile(r"^/v1/edge/whep-leases/[A-Za-z0-9_-]{43}$")
SDP = {"content-type": "application/sdp"}


@dataclass
class Harness:
    client: AsyncClient
    ring: FakeRing
    admin_factory: async_sessionmaker[AsyncSession]
    factory: async_sessionmaker[AsyncSession]
    vault: InMemoryCredentialVault
    broker: EdgeWhepBroker
    world: EdgeWorld

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"authorization": f"Bearer {token or self.world.token.get_secret_value()}"}

    async def offer(
        self, camera: Any = None, token: str | None = None, **kwargs: Any
    ) -> httpx.Response:
        camera_id = camera or self.world.camera_id
        headers = {**SDP, **self.auth(token), **kwargs.pop("headers", {})}
        return await self.client.post(
            f"/v1/edge/cameras/{camera_id}/whep",
            content=kwargs.pop("content", OFFER),
            headers=headers,
        )


@asynccontextmanager
async def harness(
    settings: Settings, admin_settings: Settings, **overrides: Any
) -> AsyncIterator[Harness]:
    configured = settings.model_copy(update=overrides) if overrides else settings
    admin_engine: AsyncEngine = make_engine(admin_settings)
    engine = make_engine(configured)
    factory = make_session_factory(engine)
    admin_factory = make_session_factory(admin_engine)
    vault = InMemoryCredentialVault()
    ring = FakeRing()
    http = httpx.AsyncClient(transport=httpx.MockTransport(ring.handler))
    world = await seed_world(admin_factory, vault)
    app = create_app(
        configured,
        engine,
        session_factory=factory,
        credential_vault=vault,
        ring_client=RingClient(configured, Secrets(), http),
        secret_resolver=Secrets(),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield Harness(
                client,
                ring,
                admin_factory,
                factory,
                vault,
                app.state.edge_whep_broker,
                world,
            )
    finally:
        await http.aclose()
        await engine.dispose()
        await admin_engine.dispose()


def _everything_the_edge_saw(response: httpx.Response) -> str:
    return response.text + " " + " ".join(f"{k}: {v}" for k, v in response.headers.items())


# ---------------------------------------------------------------------------------- happy path
async def test_offer_is_brokered_and_only_the_answer_and_an_opaque_lease_come_back(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        response = await h.offer()
        assert response.status_code == 201
        assert response.headers["content-type"].startswith("application/sdp")
        assert response.content == ANSWER
        assert LEASE_LOCATION.fullmatch(response.headers["location"])
        assert response.headers["cache-control"] == "no-store"
        seen = _everything_the_edge_saw(response)
        for secret in (RING_ACCESS, RING_REFRESH, "amazonvision", "synthetic-ring-session"):
            assert secret not in seen, secret
        assert h.world.device_id not in seen and str(h.world.connection_id) not in seen

        [post] = h.ring.whep_posts()
        assert post.url.host == "api.amazonvision.com" and post.url.scheme == "https"
        assert post.url.path == (f"/v1/devices/{h.world.device_id}/media/streaming/whep/sessions")
        assert post.url.query == b""
        assert post.headers["authorization"] == f"Bearer {RING_ACCESS}"
        assert post.headers["content-type"] == "application/sdp"
        assert post.headers["accept"] == "application/sdp"
        assert post.content == OFFER
        edge_token = h.world.token.get_secret_value()
        for request in h.ring.requests:
            assert edge_token not in str(request.headers) and edge_token.encode() not in (
                request.content or b""
            )
        assert h.broker.registry.active_count == 1

        async with h.admin_factory() as session, session.begin():
            await set_tenant(session, h.world.tenant_id)
            audits = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == h.world.tenant_id,
                        AuditEvent.action == "edge.whep.session_opened",
                    )
                )
            ).all()
        assert len(audits) == 1 and audits[0].target_id == h.world.camera_id
        assert audits[0].metadata_ == {"edge_node_id": str(h.world.node_id), "provider": "RING"}


async def test_component_identity_is_resolved_server_side(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        camera_id, *_ = await add_camera(
            h.admin_factory,
            h.world.tenant_id,
            h.world.connection_id,
            device_id=f"synthetic-multi-{uuid4().hex[:8]}",
            component_id="2",
            assign_to=h.world.node_id,
        )
        response = await h.offer(camera_id)
        assert response.status_code == 201
        [post] = h.ring.whep_posts()
        assert post.url.query == b"component_id=2"


# ---------------------------------------------------------------------------------- DELETE
async def test_delete_releases_the_provider_session_and_is_idempotent(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        created = await h.offer()
        location = created.headers["location"]
        ring_session = h.ring.whep_posts()[0].url.path + "/synthetic-ring-session-1"

        first = await h.client.delete(location, headers=h.auth())
        assert first.status_code == 204 and first.content == b""
        [delete] = h.ring.whep_deletes()
        assert delete.url.path == ring_session
        assert delete.headers["authorization"] == f"Bearer {RING_ACCESS}"
        assert h.broker.registry.active_count == 0

        again = await h.client.delete(location, headers=h.auth())
        assert again.status_code == 204
        assert len(h.ring.whep_deletes()) == 1, "an idempotent DELETE must not re-contact Ring"


async def test_a_lease_of_another_node_is_indistinguishable_from_no_lease(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        location = (await h.offer()).headers["location"]
        other_node = await add_node(h.admin_factory, h.world.tenant_id, h.world.facility_id)
        _, other_token = await add_credential(h.admin_factory, h.world.tenant_id, other_node)
        other_tenant = await seed_world(h.admin_factory, h.vault)

        denied = await h.client.delete(location, headers=h.auth(other_token.get_secret_value()))
        foreign = await h.client.delete(
            location, headers=h.auth(other_tenant.token.get_secret_value())
        )
        unknown = await h.client.delete(
            "/v1/edge/whep-leases/" + "A" * 43, headers=h.auth(other_token.get_secret_value())
        )
        garbage = await h.client.delete("/v1/edge/whep-leases/x", headers=h.auth())
        assert denied.status_code == foreign.status_code == unknown.status_code == 404
        assert garbage.status_code == 404
        assert denied.json() == foreign.json() == unknown.json() == garbage.json()
        assert h.ring.whep_deletes() == []
        assert h.broker.registry.active_count == 1, "the owner's lease is untouched"
        unauthenticated = await h.client.delete(location)
        assert unauthenticated.status_code == 401


# ---------------------------------------------------------------------------- lease bounds
async def test_lease_registry_is_bounded_per_node_and_globally(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(
        settings,
        admin_settings,
        edge_whep_max_active_leases=2,
        edge_whep_max_leases_per_node=1,
    ) as h:
        assert (await h.offer()).status_code == 201
        refused = await h.offer()
        assert refused.status_code == 503
        assert len(h.ring.whep_posts()) == 1, "capacity is checked before Ring is contacted"

        second_node = await add_node(h.admin_factory, h.world.tenant_id, h.world.facility_id)
        _, second_token = await add_credential(h.admin_factory, h.world.tenant_id, second_node)
        second_camera, *_ = await add_camera(
            h.admin_factory,
            h.world.tenant_id,
            h.world.connection_id,
            device_id=f"synthetic-second-{uuid4().hex[:8]}",
            component_id=None,
            assign_to=second_node,
        )
        assert (await h.offer(second_camera, second_token.get_secret_value())).status_code == 201
        third_node = await add_node(h.admin_factory, h.world.tenant_id, h.world.facility_id)
        _, third_token = await add_credential(h.admin_factory, h.world.tenant_id, third_node)
        third_camera, *_ = await add_camera(
            h.admin_factory,
            h.world.tenant_id,
            h.world.connection_id,
            device_id=f"synthetic-third-{uuid4().hex[:8]}",
            component_id=None,
            assign_to=third_node,
        )
        full = await h.offer(third_camera, third_token.get_secret_value())
        assert full.status_code == 503
        assert h.broker.registry.active_count == 2 and h.broker.registry.pending_count == 0


async def test_an_expired_lease_is_released_and_then_fails_safely(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        now = [1_000.0]
        h.broker.registry = WhepLeaseRegistry(
            max_active=4, max_per_node=2, ttl_seconds=60, clock=lambda: now[0]
        )
        location = (await h.offer()).headers["location"]
        now[0] += 61
        assert await h.broker.release_expired() == 1
        assert len(h.ring.whep_deletes()) == 1
        assert h.broker.registry.active_count == 0
        # Within one TTL the owner's DELETE is answered idempotently, without Ring.
        assert (await h.client.delete(location, headers=h.auth())).status_code == 204
        now[0] += 61
        await h.broker.release_expired()
        assert (await h.client.delete(location, headers=h.auth())).status_code == 404
        assert len(h.ring.whep_deletes()) == 1


async def test_shutdown_attempts_to_close_every_outstanding_session(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        await h.offer()
        second_camera, *_ = await add_camera(
            h.admin_factory,
            h.world.tenant_id,
            h.world.connection_id,
            device_id=f"synthetic-shutdown-{uuid4().hex[:8]}",
            component_id=None,
            assign_to=h.world.node_id,
        )
        await h.offer(second_camera)
        h.ring.delete_scripts = [Scripted(500, b""), Scripted(204, b"")]
        assert await h.broker.shutdown(timeout_seconds=5) == 2
        assert len(h.ring.whep_deletes()) == 2, "a failed teardown does not stop the others"
        assert h.broker.registry.active_count == 0
        assert await h.broker.shutdown() == 0


# ------------------------------------------------------------------ camera authorization
async def _mutate(h: Harness, statement: Any) -> None:
    async with h.admin_factory() as session, session.begin():
        await set_tenant(session, h.world.tenant_id)
        await session.execute(statement)


async def _assert_camera_refused(h: Harness, camera: Any = None) -> httpx.Response:
    response = await h.offer(camera)
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
    assert h.ring.requests == [], "an unauthorized camera must never reach Ring"
    assert h.broker.registry.active_count == 0 and h.broker.registry.pending_count == 0
    return response


async def test_unassigned_and_nonexistent_and_cross_tenant_cameras_look_identical(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        unassigned, *_ = await add_camera(
            h.admin_factory,
            h.world.tenant_id,
            h.world.connection_id,
            device_id=f"synthetic-unassigned-{uuid4().hex[:8]}",
            component_id=None,
        )
        other = await seed_world(h.admin_factory, h.vault)
        bodies = [
            (await _assert_camera_refused(h, unassigned)).json(),
            (await _assert_camera_refused(h, uuid4())).json(),
            (await _assert_camera_refused(h, other.camera_id)).json(),
            # Non-canonical spellings of the node's own camera are refused, not normalized.
            (await _assert_camera_refused(h, h.world.camera_id.hex)).json(),
            (await _assert_camera_refused(h, f"{{{h.world.camera_id}}}")).json(),
            (await _assert_camera_refused(h, "not-a-uuid")).json(),
        ]
        assert all(body == bodies[0] for body in bodies)


async def test_assignment_to_a_different_node_is_denied(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        other_node = await add_node(h.admin_factory, h.world.tenant_id, h.world.facility_id)
        await _mutate(
            h,
            update(CameraAssignment)
            .where(CameraAssignment.id == h.world.assignment_id)
            .values(edge_node_id=other_node),
        )
        await _assert_camera_refused(h)


async def test_an_ended_assignment_is_denied(settings: Settings, admin_settings: Settings) -> None:
    async with harness(settings, admin_settings) as h:
        await _mutate(
            h,
            update(CameraAssignment)
            .where(CameraAssignment.id == h.world.assignment_id)
            .values(ended_at=datetime.now(UTC)),
        )
        await _assert_camera_refused(h)


@pytest.mark.parametrize("camera_status", ["DISABLED", "ARCHIVED"])
async def test_a_disabled_or_archived_camera_is_denied(
    settings: Settings, admin_settings: Settings, camera_status: str
) -> None:
    async with harness(settings, admin_settings) as h:
        await _mutate(
            h, update(Camera).where(Camera.id == h.world.camera_id).values(status=camera_status)
        )
        await _assert_camera_refused(h)


async def test_a_removed_provider_component_or_device_is_denied(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        await _mutate(
            h,
            update(CameraProviderComponent)
            .where(CameraProviderComponent.id == h.world.component_record_id)
            .values(inventory_state="REMOVED", removed_at=datetime.now(UTC)),
        )
        await _assert_camera_refused(h)
        await _mutate(
            h,
            update(CameraProviderComponent)
            .where(CameraProviderComponent.id == h.world.component_record_id)
            .values(inventory_state="ACTIVE", removed_at=None),
        )
        await _mutate(
            h,
            update(CameraProviderDevice)
            .where(CameraProviderDevice.id == h.world.device_record_id)
            .values(inventory_state="REMOVED", removed_at=datetime.now(UTC)),
        )
        await _assert_camera_refused(h)


@pytest.mark.parametrize(
    "values",
    [
        {"integration_state": "REAUTH_REQUIRED"},
        {"integration_state": "DISCONNECTED", "status": "DISABLED"},
        {"status": "DISABLED"},
        {"operational_health": "REMOTE_REMOVED"},
        {"provider_type": "OTHER"},
    ],
)
async def test_an_inactive_ring_connection_is_denied(
    settings: Settings, admin_settings: Settings, values: dict[str, str]
) -> None:
    async with harness(settings, admin_settings) as h:
        await _mutate(
            h,
            update(CameraProviderConnection)
            .where(CameraProviderConnection.id == h.world.connection_id)
            .values(**values),
        )
        await _assert_camera_refused(h)


async def test_a_camera_without_live_video_is_denied(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        await _mutate(
            h,
            update(CameraProviderComponent)
            .where(CameraProviderComponent.id == h.world.component_record_id)
            .values(capabilities=["SNAPSHOT", "MOTION_EVENTS"]),
        )
        await _assert_camera_refused(h)


# --------------------------------------------------------------------- request validation
async def test_offers_are_bounded_and_validated_before_anything_is_forwarded(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings, edge_whep_max_offer_bytes=2048) as h:
        too_big = await h.offer(content=OFFER + b"a=x\r\n" * 1000)
        assert too_big.status_code == 413
        wrong_type = await h.offer(headers={"content-type": "application/json"})
        assert wrong_type.status_code == 415
        for bad in (b"", b"not sdp", b"v=0\r\nm=audio 9 RTP 0\r\n", b"v=0\r\nX=bad\r\n", b"\xff"):
            response = await h.offer(content=bad)
            assert response.status_code == 400, bad
        assert h.ring.requests == []
        # The media type is checked even without credentials; authentication still gates the
        # route itself, so an unauthenticated well-formed offer is a 401.
        anonymous = await h.client.post(
            f"/v1/edge/cameras/{h.world.camera_id}/whep", content=OFFER, headers=SDP
        )
        assert anonymous.status_code == 401


async def test_session_creation_is_rate_limited(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings, edge_whep_rate_limit_per_minute=1) as h:
        assert (await h.offer()).status_code == 201
        assert (await h.offer()).status_code == 429
        assert len(h.ring.whep_posts()) == 1


# ------------------------------------------------------------------- Ring failure semantics
async def test_a_definite_401_refreshes_through_the_lifecycle_and_retries_once(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        h.ring.whep_scripts = [Scripted(401, b"", None, None), Scripted()]
        response = await h.offer()
        assert response.status_code == 201
        first, second = h.ring.whep_posts()
        assert first.headers["authorization"] == f"Bearer {RING_ACCESS}"
        assert second.headers["authorization"] == f"Bearer {RING_ACCESS_REFRESHED}"
        assert h.ring.refresh_calls == 1
        async with h.factory() as session, session.begin():
            await set_tenant(session, h.world.tenant_id)
            connection = await session.get(CameraProviderConnection, h.world.connection_id)
            assert connection is not None and connection.credential_generation == 2
            secret_ref = connection.secret_ref
            owner = connection.credential_owner_id
        from veotrex_api.credential_vault import CredentialContext

        assert secret_ref is not None and owner is not None
        rotated = await h.vault.get(
            secret_ref, CredentialContext("RING", "ring_pending_link", owner, h.world.tenant_id)
        )
        assert rotated.material.refresh_token.get_secret_value() == RING_REFRESH_ROTATED
        assert RING_ACCESS_REFRESHED not in _everything_the_edge_saw(response)


async def test_a_second_401_is_final(settings: Settings, admin_settings: Settings) -> None:
    async with harness(settings, admin_settings) as h:
        h.ring.whep_scripts = [Scripted(401, b"", None, None), Scripted(401, b"", None, None)]
        response = await h.offer()
        assert response.status_code == 403
        assert len(h.ring.whep_posts()) == 2 and h.ring.refresh_calls == 1
        assert h.broker.registry.active_count == 0 and h.broker.registry.pending_count == 0


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("synthetic connect failure"),
        httpx.ReadTimeout("synthetic read timeout"),
        httpx.RemoteProtocolError("synthetic protocol failure"),
    ],
)
async def test_an_ambiguous_transport_failure_is_never_retried(
    settings: Settings, admin_settings: Settings, failure: Exception
) -> None:
    async with harness(settings, admin_settings) as h:
        h.ring.whep_scripts = [Scripted(raise_error=failure)]
        response = await h.offer()
        assert response.status_code == 502
        assert len(h.ring.whep_posts()) == 1
        assert h.ring.refresh_calls == 0
        assert h.broker.registry.active_count == 0


@pytest.mark.parametrize(
    ("ring_status", "edge_status"),
    [(500, 502), (502, 502), (503, 502), (429, 429), (404, 503), (403, 403), (400, 502)],
)
async def test_provider_errors_map_to_bounded_categories_without_retry(
    settings: Settings, admin_settings: Settings, ring_status: int, edge_status: int
) -> None:
    async with harness(settings, admin_settings) as h:
        body = b'{"errors":[{"title":"synthetic provider detail"}]}'
        h.ring.whep_scripts = [Scripted(ring_status, body, "application/json", None)]
        with capture_logs() as logs:
            response = await h.offer()
        assert response.status_code == edge_status
        assert "synthetic provider detail" not in response.text
        assert len(h.ring.whep_posts()) == 1
        failures = [entry for entry in logs if entry["event"] == "ring_provider_request_failed"]
        assert failures and failures[0]["status_code"] == ring_status


@pytest.mark.parametrize(
    "script",
    [
        Scripted(201, ANSWER + b"a=" + b"x" * 70_000 + b"\r\n"),
        Scripted(201, b"<html>not sdp</html>", "text/html"),
        Scripted(201, b"v=0\r\nm=audio 9 RTP/AVP 0\r\n"),
        Scripted(201, b"v=0\r\nm=video 9 RTP 96\r\nm=audio 9 RTP/AVP 0\r\n"),
        Scripted(201, b""),
    ],
)
async def test_oversized_or_malformed_answers_are_rejected_and_the_session_released(
    settings: Settings, admin_settings: Settings, script: Scripted
) -> None:
    async with harness(settings, admin_settings) as h:
        h.ring.whep_scripts = [script]
        response = await h.offer()
        assert response.status_code == 502
        assert len(h.ring.whep_deletes()) == 1, "a 2xx may have created a session: release it"
        assert h.broker.registry.active_count == 0


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/v1/devices/d/media/streaming/whep/sessions/s",
        "http://api.amazonvision.com/v1/devices/d/media/streaming/whep/sessions/s",
        "https://user@api.amazonvision.com/v1/devices/d/media/streaming/whep/sessions/s",
        "/v1/somewhere/else",
        "/v1/devices/other-device/media/streaming/whep/sessions/s",
    ],
)
async def test_an_untrusted_provider_location_is_never_contacted(
    settings: Settings, admin_settings: Settings, location: str
) -> None:
    async with harness(settings, admin_settings) as h:
        h.ring.whep_scripts = [Scripted(location=location)]
        response = await h.offer()
        assert response.status_code == 502
        assert h.ring.whep_deletes() == []
        assert all(request.url.host == "api.amazonvision.com" for request in h.ring.requests)
        assert "evil.example" not in _everything_the_edge_saw(response)


async def test_a_provider_redirect_is_refused_not_followed(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        h.ring.whep_scripts = [Scripted(307, b"", None, "https://evil.example/whep")]
        response = await h.offer()
        assert response.status_code == 502
        assert [request.url.host for request in h.ring.requests] == ["api.amazonvision.com"]


async def test_an_unusable_ring_credential_fails_closed_before_ring_is_contacted(
    settings: Settings, admin_settings: Settings
) -> None:
    async with harness(settings, admin_settings) as h:
        await _mutate(
            h,
            update(CameraProviderConnection)
            .where(CameraProviderConnection.id == h.world.connection_id)
            .values(secret_ref=None),
        )
        response = await h.offer()
        assert response.status_code == 503
        assert h.ring.requests == []


async def test_no_secret_reaches_any_log_line(settings: Settings, admin_settings: Settings) -> None:
    async with harness(settings, admin_settings) as h:
        h.ring.whep_scripts = [Scripted(401, b"", None, None), Scripted()]
        with capture_logs() as logs:
            location = (await h.offer()).headers["location"]
            await h.client.delete(location, headers=h.auth())
            await h.offer(token="vte1." + str(uuid4()) + "." + "A" * 43)
            h.ring.whep_scripts = [Scripted(500, b"")]
            await h.offer()
        rendered = repr(logs)
        forbidden = (
            RING_ACCESS,
            RING_ACCESS_REFRESHED,
            RING_REFRESH,
            RING_REFRESH_ROTATED,
            h.world.token.get_secret_value(),
            h.world.token.get_secret_value().rsplit(".", 1)[1],
            location.rsplit("/", 1)[1],
            "synthetic-ring-session",
            "a=rtpmap",
            h.world.device_id,
        )
        for value in forbidden:
            assert value not in rendered, value
        events = {entry["event"] for entry in logs}
        assert {"edge_whep_session_opened", "edge_whep_session_released"} <= events
        assert "edge_authentication_failed" in events
