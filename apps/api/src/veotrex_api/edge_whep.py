"""Brokered Ring WHEP live video for authenticated edge nodes (V1-DEMO-03B).

The edge node never holds a Ring credential. It sends its SDP offer here, with its own machine
credential, naming a VeoTrex camera UUID. The broker:

1. authorizes the node for that camera from server-side state only - an ACTIVE assignment of
   exactly this camera to exactly this node, a usable camera, an ACTIVE Ring component, device
   and connection, and a LIVE_VIDEO capability - under the node's own tenant RLS context;
2. obtains the Ring access token through ``RingLinkService.get_valid_access_token``, the single
   lifecycle authority for refresh and rotation;
3. negotiates WHEP with Ring server-side, and returns only the SDP answer plus an opaque VeoTrex
   lease. Ring's session resource URL stays in this process.

Retry rule: a WHEP POST that failed ambiguously (transport error, 5xx) may have created a Ring
session, so it is never replayed. Only a definite 401 - behind which no session can exist -
earns one lifecycle-controlled forced refresh and exactly one retry.

**The lease registry is process-local.** It is bounded in count (globally and per node) and in
age, safe for concurrent use, and released on DELETE, on expiry and at shutdown. It is correct
only while ONE API process serves every request, which is why the API image runs a single
uvicorn worker (ADR 0021). Multiple workers or replicas would route a DELETE to a process that
never saw the lease; that deployment needs a shared lease store first.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

import structlog
from pydantic import SecretStr
from sqlalchemy import and_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.camera_provider import CameraCapability
from veotrex_api.config import Settings
from veotrex_api.edge_auth import EdgePrincipal
from veotrex_api.models import (
    AuditEvent,
    Camera,
    CameraAssignment,
    CameraProviderComponent,
    CameraProviderConnection,
    CameraProviderDevice,
    EdgeNode,
)
from veotrex_api.ring_client import (
    RingAmbiguousResult,
    RingClient,
    RingClientError,
    RingWhepSession,
)
from veotrex_api.ring_service import RingLinkError, RingLinkService

LEASE_PATH_PREFIX = "/v1/edge/whep-leases/"
# 32 random bytes, base64url without padding: unguessable and path-safe.
_LEASE_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_OFFER_LINE = re.compile(r"^[a-z]=[^\r\n\x00]*$")
MAX_OFFER_LINES = 4096
USABLE_CAMERA_STATES = ("DISCOVERED", "ACTIVE")
UNUSABLE_CONNECTION_HEALTH = ("REMOTE_REMOVED", "REAUTH_REQUIRED")


class EdgeBrokerError(Exception):
    """A bounded, secret-free broker failure with the HTTP status the edge receives."""

    def __init__(self, category: str, status_code: int) -> None:
        super().__init__(f"edge broker failed: {category}")
        self.category = category
        self.status_code = status_code


def _camera_unavailable() -> EdgeBrokerError:
    # One answer for "does not exist", "another tenant's", "not assigned to you", "assignment
    # ended", "disabled", "removed upstream", "no live video" and "connection not active".
    return EdgeBrokerError("camera_unavailable", 404)


def validate_offer(body: bytes, max_bytes: int) -> bytes:
    """Refuse anything that is not a bounded, UTF-8, video-bearing SDP offer before it is
    forwarded anywhere. The offer is never logged."""
    if not body or len(body) > max_bytes:
        raise EdgeBrokerError("invalid_offer", 400)
    try:
        decoded = body.decode("utf-8", errors="strict")
    except UnicodeError:
        raise EdgeBrokerError("invalid_offer", 400) from None
    lines = [line for line in decoded.replace("\r\n", "\n").split("\n") if line]
    if (
        not lines
        or len(lines) > MAX_OFFER_LINES
        or not lines[0].startswith("v=0")
        or any(not _OFFER_LINE.fullmatch(line) for line in lines)
        or not any(line.startswith("m=video ") for line in lines)
    ):
        raise EdgeBrokerError("invalid_offer", 400)
    return body


@dataclass(frozen=True, slots=True, repr=False)
class BrokerTarget:
    """The server-resolved Ring identity of one authorized camera. Never leaves the API."""

    tenant_id: UUID
    camera_id: UUID
    connection_id: UUID
    provider_device_id: str
    provider_component_id: str | None

    def __repr__(self) -> str:
        return f"BrokerTarget(camera_id={self.camera_id}, provider_identity=REDACTED)"


@dataclass(slots=True, repr=False)
class WhepLease:
    lease_id: str
    tenant_id: UUID
    edge_node_id: UUID
    camera_id: UUID
    connection_id: UUID
    provider_session_url: str | None
    created_monotonic: float
    expires_monotonic: float

    @property
    def location(self) -> str:
        return f"{LEASE_PATH_PREFIX}{self.lease_id}"

    def __repr__(self) -> str:
        # Neither the lease id (a capability) nor the Ring resource URL.
        return (
            f"WhepLease(camera_id={self.camera_id}, edge_node_id={self.edge_node_id}, "
            f"lease=REDACTED, provider_session=REDACTED)"
        )

    __str__ = __repr__


@dataclass(slots=True)
class LeaseReservation:
    """A capacity slot held while the Ring session is being negotiated."""

    tenant_id: UUID
    edge_node_id: UUID
    settled: bool = False


class ReleaseOutcome(StrEnum):
    RELEASED = "RELEASED"
    ALREADY_RELEASED = "ALREADY_RELEASED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(slots=True)
class _NodeCount:
    count: int = 0


class WhepLeaseRegistry:
    """Process-local, bounded registry of brokered WHEP sessions.

    NOT suitable for more than one API process: see the module docstring. Every method takes
    one short lock and does no I/O, so it is safe from the event loop and from threads alike.
    Bounds: ``max_active`` leases plus in-flight reservations overall, ``max_per_node`` per
    edge node, and ``ttl_seconds`` of age after which a lease is expired and released. Released
    lease ids are remembered (bounded, for one TTL) only so a repeated DELETE by the owner is
    answered idempotently.
    """

    def __init__(
        self,
        *,
        max_active: int,
        max_per_node: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_per_node <= max_active or ttl_seconds <= 0:
            raise ValueError("invalid lease bounds")
        self._max_active = max_active
        self._max_per_node = max_per_node
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, WhepLease] = {}
        self._per_node: dict[tuple[UUID, UUID], _NodeCount] = {}
        self._pending = 0
        self._tombstones: OrderedDict[str, tuple[tuple[UUID, UUID], float]] = OrderedDict()
        self._tombstone_capacity = max_active * 8

    # ------------------------------------------------------------------ accounting
    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._leases)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending

    def _occupied(self) -> int:
        return len(self._leases) + self._pending

    def _node(self, key: tuple[UUID, UUID]) -> _NodeCount:
        entry = self._per_node.get(key)
        if entry is None:
            entry = self._per_node[key] = _NodeCount()
        return entry

    def _decrement(self, key: tuple[UUID, UUID]) -> None:
        entry = self._per_node.get(key)
        if entry is not None:
            entry.count -= 1
            if entry.count <= 0:
                del self._per_node[key]

    def _remember_released(self, lease: WhepLease, now: float) -> None:
        self._tombstones[lease.lease_id] = ((lease.tenant_id, lease.edge_node_id), now + self._ttl)
        self._tombstones.move_to_end(lease.lease_id)
        while len(self._tombstones) > self._tombstone_capacity:
            self._tombstones.popitem(last=False)

    # ------------------------------------------------------------------ lifecycle
    def reserve(self, tenant_id: UUID, edge_node_id: UUID) -> LeaseReservation | None:
        key = (tenant_id, edge_node_id)
        with self._lock:
            if self._occupied() >= self._max_active:
                return None
            node = self._node(key)
            if node.count >= self._max_per_node:
                if node.count == 0:
                    del self._per_node[key]
                return None
            node.count += 1
            self._pending += 1
            return LeaseReservation(tenant_id, edge_node_id)

    def cancel(self, reservation: LeaseReservation) -> None:
        with self._lock:
            if reservation.settled:
                return
            reservation.settled = True
            self._pending -= 1
            self._decrement((reservation.tenant_id, reservation.edge_node_id))

    def commit(
        self,
        reservation: LeaseReservation,
        *,
        camera_id: UUID,
        connection_id: UUID,
        provider_session_url: str | None,
    ) -> WhepLease:
        with self._lock:
            if reservation.settled:
                raise RuntimeError("reservation already settled")
            reservation.settled = True
            self._pending -= 1
            now = self._clock()
            lease_id = secrets.token_urlsafe(32)
            while lease_id in self._leases or lease_id in self._tombstones:
                lease_id = secrets.token_urlsafe(32)  # pragma: no cover - 2^-256
            lease = WhepLease(
                lease_id=lease_id,
                tenant_id=reservation.tenant_id,
                edge_node_id=reservation.edge_node_id,
                camera_id=camera_id,
                connection_id=connection_id,
                provider_session_url=provider_session_url,
                created_monotonic=now,
                expires_monotonic=now + self._ttl,
            )
            self._leases[lease_id] = lease
            return lease

    def release_owned(
        self, lease_id: str, tenant_id: UUID, edge_node_id: UUID
    ) -> tuple[ReleaseOutcome, WhepLease | None]:
        """Remove a lease only for its owning node. A lease of another node, an expired
        tombstone and an unknown id are all NOT_FOUND, so a caller learns nothing."""
        owner = (tenant_id, edge_node_id)
        with self._lock:
            now = self._clock()
            lease = self._leases.get(lease_id)
            if lease is not None:
                if (lease.tenant_id, lease.edge_node_id) != owner:
                    return (ReleaseOutcome.NOT_FOUND, None)
                del self._leases[lease_id]
                self._decrement(owner)
                self._remember_released(lease, now)
                return (ReleaseOutcome.RELEASED, lease)
            tombstone = self._tombstones.get(lease_id)
            if tombstone is not None and tombstone[0] == owner and tombstone[1] > now:
                return (ReleaseOutcome.ALREADY_RELEASED, None)
            return (ReleaseOutcome.NOT_FOUND, None)

    def pop_expired(self) -> list[WhepLease]:
        with self._lock:
            now = self._clock()
            expired = [lease for lease in self._leases.values() if lease.expires_monotonic <= now]
            for lease in expired:
                del self._leases[lease.lease_id]
                self._decrement((lease.tenant_id, lease.edge_node_id))
                self._remember_released(lease, now)
            for lease_id in [key for key, value in self._tombstones.items() if value[1] <= now]:
                del self._tombstones[lease_id]
            return expired

    def drain(self) -> list[WhepLease]:
        with self._lock:
            leases = list(self._leases.values())
            now = self._clock()
            for lease in leases:
                self._decrement((lease.tenant_id, lease.edge_node_id))
                self._remember_released(lease, now)
            self._leases.clear()
            return leases


@dataclass(slots=True)
class BrokerMetrics:
    sessions_opened_total: int = 0
    sessions_released_total: int = 0
    sessions_expired_total: int = 0
    provider_release_failures_total: int = 0
    open_failures: dict[str, int] = field(default_factory=dict)


class EdgeWhepBroker:
    def __init__(
        self,
        settings: Settings,
        factory: async_sessionmaker[AsyncSession],
        link_service: RingLinkService,
        client: RingClient,
        registry: WhepLeaseRegistry,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._link = link_service
        self._client = client
        self.registry = registry
        self.metrics = BrokerMetrics()
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------ authorization
    async def authorize(self, principal: EdgePrincipal, camera_id: UUID) -> BrokerTarget:
        """Resolve the Ring identity of ``camera_id`` only if every condition holds.

        Runs under the node's own tenant RLS context, so another tenant's rows are invisible
        before any predicate is evaluated; the predicates repeat the tenant for defence in depth.
        """
        tenant_id = principal.tenant_id
        statement = (
            select(
                Camera.id,
                Camera.provider_connection_id,
                Camera.provider_device_id,
                CameraProviderComponent.provider_component_id,
                CameraProviderComponent.capabilities,
            )
            .select_from(CameraAssignment)
            .join(
                EdgeNode,
                and_(
                    EdgeNode.id == CameraAssignment.edge_node_id,
                    EdgeNode.tenant_id == CameraAssignment.tenant_id,
                ),
            )
            .join(
                Camera,
                and_(
                    Camera.id == CameraAssignment.camera_id,
                    Camera.tenant_id == CameraAssignment.tenant_id,
                ),
            )
            .join(
                CameraProviderComponent,
                and_(
                    CameraProviderComponent.camera_id == Camera.id,
                    CameraProviderComponent.tenant_id == Camera.tenant_id,
                ),
            )
            .join(
                CameraProviderDevice,
                and_(
                    CameraProviderDevice.id == CameraProviderComponent.provider_device_record_id,
                    CameraProviderDevice.tenant_id == Camera.tenant_id,
                ),
            )
            .join(
                CameraProviderConnection,
                and_(
                    CameraProviderConnection.id == Camera.provider_connection_id,
                    CameraProviderConnection.tenant_id == Camera.tenant_id,
                ),
            )
            .where(
                CameraAssignment.tenant_id == tenant_id,
                CameraAssignment.camera_id == camera_id,
                CameraAssignment.edge_node_id == principal.edge_node_id,
                CameraAssignment.ended_at.is_(None),
                EdgeNode.status != "DISABLED",
                Camera.status.in_(USABLE_CAMERA_STATES),
                CameraProviderComponent.inventory_state == "ACTIVE",
                CameraProviderDevice.inventory_state == "ACTIVE",
                CameraProviderDevice.provider_connection_id == Camera.provider_connection_id,
                CameraProviderDevice.provider_device_id == Camera.provider_device_id,
                CameraProviderConnection.provider_type == "RING",
                CameraProviderConnection.status == "ACTIVE",
                CameraProviderConnection.integration_state == "ACTIVE",
                CameraProviderConnection.operational_health.not_in(UNUSABLE_CONNECTION_HEALTH),
            )
        )
        try:
            async with self._factory() as session, session.begin():
                await _set_tenant(session, tenant_id)
                row = (await session.execute(statement)).one_or_none()
        except SQLAlchemyError:
            raise EdgeBrokerError("authorization_unavailable", 503) from None
        if row is None:
            raise _camera_unavailable()
        capabilities = row.capabilities if isinstance(row.capabilities, list) else []
        if CameraCapability.LIVE_VIDEO.value not in capabilities:
            raise _camera_unavailable()
        return BrokerTarget(
            tenant_id=tenant_id,
            camera_id=row.id,
            connection_id=row.provider_connection_id,
            provider_device_id=row.provider_device_id,
            provider_component_id=row.provider_component_id,
        )

    # ------------------------------------------------------------------ Ring side
    async def _ring_token(
        self, tenant_id: UUID, connection_id: UUID, camera_id: UUID, *, force_refresh: bool
    ) -> SecretStr:
        try:
            return await self._link.get_valid_access_token(
                tenant_id, connection_id, force_refresh=force_refresh
            )
        except RingLinkError as exc:
            self._logger.warning(
                "edge_whep_provider_credential_unavailable",
                camera_id=str(camera_id),
                category=exc.category,
            )
            raise EdgeBrokerError("provider_credential_unavailable", 503) from None

    @staticmethod
    def _provider_failure(exc: RingClientError) -> EdgeBrokerError:
        if isinstance(exc, RingAmbiguousResult):
            return EdgeBrokerError("provider_uncertain", 502)
        if exc.category in {"unauthorized", "forbidden"}:
            return EdgeBrokerError("provider_refused", 403)
        if exc.category == "not_found":
            return EdgeBrokerError("provider_camera_unavailable", 503)
        if exc.category == "rate_limited":
            return EdgeBrokerError("provider_rate_limited", 429)
        return EdgeBrokerError("provider_failed", 502)

    async def _create_provider_session(self, target: BrokerTarget, offer: bytes) -> RingWhepSession:
        max_answer = self._settings.edge_whep_max_answer_bytes
        token = await self._ring_token(
            target.tenant_id, target.connection_id, target.camera_id, force_refresh=False
        )
        try:
            return await self._client.create_whep_session(
                token,
                target.provider_device_id,
                target.provider_component_id,
                offer,
                max_answer_bytes=max_answer,
            )
        except RingAmbiguousResult as exc:
            # The POST may have created a session; replaying it could create a second one.
            raise self._provider_failure(exc) from None
        except RingClientError as exc:
            if exc.category != "unauthorized":
                raise self._provider_failure(exc) from None
        # A definite 401: Ring rejected the credential, so no session exists behind it. One
        # lifecycle-controlled refresh through RingLinkService, then exactly one retry.
        token = await self._ring_token(
            target.tenant_id, target.connection_id, target.camera_id, force_refresh=True
        )
        try:
            return await self._client.create_whep_session(
                token,
                target.provider_device_id,
                target.provider_component_id,
                offer,
                max_answer_bytes=max_answer,
            )
        except RingClientError as exc:
            raise self._provider_failure(exc) from None

    async def _release_provider_session(self, lease: WhepLease) -> bool:
        """Best-effort Ring teardown. Never raises; the URL and token never reach a log."""
        url = lease.provider_session_url
        if url is None:
            return True
        for attempt in (0, 1):
            try:
                token = await self._ring_token(
                    lease.tenant_id,
                    lease.connection_id,
                    lease.camera_id,
                    force_refresh=attempt == 1,
                )
                await self._client.delete_whep_session(token, url)
                return True
            except EdgeBrokerError:
                break
            except RingClientError as exc:
                # DELETE is idempotent, so a definite 401 may be retried once after a refresh.
                if exc.category == "unauthorized" and attempt == 0:
                    continue
                self._logger.warning(
                    "edge_whep_provider_release_failed",
                    camera_id=str(lease.camera_id),
                    category=exc.category,
                )
                break
            except Exception:  # pragma: no cover - defensive: teardown must never raise
                self._logger.warning(
                    "edge_whep_provider_release_failed",
                    camera_id=str(lease.camera_id),
                    category="internal_error",
                )
                break
        self.metrics.provider_release_failures_total += 1
        return False

    async def _audit_opened(self, target: BrokerTarget, principal: EdgePrincipal) -> None:
        async with self._factory() as session, session.begin():
            await _set_tenant(session, target.tenant_id)
            session.add(
                AuditEvent(
                    tenant_id=target.tenant_id,
                    actor_id=None,
                    action="edge.whep.session_opened",
                    target_type="camera",
                    target_id=target.camera_id,
                    request_id=f"edge-node:{principal.edge_node_id}",
                    metadata_={"edge_node_id": str(principal.edge_node_id), "provider": "RING"},
                )
            )

    # ------------------------------------------------------------------ public operations
    async def open_session(
        self, principal: EdgePrincipal, camera_id: UUID, offer: bytes
    ) -> tuple[WhepLease, str]:
        """Authorize, negotiate with Ring, register a lease. Returns (lease, SDP answer)."""
        try:
            validated = validate_offer(offer, self._settings.edge_whep_max_offer_bytes)
            reservation = self.registry.reserve(principal.tenant_id, principal.edge_node_id)
            if reservation is None:
                raise EdgeBrokerError("lease_capacity_exhausted", 503)
            try:
                target = await self.authorize(principal, camera_id)
                session = await self._create_provider_session(target, validated)
            except BaseException:
                self.registry.cancel(reservation)
                raise
            lease = self.registry.commit(
                reservation,
                camera_id=target.camera_id,
                connection_id=target.connection_id,
                provider_session_url=session.session_url,
            )
            try:
                await self._audit_opened(target, principal)
            except BaseException:
                # Unaudited access to live video is not granted: undo the session.
                self.registry.release_owned(
                    lease.lease_id, principal.tenant_id, principal.edge_node_id
                )
                await self._release_provider_session(lease)
                raise EdgeBrokerError("audit_unavailable", 503) from None
        except EdgeBrokerError as exc:
            self.metrics.open_failures[exc.category] = (
                self.metrics.open_failures.get(exc.category, 0) + 1
            )
            self._logger.warning(
                "edge_whep_session_refused",
                edge_node_id=str(principal.edge_node_id),
                category=exc.category,
            )
            raise
        self.metrics.sessions_opened_total += 1
        self._logger.info(
            "edge_whep_session_opened",
            edge_node_id=str(principal.edge_node_id),
            camera_id=str(target.camera_id),
            provider_session="present" if session.session_url else "absent",
        )
        return lease, session.answer_sdp

    async def close_session(self, principal: EdgePrincipal, lease_id: str) -> None:
        """Release the node's own lease. Idempotent for the owner; NOT_FOUND otherwise."""
        if not isinstance(lease_id, str) or not _LEASE_ID.fullmatch(lease_id):
            raise EdgeBrokerError("lease_not_found", 404)
        outcome, lease = self.registry.release_owned(
            lease_id, principal.tenant_id, principal.edge_node_id
        )
        if outcome is ReleaseOutcome.NOT_FOUND:
            raise EdgeBrokerError("lease_not_found", 404)
        if lease is None:
            return
        await self._release_provider_session(lease)
        self.metrics.sessions_released_total += 1
        self._logger.info(
            "edge_whep_session_released",
            edge_node_id=str(principal.edge_node_id),
            camera_id=str(lease.camera_id),
        )

    async def release_expired(self) -> int:
        expired = self.registry.pop_expired()
        for lease in expired:
            await self._release_provider_session(lease)
            self.metrics.sessions_expired_total += 1
            self._logger.info(
                "edge_whep_session_expired",
                edge_node_id=str(lease.edge_node_id),
                camera_id=str(lease.camera_id),
            )
        return len(expired)

    async def run_expiry_loop(self, interval_seconds: float | None = None) -> None:
        interval = interval_seconds or max(
            5.0, min(60.0, self._settings.edge_whep_lease_ttl_seconds / 4)
        )
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self.release_expired()

    async def shutdown(self, timeout_seconds: float = 10.0) -> int:
        """Attempt to close every outstanding Ring session, bounded in total time."""
        leases = self.registry.drain()
        if not leases:
            return 0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(self._release_provider_session(lease) for lease in leases)),
                timeout_seconds,
            )
        self._logger.info("edge_whep_sessions_closed_at_shutdown", count=len(leases))
        return len(leases)


async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
