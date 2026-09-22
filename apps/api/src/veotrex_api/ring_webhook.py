from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.credential_vault import CredentialVault, CredentialVaultError
from veotrex_api.models import (
    AuditEvent,
    Camera,
    CameraProviderComponent,
    CameraProviderConnection,
    CameraProviderDevice,
    ProviderEvent,
)
from veotrex_api.ring_inventory_service import RingInventoryService
from veotrex_api.ring_service import ring_credential_context
from veotrex_api.secrets import SecretResolver

_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")
_KNOWN_EVENTS = {
    "motion_detected",
    "button_press",
    "device_added",
    "device_removed",
    "device_online",
    "device_offline",
    "app_integration_added",
    "app_integration_removed",
    "subscription_activated",
    "subscription_deactivated",
}


class RingWebhookError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(f"Ring webhook failed: {category}")
        self.category = category


class WebhookMeta(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: str = Field(min_length=1, max_length=16)
    time: datetime
    request_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=512)


class WebhookIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=512)


class WebhookRelationship(BaseModel):
    model_config = ConfigDict(extra="allow")
    data: WebhookIdentifier | list[WebhookIdentifier] | None = None


class WebhookEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1, max_length=512)
    type: str = Field(min_length=1, max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, WebhookRelationship] = Field(default_factory=dict)


class WebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    meta: WebhookMeta
    data: WebhookEvent


@dataclass(frozen=True, slots=True)
class NormalizedWebhook:
    request_id: str
    version: str
    envelope_time: datetime
    account_id: str
    event_id: str
    event_type: str
    source_id: str | None
    source_type: str | None
    event_timestamp_ms: int | None
    sub_type: str | None
    component_ids: tuple[str, ...]
    related_device_ids: tuple[str, ...]


def verify_signature(raw_body: bytes, signature: str | None, key: str) -> bool:
    if signature is None:
        return False
    match = _SIGNATURE.fullmatch(signature)
    if match is None:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1))


def parse_webhook(raw_body: bytes) -> NormalizedWebhook:
    try:
        envelope = WebhookEnvelope.model_validate_json(raw_body)
    except (ValidationError, ValueError) as exc:
        raise RingWebhookError("malformed_envelope") from exc
    if envelope.meta.version != "1.1":
        raise RingWebhookError("unsupported_version")
    attributes = envelope.data.attributes
    component_ids = attributes.get("component_ids", [])
    if component_ids is None:
        component_ids = []
    if not isinstance(component_ids, list) or not all(
        isinstance(value, str) and 0 < len(value) <= 512 for value in component_ids
    ):
        raise RingWebhookError("malformed_component_ids")
    raw_source = attributes.get("source")
    raw_source_type = attributes.get("source_type")
    raw_source_id = attributes.get("source_id")
    for value in (raw_source, raw_source_type, raw_source_id):
        if value is not None and (not isinstance(value, str) or not 0 < len(value) <= 512):
            raise RingWebhookError("malformed_source")
    source_id: str | None = raw_source if raw_source_type == "devices" else raw_source_id
    source_type: str | None = raw_source_type
    source = envelope.data.relationships.get("source")
    if source_id is None and source is not None and isinstance(source.data, WebhookIdentifier):
        source_id, source_type = source.data.id, source.data.type
    related_ids: list[str] = []
    related = envelope.data.relationships.get("devices")
    if related is not None:
        identifiers = related.data if isinstance(related.data, list) else []
        related_ids = [identifier.id for identifier in identifiers]
    timestamp = attributes.get("timestamp")
    if timestamp is not None and (not isinstance(timestamp, int) or isinstance(timestamp, bool)):
        raise RingWebhookError("malformed_event_timestamp")
    sub_type = attributes.get("sub_type")
    if sub_type is not None and (not isinstance(sub_type, str) or len(sub_type) > 128):
        raise RingWebhookError("malformed_sub_type")
    return NormalizedWebhook(
        envelope.meta.request_id,
        envelope.meta.version,
        envelope.meta.time,
        envelope.meta.account_id,
        envelope.data.id,
        envelope.data.type,
        source_id,
        source_type,
        timestamp,
        sub_type,
        tuple(component_ids),
        tuple(related_ids),
    )


