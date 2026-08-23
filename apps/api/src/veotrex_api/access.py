from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.authorization import (
    Permission,
    Role,
    RoleGrant,
    has_permission,
    permissions_for,
)
from veotrex_api.identity import ExternalIdentity, IdentityVerificationError, IdentityVerifier
from veotrex_api.models import Actor, ActorIdentity, RoleAssignment

bearer = HTTPBearer(auto_error=False)


class AuthenticationFailureLogLimiter:
    def __init__(self, limit: int = 20, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._window_started = monotonic()
        self._count = 0
        self._lock = Lock()

    def allow(self) -> bool:
        with self._lock:
            now = monotonic()
            if now - self._window_started >= self._window_seconds:
                self._window_started = now
                self._count = 0
            if self._count >= self._limit:
                return False
            self._count += 1
            return True


authentication_failure_log_limiter = AuthenticationFailureLogLimiter()


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    issuer: str
    subject: str
    external_organization_id: str
    actor_id: UUID
    tenant_id: UUID
    display_name: str | None
    grants: tuple[RoleGrant, ...]
    permissions: frozenset[Permission]


@dataclass(slots=True)
class PrincipalContext:
    principal: AuthenticatedPrincipal
    session: AsyncSession


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")


async def verify_request_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> ExternalIdentity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    verifier: IdentityVerifier = request.app.state.identity_verifier
    try:
        return await verifier.verify(credentials.credentials)
    except IdentityVerificationError as exc:
        # Log only the failure class and request metadata already bound by middleware.
        if authentication_failure_log_limiter.allow():
            structlog.get_logger().warning("authentication_failed", reason=type(exc).__name__)
        raise _unauthorized() from None


async def resolve_authenticated_principal(
    request: Request,
    external: Annotated[ExternalIdentity, Depends(verify_request_identity)],
) -> AsyncIterator[PrincipalContext]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session, session.begin():
        tenant_id = await session.scalar(
            text("SELECT resolve_tenant_identity_binding(:provider, :issuer, :organization)"),
            {
                "provider": external.provider,
                "issuer": external.issuer,
                "organization": external.external_organization_id,
            },
        )
        if not isinstance(tenant_id, UUID):
            raise _forbidden()

        # The RLS context comes only from the exact server-side binding result.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        actor_row = (
            await session.execute(
                select(Actor.id, Actor.display_name)
                .join(ActorIdentity, ActorIdentity.actor_id == Actor.id)
                .where(
                    Actor.tenant_id == tenant_id,
                    Actor.status == "ACTIVE",
                    ActorIdentity.tenant_id == tenant_id,
                    ActorIdentity.provider == external.provider,
                    ActorIdentity.issuer == external.issuer,
                    ActorIdentity.subject == external.subject,
                    ActorIdentity.archived_at.is_(None),
                )
            )
        ).one_or_none()
        if actor_row is None:
            raise _forbidden()

        assignment_rows = (
            await session.execute(
                select(RoleAssignment.role, RoleAssignment.facility_id).where(
                    RoleAssignment.tenant_id == tenant_id,
                    RoleAssignment.actor_id == actor_row.id,
                    RoleAssignment.archived_at.is_(None),
                )
            )
        ).all()
        try:
            grants = tuple(RoleGrant(Role(row.role), row.facility_id) for row in assignment_rows)
        except ValueError:
            structlog.get_logger().error("invalid_role_assignment", actor_id=str(actor_row.id))
            raise _forbidden() from None
        if not grants:
            raise _forbidden()

        await session.execute(
            update(ActorIdentity)
            .where(
                ActorIdentity.tenant_id == tenant_id,
                ActorIdentity.actor_id == actor_row.id,
                ActorIdentity.provider == external.provider,
                ActorIdentity.issuer == external.issuer,
                ActorIdentity.subject == external.subject,
            )
            .values(last_authenticated_at=text("now()"))
        )
        principal = AuthenticatedPrincipal(
            issuer=external.issuer,
            subject=external.subject,
            external_organization_id=external.external_organization_id,
            actor_id=actor_row.id,
            tenant_id=tenant_id,
            display_name=actor_row.display_name,
            grants=grants,
            permissions=permissions_for(grants),
        )
        yield PrincipalContext(principal=principal, session=session)


PrincipalDependency = Annotated[PrincipalContext, Depends(resolve_authenticated_principal)]


def require_permission(permission: Permission) -> Callable[[PrincipalDependency], PrincipalContext]:
    def dependency(context: PrincipalDependency) -> PrincipalContext:
        if not has_permission(context.principal.grants, permission):
            raise _forbidden()
        return context

    return dependency
