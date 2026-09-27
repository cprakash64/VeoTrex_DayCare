"""Edge node machine authentication (V1-DEMO-03B).

An EdgeNode is a machine, not a person, so it never authenticates through the Auth0
``IdentityVerifier``. It presents a dedicated credential that the admin tool issued once:

    vte1.<credential uuid>.<43 base64url characters = 256 random bits>

The uuid is a public selector, so authentication is one primary-key lookup rather than a scan.
Only ``SHA-256(domain || selector || secret)`` is stored; binding the selector into the digest
means a digest copied onto another row does not authenticate. Because the secret is 256 bits
from the operating system CSPRNG, offline guessing against a leaked digest is infeasible and a
slow password hash would add nothing.

The tenant is never taken from the caller. ``authenticate_edge_node_credential`` (migration
0008, SECURITY DEFINER) resolves (tenant, node, facility) from the credential itself, and only
that result becomes ``app.tenant_id`` for later queries. Every failure - malformed header,
unknown selector, wrong secret, revoked credential, disabled node - is the same generic 401,
and the presented value never reaches a log, an exception message or a response.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Annotated
from uuid import UUID, uuid4

import structlog
from fastapi import Depends, HTTPException, Request, status
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.access import AuthenticationFailureLogLimiter

TOKEN_VERSION = "vte1"  # noqa: S105 - format version, not a credential
TOKEN_PREFIX = f"{TOKEN_VERSION}."
SECRET_BYTES = 32
# "vte1." + 36-character uuid + "." + 43 characters. Anything longer is refused before parsing.
TOKEN_LENGTH = len(TOKEN_PREFIX) + 36 + 1 + 43
# "Bearer " plus the token, with room for nothing else.
MAX_AUTHORIZATION_HEADER_BYTES = 128
_TOKEN = re.compile(
    r"^vte1\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([A-Za-z0-9_-]{43})$"
)
_DIGEST_DOMAIN = b"veotrex-edge-node-credential-v1\x00"
_AUTHENTICATE_SQL = text(
    "SELECT tenant_id, edge_node_id, facility_id "
    "FROM authenticate_edge_node_credential(:credential_id, :secret_sha256)"
)


class MalformedEdgeCredential(ValueError):
    """The presented value is not a well-formed edge credential. Never carries the value."""

    def __init__(self) -> None:
        super().__init__("malformed edge credential")


class EdgeAuthenticationFailed(Exception):
    """Generic authentication failure. ``reason`` is a fixed category for logs only."""

    def __init__(self, reason: str) -> None:
        super().__init__("edge authentication failed")
        self.reason = reason


class EdgeAuthenticationUnavailable(Exception):
    """The credential could not be checked (database unavailable). Fails closed."""


def credential_digest(credential_id: UUID, secret: str) -> bytes:
    """Domain-separated SHA-256 of the secret, bound to its selector."""
    return hashlib.sha256(_DIGEST_DOMAIN + credential_id.bytes + secret.encode("ascii")).digest()


@dataclass(frozen=True, slots=True, repr=False)
class IssuedEdgeCredential:
    """A freshly generated credential. ``token`` is shown to nobody; it goes to one file."""

    credential_id: UUID
    token: SecretStr
    secret_sha256: bytes = field(repr=False)

    def __repr__(self) -> str:
        return f"IssuedEdgeCredential(credential_id={self.credential_id}, token=**********)"

    __str__ = __repr__


def issue_edge_credential() -> IssuedEdgeCredential:
    credential_id = uuid4()
    secret = base64.urlsafe_b64encode(secrets.token_bytes(SECRET_BYTES)).rstrip(b"=").decode()
    return IssuedEdgeCredential(
        credential_id,
        SecretStr(f"{TOKEN_PREFIX}{credential_id}.{secret}"),
        credential_digest(credential_id, secret),
    )


def parse_edge_token(raw: object) -> tuple[UUID, bytes]:
    """Return (selector, presented digest) or raise ``MalformedEdgeCredential``.

    Strict: canonical lower-case uuid, exactly 43 base64url characters that decode to exactly
    32 bytes and re-encode to the same text. The secret leaves this function only as a digest.
    """
    if not isinstance(raw, str) or len(raw) != TOKEN_LENGTH:
        raise MalformedEdgeCredential
    match = _TOKEN.fullmatch(raw)
    if match is None:
        raise MalformedEdgeCredential
    selector, secret = match.group(1), match.group(2)
    try:
        credential_id = UUID(selector)
        decoded = base64.urlsafe_b64decode(secret + "=")
    except ValueError:
        raise MalformedEdgeCredential from None
    if str(credential_id) != selector or len(decoded) != SECRET_BYTES:
        raise MalformedEdgeCredential
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != secret:
        raise MalformedEdgeCredential  # non-canonical trailing bits
    return credential_id, credential_digest(credential_id, secret)


@dataclass(frozen=True, slots=True)
class EdgePrincipal:
    """What an authenticated edge node is, resolved server-side. Nothing else."""

    tenant_id: UUID
    edge_node_id: UUID
    facility_id: UUID


def bearer_value(headers: list[str]) -> str:
    """Extract the single ``Bearer`` value from the Authorization header(s), or refuse."""
    if len(headers) != 1:
        raise EdgeAuthenticationFailed("missing_or_repeated_header")
    header = headers[0]
    if len(header.encode("utf-8", errors="replace")) > MAX_AUTHORIZATION_HEADER_BYTES:
        raise EdgeAuthenticationFailed("oversized_header")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value or value != value.strip():
        raise EdgeAuthenticationFailed("malformed_header")
    return value


class EdgeAuthenticator:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        failure_log_limiter: AuthenticationFailureLogLimiter | None = None,
    ) -> None:
        self._factory = factory
        self._log_limiter = failure_log_limiter or AuthenticationFailureLogLimiter()

    async def authenticate(self, authorization_headers: list[str]) -> EdgePrincipal:
        try:
            try:
                credential_id, digest = parse_edge_token(bearer_value(authorization_headers))
            except MalformedEdgeCredential:
                raise EdgeAuthenticationFailed("malformed_credential") from None
            try:
                async with self._factory() as session, session.begin():
                    row = (
                        await session.execute(
                            _AUTHENTICATE_SQL,
                            {"credential_id": credential_id, "secret_sha256": digest},
                        )
                    ).one_or_none()
            except SQLAlchemyError:
                structlog.get_logger().warning(
                    "edge_authentication_unavailable", dependency="database"
                )
                raise EdgeAuthenticationUnavailable from None
            if row is None:
                raise EdgeAuthenticationFailed("credential_rejected")
            return EdgePrincipal(
                tenant_id=row.tenant_id,
                edge_node_id=row.edge_node_id,
                facility_id=row.facility_id,
            )
        except EdgeAuthenticationFailed as exc:
            if self._log_limiter.allow():
                # The category only: never the header, the selector or the secret.
                structlog.get_logger().warning("edge_authentication_failed", reason=exc.reason)
            raise


def _edge_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate_edge_node(request: Request) -> EdgePrincipal:
    """FastAPI dependency for the ``/v1/edge/...`` namespace only."""
    authenticator: EdgeAuthenticator = request.app.state.edge_authenticator
    try:
        return await authenticator.authenticate(request.headers.getlist("authorization"))
    except EdgeAuthenticationFailed:
        raise _edge_unauthorized() from None
    except EdgeAuthenticationUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="service unavailable"
        ) from None


EdgePrincipalDependency = Annotated[EdgePrincipal, Depends(authenticate_edge_node)]