class RingWebhookService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        secrets: SecretResolver,
        signing_key_ref: str,
        vault: CredentialVault,
        inventory: RingInventoryService,
    ) -> None:
        self._factory = factory
        self._secrets = secrets
        self._signing_key_ref = signing_key_ref
        self._vault = vault
        self._inventory = inventory

    async def ingest(self, raw_body: bytes, signature: str | None) -> bool:
        try:
            key = self._secrets.resolve(self._signing_key_ref).get_secret_value()
        except Exception as exc:
            raise RingWebhookError("verification_unavailable") from exc
        if not verify_signature(raw_body, signature, key):
            raise RingWebhookError("invalid_signature")
        value = parse_webhook(raw_body)
        async with self._factory() as session, session.begin():
            inserted = await session.scalar(
                text(
                    "SELECT ingest_ring_webhook(:id, :request_id, :version, :envelope_time, "
                    ":account_id, :event_id, :event_type, :source_id, :source_type, "
                    ":timestamp_ms, :sub_type, CAST(:component_ids AS json), "
                    "CAST(:device_ids AS json))"
                ),
                {
                    "id": uuid4(),
                    "request_id": value.request_id,
                    "version": value.version,
                    "envelope_time": value.envelope_time,
                    "account_id": value.account_id,
                    "event_id": value.event_id,
                    "event_type": value.event_type,
                    "source_id": value.source_id,
                    "source_type": value.source_type,
                    "timestamp_ms": value.event_timestamp_ms,
                    "sub_type": value.sub_type,
                    "component_ids": json.dumps(value.component_ids),
                    "device_ids": json.dumps(value.related_device_ids),
                },
            )
        return bool(inserted)

    async def process_one(self) -> bool:
        async with self._factory() as session, session.begin():
            row = (
                (await session.execute(text("SELECT * FROM claim_next_ring_webhook()")))
                .mappings()
                .first()
            )
        if row is None:
            return False
        inbox_id = UUID(str(row["id"]))
        try:
            async with self._factory() as session, session.begin():
                resolved = (
                    (
                        await session.execute(
                            text("SELECT * FROM resolve_ring_webhook_connection(:account_id)"),
                            {"account_id": row["ring_account_id"]},
                        )
                    )
                    .mappings()
                    .first()
                )
            if resolved is None:
                await self._finish(inbox_id, "FAILED_PERMANENT", "unknown_account")
                return True
            tenant_id = UUID(str(resolved["tenant_id"]))
            connection_id = UUID(str(resolved["connection_id"]))
            if row["event_type"] == "device_added" and row["source_id"]:
                await self._inventory.sync_device(tenant_id, connection_id, row["source_id"])
            await self._apply(tenant_id, connection_id, row)
        except Exception as exc:
            category = exc.category if isinstance(exc, RingWebhookError) else "processing_failure"
            retryable = int(row["attempts"]) < 5
            await self._finish(
                inbox_id,
                "FAILED_RETRYABLE" if retryable else "FAILED_PERMANENT",
                category,
                datetime.now(UTC) + timedelta(seconds=min(60, 2 ** int(row["attempts"])))
                if retryable
                else None,
            )
            return True
        await self._finish(inbox_id, "PROCESSED", None)
        return True

    @staticmethod
    async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :value, true)"), {"value": str(tenant_id)}
        )

    async def _apply(self, tenant_id: UUID, connection_id: UUID, row: Any) -> None:
        secret: tuple[str, UUID] | None = None
        event_type = str(row["event_type"])
        if event_type not in _KNOWN_EVENTS:
            structlog.get_logger().warning("ring_webhook_unsupported_event")
        occurred = (
            datetime.fromtimestamp(int(row["event_timestamp_ms"]) / 1000, UTC)
            if row["event_timestamp_ms"] is not None
            else row["envelope_time"]
        )
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, tenant_id)
            connection = await session.scalar(
                select(CameraProviderConnection)
                .where(
                    CameraProviderConnection.id == connection_id,
                    CameraProviderConnection.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if connection is None:
                raise RingWebhookError("connection_missing")
            device = None
            if row["source_id"]:
                device = await session.scalar(
                    select(CameraProviderDevice)
                    .where(
                        CameraProviderDevice.tenant_id == tenant_id,
                        CameraProviderDevice.provider_connection_id == connection_id,
                        CameraProviderDevice.provider_device_id == row["source_id"],
                    )
                    .with_for_update()
                )
            existing_event = await session.scalar(
                select(ProviderEvent.id).where(
                    ProviderEvent.provider == "RING",
                    ProviderEvent.provider_request_id == row["request_id"],
                )
            )
            if existing_event is None:
                session.add(
                    ProviderEvent(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        provider="RING",
                        provider_connection_id=connection_id,
                        provider_device_record_id=device.id if device else None,
                        provider_request_id=row["request_id"],
                        provider_event_id=row["event_id"],
                        event_type=event_type,
                        provider_sub_type=row["sub_type"],
                        component_ids=list(row["component_ids"] or []),
                        provider_occurred_at=occurred,
                    )
                )
            if event_type in {"device_online", "device_offline"} and device is not None:
                if device.status_observed_at is None or occurred >= device.status_observed_at:
                    device.provider_online = event_type == "device_online"
                    device.status_observed_at = occurred
            elif event_type == "device_removed" and device is not None:
                await self._remove_device(session, tenant_id, device, occurred)
            elif event_type == "app_integration_removed":
                if connection.secret_ref and connection.credential_owner_id:
                    secret = (connection.secret_ref, connection.credential_owner_id)
                connection.integration_state = "DISCONNECTED"
                connection.status = "DISABLED"
                connection.operational_health = "REMOTE_REMOVED"
                connection.remote_removed_at = occurred
                connection.disconnected_at = occurred
                devices = (
                    await session.scalars(
                        select(CameraProviderDevice).where(
                            CameraProviderDevice.tenant_id == tenant_id,
                            CameraProviderDevice.provider_connection_id == connection_id,
                        )
                    )
                ).all()
                for value in devices:
                    await self._remove_device(session, tenant_id, value, occurred)
                session.add(
                    AuditEvent(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        actor_id=None,
                        action="integration.ring.remote_removed",
                        target_type="camera_provider_connection",
                        target_id=connection_id,
                        request_id=str(row["request_id"])[:128],
                        metadata_={"provider": "RING", "to_state": "REMOTE_REMOVED"},
                    )
                )
        if secret is not None:
            try:
                await self._vault.delete(secret[0], ring_credential_context(secret[1], tenant_id))
            except CredentialVaultError as exc:
                raise RingWebhookError("credential_delete_failed") from exc
            async with self._factory() as session, session.begin():
                await self._set_tenant(session, tenant_id)
                connection = await session.scalar(
                    select(CameraProviderConnection).where(
                        CameraProviderConnection.id == connection_id,
                        CameraProviderConnection.tenant_id == tenant_id,
                    )
                )
                if connection is not None and connection.operational_health == "REMOTE_REMOVED":
                    connection.secret_ref = None

    async def _remove_device(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        device: CameraProviderDevice,
        occurred: datetime,
    ) -> None:
        device.inventory_state = "REMOVED"
        device.removed_at = occurred
        components = (
            await session.scalars(
                select(CameraProviderComponent).where(
                    CameraProviderComponent.tenant_id == tenant_id,
                    CameraProviderComponent.provider_device_record_id == device.id,
                )
            )
        ).all()
        for component in components:
            component.inventory_state = "REMOVED"
            component.removed_at = occurred
            camera = await session.scalar(
                select(Camera).where(
                    Camera.id == component.camera_id, Camera.tenant_id == tenant_id
                )
            )
            if camera is not None:
                camera.status = "DISABLED"

    async def _finish(
        self,
        inbox_id: UUID,
        state: str,
        failure: str | None,
        retry_at: datetime | None = None,
    ) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                text("SELECT finish_ring_webhook(:id, :state, :failure, :retry_at)"),
                {"id": inbox_id, "state": state, "failure": failure, "retry_at": retry_at},
            )
