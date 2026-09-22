from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission
from veotrex_api.config import Settings
from veotrex_api.credential_vault import (
    CredentialContext,
    CredentialMaterial,
    CredentialVault,
    CredentialVaultError,
    CredentialVersionConflict,
)
from veotrex_api.models import AuditEvent, CameraProviderConnection, Tenant
from veotrex_api.ring_client import RingAmbiguousResult, RingClient, RingClientError
from veotrex_api.ring_nonce import (
    InvalidRingLink,
    ring_nonce_matches,
    validate_ring_timestamp,
)
from veotrex_api.ring_repository import (
    ConnectionState,
    PendingCandidate,
    PendingLinkState,
    RingPendingRepository,
)
from veotrex_api.secrets import SecretResolver


class RingLinkError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(f"Ring link failed: {category}")
        self.category = category


class TokenReceiptState(StrEnum):
    UNCLAIMED = "UNCLAIMED"
    ACCOUNT_LOOKUP_PENDING = "ACCOUNT_LOOKUP_PENDING"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    connection_id: UUID
    state: ConnectionState


@dataclass(frozen=True, slots=True)
class LinkContext:
    tenant_name: str
    eligible: bool


ACCOUNT_IDENTIFIER_MAX = 64


def partner_account_identifier(principal: AuthenticatedPrincipal) -> str:
    """The obfuscated partner account identifier Ring shows the user during linking.

    The Partner API documents it as "an obfuscated identifier for the partner user account
    (e.g., masked email) ... must be derived from the user's actual signed-in session". The
    VeoTrex principal carries no email (the Auth0 access token has ``sub`` and ``org_id`` only),
    so the identifier is the signed-in actor's display name masked to its first and last
    character, or a masked actor id when no display name exists. Deterministic for one actor,
    printable ASCII, bounded, and never a raw name, email, subject or tenant id.
    """
    name = (principal.display_name or "").strip()
    printable = "".join(ch for ch in name if 33 <= ord(ch) <= 126)
    if len(printable) >= 2:
        masked = f"{printable[0]}***{printable[-1]}"
    else:
        digest = principal.actor_id.hex
        masked = f"{digest[:2]}***{digest[-2:]}"
    return f"{masked}@veotrex"[:ACCOUNT_IDENTIFIER_MAX]


def _credential_context(owner_id: UUID, tenant_id: UUID | None = None) -> CredentialContext:
    return CredentialContext(
        provider="RING", owner_kind="ring_pending_link", owner_id=owner_id, tenant_id=tenant_id
    )


def ring_credential_context(owner_id: UUID, tenant_id: UUID | None = None) -> CredentialContext:
    """Return the context binding used by Stage 1B vault records.

    ``tenant_id`` is the caller's tenant for operations that happen after a link is claimed; it
    lets the production vault authorize the operation against the tenant-scoped connection.
    Pre-tenant operations (token receipt and its clean-up) pass none.
    """
    return _credential_context(owner_id, tenant_id)


class RingLinkService:
    def __init__(
        self,
        settings: Settings,
        factory: async_sessionmaker[AsyncSession],
        vault: CredentialVault,
        client: RingClient,
        secrets: SecretResolver,
        repository: RingPendingRepository | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._vault = vault
        self._client = client
        self._secrets = secrets
        self._pending = repository or RingPendingRepository()

    async def receive_authorization_code(self, code: SecretStr) -> TokenReceiptState:
        # One attempt only. A timeout is ambiguous because the code is one-use.
        tokens = await self._client.exchange_authorization_code(code)
        pending_id = uuid4()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=tokens.expires_in)
        stored = await self._vault.store_new(
            _credential_context(pending_id),
            CredentialMaterial(tokens.access_token, tokens.refresh_token),
        )
        try:
            async with self._factory() as session, session.begin():
                await self._pending.create(
                    session,
                    pending_id=pending_id,
                    secret_ref=stored.secret_ref,
                    generation=stored.version,
                    access_expires_at=expires_at,
                )
        except Exception:
            await self._vault.delete(stored.secret_ref, _credential_context(pending_id))
            raise

        try:
            account_id = await self._client.get_account_id(tokens.access_token)
        except RingClientError as exc:
            # The safely vaulted credential stays operator-visible and recoverable until
            # access expiry; the one-time authorization code is never replayed.
            async with self._factory() as session, session.begin():
                await self._pending.record_failure(session, pending_id, exc.category)
            return TokenReceiptState.ACCOUNT_LOOKUP_PENDING

        try:
            async with self._factory() as session, session.begin():
                if not await self._pending.complete_account(session, pending_id, account_id):
                    raise RingLinkError("pending_state_conflict")
        except IntegrityError as exc:
            # The partial unique index refuses duplicate eligible Ring accounts.
            await self._vault.delete(stored.secret_ref, _credential_context(pending_id))
            await self._transition_pending(
                pending_id,
                PendingLinkState.RECEIVED,
                PendingLinkState.FAILED,
                "account_already_pending",
            )
            raise RingLinkError("account_already_pending") from exc
        return TokenReceiptState.UNCLAIMED

    async def claim(
        self,
        principal: AuthenticatedPrincipal,
        *,
        timestamp_ms: int,
        nonce: str,
        request_id: str,
    ) -> ClaimResult:
        if Permission.MANAGE_INTEGRATIONS not in principal.permissions:
            raise RingLinkError("access_denied")
        candidate = await self._matching_candidate(timestamp_ms, nonce)

        async with self._factory() as session, session.begin():
            won = await self._pending.start_claim(
                session, candidate.id, principal.tenant_id, principal.actor_id
            )
        if not won:
            raise RingLinkError("link_already_used")

        try:
            credential = await self._vault.get(
                candidate.secret_ref, _credential_context(candidate.id, principal.tenant_id)
            )
            if credential.version != candidate.generation:
                raise CredentialVersionConflict("credential generation changed")
        except CredentialVaultError as exc:
            await self._transition_pending(
                candidate.id,
                PendingLinkState.CLAIMING,
                PendingLinkState.FAILED,
                "credential_unavailable",
            )
            raise RingLinkError("credential_unavailable") from exc

        try:
            await self._client.confirm_app_integration(
                credential.material.access_token, nonce, partner_account_identifier(principal)
            )
        except RingAmbiguousResult as exc:
            await self._transition_pending(
                candidate.id,
                PendingLinkState.CLAIMING,
                PendingLinkState.RING_CONFIRMATION_UNCERTAIN,
                exc.category,
            )
            raise RingLinkError("ring_confirmation_uncertain") from exc
        except RingClientError as exc:
            await self._transition_pending(
                candidate.id, PendingLinkState.CLAIMING, PendingLinkState.FAILED, exc.category
            )
            raise RingLinkError("ring_confirmation_failed") from exc

        connection_id = uuid4()
        try:
            async with self._factory() as session, session.begin():
                await self._set_tenant(session, principal.tenant_id)
                tenant_name = await session.scalar(
                    select(Tenant.name).where(Tenant.id == principal.tenant_id)
                )
                if not isinstance(tenant_name, str):
                    raise RingLinkError("tenant_unavailable")
                connection = CameraProviderConnection(
                    id=connection_id,
                    tenant_id=principal.tenant_id,
                    facility_id=None,
                    name=f"Ring account {connection_id.hex[:8]}",
                    provider_type="RING",
                    secret_ref=candidate.secret_ref,
                    credential_owner_id=candidate.id,
                    status="PENDING",
                    external_account_id=candidate.ring_account_id,
                    integration_state=ConnectionState.CONFIGURING.value,
                    linked_by_actor_id=principal.actor_id,
                    linked_at=datetime.now(UTC),
                    access_expires_at=candidate.access_expires_at,
                    credential_generation=candidate.generation,
                )
                session.add(connection)
                if not await self._pending.transition(
                    session, candidate.id, PendingLinkState.CLAIMING, PendingLinkState.CLAIMED
                ):
                    raise RingLinkError("pending_state_conflict")
                self._audit(
                    session,
                    principal,
                    connection_id,
                    "integration.ring.claimed",
                    request_id,
                    {"provider": "RING", "to_state": ConnectionState.CONFIGURING.value},
                )
                await session.flush()
        except (IntegrityError, RingLinkError) as exc:
            await self._transition_pending(
                candidate.id,
                PendingLinkState.CLAIMING,
                PendingLinkState.RING_CONFIRMED_UNBOUND,
                "binding_conflict",
            )
            raise RingLinkError("connection_conflict") from exc

        try:
            await self._client.complete_app_integration(
                credential.material.access_token, partner_account_identifier(principal)
            )
        except RingClientError as exc:
            await self._record_connection_failure(principal.tenant_id, connection_id, exc.category)
            return ClaimResult(connection_id, ConnectionState.CONFIGURING)
        await self._activate(principal, connection_id, request_id)
        return ClaimResult(connection_id, ConnectionState.ACTIVE)

    async def link_context(
        self, principal: AuthenticatedPrincipal, timestamp_ms: int, nonce: str
    ) -> LinkContext:
        if Permission.MANAGE_INTEGRATIONS not in principal.permissions:
            raise RingLinkError("access_denied")
        eligible = True
        try:
            await self._matching_candidate(timestamp_ms, nonce)
        except RingLinkError:
            eligible = False
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            tenant_name = await session.scalar(
                select(Tenant.name).where(Tenant.id == principal.tenant_id)
            )
        if not isinstance(tenant_name, str):
            raise RingLinkError("tenant_unavailable")
        return LinkContext(tenant_name, eligible)

    async def resume_completion(
        self, principal: AuthenticatedPrincipal, connection_id: UUID, request_id: str
    ) -> ClaimResult:
        if Permission.MANAGE_INTEGRATIONS not in principal.permissions:
            raise RingLinkError("access_denied")
        access_token = await self.get_valid_access_token(principal.tenant_id, connection_id)
        try:
            await self._client.complete_app_integration(
                access_token, partner_account_identifier(principal)
            )
        except RingClientError as exc:
            await self._record_connection_failure(principal.tenant_id, connection_id, exc.category)
            raise RingLinkError("completion_failed") from exc
        await self._activate(principal, connection_id, request_id)
        return ClaimResult(connection_id, ConnectionState.ACTIVE)

    async def get_valid_access_token(
        self, tenant_id: UUID, connection_id: UUID, *, force_refresh: bool = False
    ) -> SecretStr:
        failure: RingLinkError | None = None
        result: SecretStr | None = None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection)
                .where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == tenant_id,
                    CameraProviderConnection.provider_type == "RING",
                )
                .with_for_update()
            )
            if (
                connection is None
                or connection.integration_state
                in {
                    ConnectionState.DISCONNECTED.value,
                    ConnectionState.ARCHIVED.value,
                    ConnectionState.REAUTH_REQUIRED.value,
                    ConnectionState.REFRESH_UNCERTAIN.value,
                }
                or connection.secret_ref is None
                or connection.credential_owner_id is None
                or connection.access_expires_at is None
            ):
                raise RingLinkError("credential_unavailable")
            context = _credential_context(connection.credential_owner_id, tenant_id)
            try:
                credential = await self._vault.get(connection.secret_ref, context)
            except CredentialVaultError:
                connection.integration_state = ConnectionState.REAUTH_REQUIRED.value
                connection.last_failure_category = "credential_unavailable"
                self._system_reauth_audit(session, tenant_id, connection_id)
                failure = RingLinkError("credential_unavailable")
                credential = None
            if credential is not None and credential.version != connection.credential_generation:
                connection.integration_state = ConnectionState.REFRESH_UNCERTAIN.value
                connection.last_failure_category = "generation_mismatch"
                failure = RingLinkError("credential_generation_mismatch")
            if credential is not None and failure is None:
                refresh_at = connection.access_expires_at - timedelta(
                    seconds=self._settings.ring_access_token_refresh_margin_seconds
                )
                if datetime.now(UTC) < refresh_at and not force_refresh:
                    result = credential.material.access_token
                else:
                    try:
                        tokens = await self._client.refresh(credential.material.refresh_token)
                    except RingAmbiguousResult as exc:
                        connection.integration_state = ConnectionState.REFRESH_UNCERTAIN.value
                        connection.last_failure_category = exc.category
                        failure = RingLinkError("refresh_uncertain")
                    except RingClientError as exc:
                        connection.integration_state = ConnectionState.REAUTH_REQUIRED.value
                        connection.last_failure_category = exc.category
                        self._system_reauth_audit(session, tenant_id, connection_id)
                        failure = RingLinkError("reauth_required")
                    else:
                        try:
                            replacement = await self._vault.replace_if_version(
                                connection.secret_ref,
                                context,
                                connection.credential_generation,
                                CredentialMaterial(tokens.access_token, tokens.refresh_token),
                            )
                        except CredentialVaultError:
                            connection.integration_state = ConnectionState.REFRESH_UNCERTAIN.value
                            connection.last_failure_category = "vault_rotation_failed"
                            failure = RingLinkError("refresh_uncertain")
                        else:
                            now = datetime.now(UTC)
                            connection.credential_generation = replacement.version
                            connection.access_expires_at = now + timedelta(
                                seconds=tokens.expires_in
                            )
                            connection.last_refresh_at = now
                            connection.last_failure_category = None
                            result = replacement.material.access_token
        if failure is not None:
            raise failure
        if result is None:
            raise RingLinkError("credential_unavailable")
        return result

    async def due_for_proactive_refresh(self, tenant_id: UUID) -> tuple[UUID, ...]:
        threshold = datetime.now(UTC) + timedelta(
            seconds=self._settings.ring_access_token_refresh_margin_seconds
        )
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, tenant_id)
            values = (
                await session.scalars(
                    select(CameraProviderConnection.id).where(
                        CameraProviderConnection.tenant_id == tenant_id,
                        CameraProviderConnection.provider_type == "RING",
                        CameraProviderConnection.integration_state.in_(
                            [ConnectionState.ACTIVE.value, ConnectionState.CONFIGURING.value]
                        ),
                        CameraProviderConnection.access_expires_at <= threshold,
                    )
                )
            ).all()
            return tuple(values)

    async def disconnect(
        self, principal: AuthenticatedPrincipal, connection_id: UUID, request_id: str
    ) -> None:
        if Permission.MANAGE_INTEGRATIONS not in principal.permissions:
            raise RingLinkError("access_denied")
        secret: tuple[str, UUID] | None = None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection)
                .where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == principal.tenant_id,
                    CameraProviderConnection.provider_type == "RING",
                )
                .with_for_update()
            )
            if connection is None:
                raise RingLinkError("connection_not_found")
            if connection.secret_ref is not None and connection.credential_owner_id is not None:
                secret = (connection.secret_ref, connection.credential_owner_id)
            connection.integration_state = ConnectionState.DISCONNECTED.value
            connection.status = "DISABLED"
            connection.disconnected_at = datetime.now(UTC)
            self._audit(
                session,
                principal,
                connection_id,
                "integration.ring.disconnected",
                request_id,
                {"provider": "RING", "remote_revocation": "not_performed"},
            )
        if secret is not None:
            await self._vault.delete(secret[0], _credential_context(secret[1], principal.tenant_id))
            async with self._factory() as session, session.begin():
                await self._set_tenant(session, principal.tenant_id)
                connection = await session.scalar(
                    select(CameraProviderConnection).where(
                        CameraProviderConnection.id == connection_id,
                        CameraProviderConnection.tenant_id == principal.tenant_id,
                    )
                )
                if connection is not None and connection.integration_state == "DISCONNECTED":
                    connection.secret_ref = None

    async def _activate(
        self, principal: AuthenticatedPrincipal, connection_id: UUID, request_id: str
    ) -> None:
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection).where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == principal.tenant_id,
                    CameraProviderConnection.integration_state == ConnectionState.CONFIGURING.value,
                )
            )
            if connection is None:
                raise RingLinkError("connection_not_configurable")
            connection.integration_state = ConnectionState.ACTIVE.value
            connection.status = "ACTIVE"
            connection.last_failure_category = None
            self._audit(
                session,
                principal,
                connection_id,
                "integration.ring.activated",
                request_id,
                {"provider": "RING", "to_state": ConnectionState.ACTIVE.value},
            )

    async def _record_connection_failure(
        self, tenant_id: UUID, connection_id: UUID, category: str
    ) -> None:
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection).where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == tenant_id,
                )
            )
            if connection is not None:
                connection.last_failure_category = category

    async def _transition_pending(
        self,
        pending_id: UUID,
        expected: PendingLinkState,
        requested: PendingLinkState,
        failure: str | None,
    ) -> None:
        async with self._factory() as session, session.begin():
            await self._pending.transition(session, pending_id, expected, requested, failure)

    async def _matching_candidate(self, timestamp_ms: int, nonce: str) -> PendingCandidate:
        try:
            validate_ring_timestamp(
                timestamp_ms,
                validation_window_seconds=self._settings.ring_nonce_validation_window_seconds,
                future_tolerance_seconds=self._settings.ring_nonce_future_tolerance_seconds,
            )
        except InvalidRingLink as exc:
            raise RingLinkError("invalid_or_expired_link") from exc
        key = self._secrets.resolve(self._settings.ring_hmac_signing_key_ref).get_secret_value()
        received_after = datetime.now(UTC) - timedelta(
            seconds=self._settings.ring_pending_candidate_max_age_seconds
        )
        async with self._factory() as session, session.begin():
            candidates = await self._pending.candidates(session, received_after)
        matches = tuple(
            candidate
            for candidate in candidates
            if ring_nonce_matches(nonce, timestamp_ms, candidate.ring_account_id, key)
        )
        if len(matches) != 1:
            raise RingLinkError("invalid_or_expired_link")
        return matches[0]

    @staticmethod
    async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    @staticmethod
    def _audit(
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        connection_id: UUID,
        action: str,
        request_id: str,
        metadata: dict[str, str],
    ) -> None:
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_id=principal.actor_id,
                action=action,
                target_type="camera_provider_connection",
                target_id=connection_id,
                request_id=request_id[:128],
                metadata_=metadata,
            )
        )

    @staticmethod
    def _system_reauth_audit(session: AsyncSession, tenant_id: UUID, connection_id: UUID) -> None:
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_id=None,
                action="integration.ring.reauth_required",
                target_type="camera_provider_connection",
                target_id=connection_id,
                request_id="ring-token-maintenance",
                metadata_={"provider": "RING", "to_state": "REAUTH_REQUIRED"},
            )
        )
