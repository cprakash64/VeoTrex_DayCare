"""Classrooms and their configured ratio policies (V1-04A).

A classroom is an existing ``areas`` row with ``kind = 'CLASSROOM'``; nothing here introduces
a competing location table. Every operation:

* runs under the caller's tenant (RLS context plus explicit ``tenant_id`` predicates);
* is scoped to one facility: a principal reads a classroom only with READ_OPERATIONAL on its
  facility and changes it only with ADMINISTER_FACILITY there (tenant owners hold both);
* answers an unknown, other-tenant or unreadable-facility identifier identically (``not_found``)
  so identifiers cannot be probed;
* writes an audit event carrying numbers and labels only - never a person.

Ratio status is computed by the pure :mod:`veotrex_api.classroom_ratio` engine. Since V1-04B
the one connected presence source is MANUAL: an operator-reported, append-only, short-lived
aggregate snapshot (``classroom_presence_snapshots``). Storage and selection live here; the
arithmetic stays in the engine. Nothing here counts children from a camera, and nothing
substitutes vision occupancy for a missing or expired count.

Since V1-04C a classroom's ``presence_source_mode`` says explicitly where its qualified-staff
count comes from: the manual report (MANUAL_AGGREGATE, the default and the V1-04B behaviour) or
the operator check-in roster (ROSTER_STAFF_PLUS_MANUAL_CHILDREN). Children and visitors always
come from the manual report. The two staff counts are never combined
(:func:`veotrex_api.staff_presence.compose_presence`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.access import AuthenticatedPrincipal
from veotrex_api.authorization import Permission, has_permission
from veotrex_api.classroom_ratio import (
    Freshness,
    ManualPresenceRecord,
    PresenceAvailability,
    PresenceResolution,
    RatioEvaluation,
    RatioPolicyError,
    RatioPolicyTerms,
    VisionReconciliation,
    evaluate_ratio,
    find_overlap,
    freshness,
    reconcile_vision,
    resolve_manual_presence,
    select_policy,
    validate_effective_period,
    validate_manual_counts,
    validate_manual_validity,
    validate_policy_numbers,
)
from veotrex_api.models import (
    Area,
    AuditEvent,
    Camera,
    ClassroomPresenceSnapshot,
    ClassroomRatioPolicy,
    Facility,
    Zone,
)
from veotrex_api.staff_presence import CompositePresence, PresenceSourceMode, compose_presence
from veotrex_api.staff_roster_store import resolve_roster

CLASSROOM_KIND = "CLASSROOM"
MAX_CLASSROOMS_PER_FACILITY = 200
MAX_POLICIES_PER_CLASSROOM = 100
NAME_MAX = 200
POLICY_LABEL_MAX = 120
AGE_BAND_MAX = 64
SOURCE_REFERENCE_MAX = 500
# Operator text: printable, no markup delimiters, no control characters. Labels are free text by
# necessity; the UI tells operators never to put a child's name in them.
_AGE_BAND = re.compile(r"^[A-Za-z0-9 .,'()/+&_-]+$")
_FREE_TEXT = re.compile(r"^[^\x00-\x1f\x7f<>]+$")

VALIDATION_CATEGORIES = frozenset(
    {
        "invalid_classroom_name",
        "invalid_age_band_label",
        "invalid_policy_label",
        "invalid_source_reference",
        "invalid_max_children_per_staff",
        "invalid_minimum_staff",
        "invalid_maximum_group_size",
        "effective_period_inverted",
        "invalid_effective_date",
        "invalid_child_count",
        "invalid_qualified_staff_count",
        "invalid_visitor_count",
        "invalid_validity_seconds",
        "invalid_presence_source_mode",
    }
)
# How many past reports the presence endpoint returns. History is kept in full in the table;
# this only bounds one response.
PRESENCE_HISTORY_LIMIT = 20
CONFLICT_CATEGORIES = frozenset(
    {
        "classroom_name_exists",
        "classroom_limit_reached",
        "classroom_inactive",
        "facility_inactive",
        "facility_timezone_invalid",
        "policy_period_overlaps",
        "policy_limit_reached",
        "policy_inactive",
        # V1-04C: a roster-mode classroom takes its staff count from check-ins, never a number.
        "staff_count_comes_from_roster",
    }
)


class ClassroomError(Exception):
    def __init__(self, category: str) -> None:
        super().__init__(f"classroom operation failed: {category}")
        self.category = category


@dataclass(frozen=True, slots=True)
class FacilitySummary:
    facility_id: UUID
    name: str
    timezone: str
    status: str
    can_administer: bool


@dataclass(frozen=True, slots=True)
class CameraRef:
    """A camera associated with the classroom through one of its zones. No provider ids."""

    camera_id: UUID
    name: str
    status: str
    zone_name: str


@dataclass(frozen=True, slots=True)
class PolicySummary:
    policy_id: UUID
    label: str
    age_band_label: str | None
    max_children_per_staff: int
    minimum_staff: int
    maximum_group_size: int | None
    effective_from: datetime
    effective_until: datetime | None
    effective_from_date: date
    # Last local day the policy applies, inclusive; None means open-ended.
    effective_through_date: date | None
    status: str
    revision: int
    source_reference: str | None
    in_effect: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ClassroomSummary:
    classroom_id: UUID
    facility_id: UUID
    facility_name: str
    facility_timezone: str
    name: str
    status: str
    age_band_label: str | None
    cameras: tuple[CameraRef, ...]
    policies: tuple[PolicySummary, ...]
    current_policy_id: UUID | None
    can_administer: bool
    created_at: datetime
    updated_at: datetime
    presence_source_mode: str = str(PresenceSourceMode.MANUAL_AGGREGATE)


@dataclass(frozen=True, slots=True)
class RatioStatus:
    classroom_id: UUID
    evaluation: RatioEvaluation
    reconciliation: VisionReconciliation
    presence_connected: bool
    vision_connected: bool
    presence: PresenceResolution | None = None
    composite: CompositePresence | None = None

    def as_dict(self) -> dict[str, Any]:
        resolution = self.presence
        roster_mode = (
            self.composite is not None
            and self.composite.mode is PresenceSourceMode.ROSTER_STAFF_PLUS_MANUAL_CHILDREN
        )
        return {
            "classroom_id": str(self.classroom_id),
            "evaluation": self.evaluation.as_dict(),
            "reconciliation": self.reconciliation.as_dict(),
            "presence_connected": self.presence_connected,
            "vision_connected": self.vision_connected,
            "policy_basis": "CONFIGURED_CLASSROOM_POLICY",
            "presence": presence_dict(
                resolution, self.evaluation.evaluated_at, staff_from_roster=roster_mode
            ),
            "presence_source_mode": str(
                PresenceSourceMode.MANUAL_AGGREGATE
                if self.composite is None
                else self.composite.mode
            ),
            # Where each ratio input came from (V1-04C), so a reader never has to guess which
            # source produced a number.
            "sources": None if self.composite is None else self.composite.as_dict(),
        }


def presence_dict(
    resolution: PresenceResolution | None, now: datetime, *, staff_from_roster: bool = False
) -> dict[str, Any]:
    """The authoritative presence behind a status. Counts appear only while FRESH: a stale or
    revoked report is described, never shown as if it still held. In roster mode the manual
    report's staff number - if it has one, from before the switch - is not shown, because it is
    not the staff count in use."""
    if resolution is None or resolution.record is None:
        availability = (
            PresenceAvailability.PRESENCE_NOT_CONNECTED
            if resolution is None
            else resolution.availability
        )
        return {
            "availability": str(availability),
            "source": None,
            "snapshot_id": None,
            "observed_at": None,
            "valid_until": None,
            "freshness": str(Freshness.MISSING),
            "child_count": None,
            "qualified_staff_count": None,
            "visitor_count": None,
        }
    record = resolution.record
    fresh = resolution.availability is PresenceAvailability.PRESENCE_FRESH
    return {
        "availability": str(resolution.availability),
        "source": "MANUAL",
        "snapshot_id": str(record.snapshot_id),
        "observed_at": utc(record.observed_at).isoformat(),
        "valid_until": utc(record.valid_until).isoformat(),
        "freshness": str(Freshness.FRESH if fresh else Freshness.STALE),
        "child_count": record.child_count if fresh else None,
        "qualified_staff_count": record.qualified_staff_count
        if fresh and not staff_from_roster
        else None,
        "visitor_count": record.visitor_count if fresh else None,
    }


@dataclass(frozen=True, slots=True)
class ManualPresenceInput:
    child_count: int
    # Required in MANUAL_AGGREGATE mode, refused in roster mode (V1-04C).
    qualified_staff_count: int | None
    visitor_count: int
    valid_for_seconds: int


@dataclass(frozen=True, slots=True)
class PresenceReport:
    """One stored report as history shows it: counts, times, state. No person, no name."""

    snapshot_id: UUID
    source: str
    child_count: int
    qualified_staff_count: int | None
    visitor_count: int
    observed_at: datetime
    valid_until: datetime
    created_at: datetime
    revoked_at: datetime | None
    freshness: str
    authoritative: bool
    submitted_by_caller: bool


@dataclass(frozen=True, slots=True)
class ClassroomPresence:
    classroom_id: UUID
    availability: str
    current: PresenceReport | None
    history: tuple[PresenceReport, ...]


@dataclass(frozen=True, slots=True)
class PolicyInput:
    label: str
    age_band_label: str | None
    max_children_per_staff: int
    minimum_staff: int
    maximum_group_size: int | None
    effective_from_date: date
    effective_through_date: date | None
    source_reference: str | None


# ------------------------------------------------------------------------------ validation
def utc(value: datetime) -> datetime:
    """Every timestamp leaves this module in UTC, whatever the database session's timezone:
    a value read back from PostgreSQL must render exactly as the one just written."""
    return value.astimezone(UTC)


def clean_name(value: str, *, maximum: int = NAME_MAX, category: str) -> str:
    name = " ".join(str(value).split())
    if not name or len(name) > maximum or not _FREE_TEXT.match(name):
        raise ClassroomError(category)
    return name


def clean_optional(
    value: str | None, *, maximum: int, pattern: re.Pattern[str], category: str
) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    if not cleaned:
        return None
    if len(cleaned) > maximum or not pattern.match(cleaned):
        raise ClassroomError(category)
    return cleaned


def facility_zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ClassroomError("facility_timezone_invalid") from None


def local_period(
    start: date, through: date | None, zone: ZoneInfo
) -> tuple[datetime, datetime | None]:
    """Local calendar days in the facility's timezone -> a half-open UTC period.

    ``through`` is the last day the policy applies, so the period ends at the start of the
    following local day. Stored and compared in UTC; entered and shown in facility time.
    """
    if through is not None and through < start:
        raise ClassroomError("effective_period_inverted")
    try:
        begins = datetime.combine(start, time.min, tzinfo=zone).astimezone(UTC)
        ends = (
            None
            if through is None
            else datetime.combine(through + timedelta(days=1), time.min, tzinfo=zone).astimezone(
                UTC
            )
        )
    except (OverflowError, ValueError):
        raise ClassroomError("invalid_effective_date") from None
    return begins, ends


def validated_policy(payload: PolicyInput, zone: ZoneInfo) -> dict[str, Any]:
    label = clean_name(payload.label, maximum=POLICY_LABEL_MAX, category="invalid_policy_label")
    age_band = clean_optional(
        payload.age_band_label,
        maximum=AGE_BAND_MAX,
        pattern=_AGE_BAND,
        category="invalid_age_band_label",
    )
    source = clean_optional(
        payload.source_reference,
        maximum=SOURCE_REFERENCE_MAX,
        pattern=_FREE_TEXT,
        category="invalid_source_reference",
    )
    try:
        validate_policy_numbers(
            payload.max_children_per_staff, payload.minimum_staff, payload.maximum_group_size
        )
    except RatioPolicyError as exc:
        raise ClassroomError(exc.category) from None
    begins, ends = local_period(payload.effective_from_date, payload.effective_through_date, zone)
    try:
        validate_effective_period(begins, ends)
    except RatioPolicyError as exc:
        raise ClassroomError(exc.category) from None
    return {
        "label": label,
        "age_band_label": age_band,
        "max_children_per_staff": payload.max_children_per_staff,
        "minimum_staff": payload.minimum_staff,
        "maximum_group_size": payload.maximum_group_size,
        "effective_from": begins,
        "effective_until": ends,
        "source_reference": source,
    }


def policy_terms(row: ClassroomRatioPolicy) -> RatioPolicyTerms:
    return RatioPolicyTerms(
        policy_id=row.id,
        classroom_id=row.area_id,
        label=row.label,
        max_children_per_staff=row.max_children_per_staff,
        minimum_staff=row.minimum_staff,
        maximum_group_size=row.maximum_group_size,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        active=row.status == "ACTIVE",
        revision=row.revision,
        age_band_label=row.age_band_label,
    )


# -------------------------------------------------------------------------------- service
class ClassroomService:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._logger = structlog.get_logger()

    # ------------------------------------------------------------------------- helpers
    @staticmethod
    async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    @staticmethod
    def _can(principal: AuthenticatedPrincipal, permission: Permission, facility_id: UUID) -> bool:
        return has_permission(principal.grants, permission, facility_id)

    def _require_any(self, principal: AuthenticatedPrincipal, permission: Permission) -> None:
        if permission not in principal.permissions:
            raise ClassroomError("access_denied")

    async def _facility(
        self, session: AsyncSession, principal: AuthenticatedPrincipal, facility_id: UUID
    ) -> Facility:
        facility = await session.scalar(
            select(Facility).where(
                Facility.id == facility_id, Facility.tenant_id == principal.tenant_id
            )
        )
        if facility is None or not self._can(principal, Permission.READ_OPERATIONAL, facility.id):
            # Unknown, other-tenant and unreadable facilities are indistinguishable.
            raise ClassroomError("not_found")
        return facility

    async def _classroom(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        *,
        administer: bool = False,
    ) -> tuple[Area, Facility]:
        area = await session.scalar(
            select(Area).where(
                Area.id == classroom_id,
                Area.tenant_id == principal.tenant_id,
                Area.kind == CLASSROOM_KIND,
            )
        )
        if area is None:
            raise ClassroomError("not_found")
        facility = await self._facility(session, principal, area.facility_id)
        if administer and not self._can(principal, Permission.ADMINISTER_FACILITY, facility.id):
            # The caller can see this classroom, so saying "not found" would be a lie.
            raise ClassroomError("access_denied")
        return area, facility

    @staticmethod
    def _audit(
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        target_type: str,
        target_id: UUID,
        action: str,
        request_id: str,
        metadata: dict[str, Any],
    ) -> None:
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_id=principal.actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                request_id=request_id[:128],
                metadata_=metadata,
            )
        )

    @staticmethod
    async def _lock_classroom_policies(session: AsyncSession, classroom_id: UUID) -> None:
        """Serialise policy writes for one classroom, so two concurrent writers cannot both pass
        the overlap check. Transaction-scoped; released at commit or rollback."""
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"classroom_ratio_policy:{classroom_id}"},
        )

    async def _policies(
        self, session: AsyncSession, tenant_id: UUID, classroom_id: UUID
    ) -> list[ClassroomRatioPolicy]:
        return list(
            (
                await session.scalars(
                    select(ClassroomRatioPolicy)
                    .where(
                        ClassroomRatioPolicy.tenant_id == tenant_id,
                        ClassroomRatioPolicy.area_id == classroom_id,
                    )
                    .order_by(
                        ClassroomRatioPolicy.effective_from.desc(),
                        ClassroomRatioPolicy.revision.desc(),
                        ClassroomRatioPolicy.id,
                    )
                )
            ).all()
        )

    async def _cameras(
        self, session: AsyncSession, tenant_id: UUID, classroom_id: UUID
    ) -> tuple[CameraRef, ...]:
        rows = (
            await session.execute(
                select(Camera.id, Camera.name, Camera.status, Zone.name.label("zone_name"))
                .join(Zone, (Zone.id == Camera.zone_id) & (Zone.tenant_id == Camera.tenant_id))
                .where(
                    Camera.tenant_id == tenant_id,
                    Zone.tenant_id == tenant_id,
                    Zone.area_id == classroom_id,
                    Camera.status != "ARCHIVED",
                )
                .order_by(Camera.name, Camera.id)
            )
        ).all()
        return tuple(CameraRef(row.id, row.name, row.status, row.zone_name) for row in rows)

    @staticmethod
    def _policy_summary(row: ClassroomRatioPolicy, zone: ZoneInfo, now: datetime) -> PolicySummary:
        through = (
            None
            if row.effective_until is None
            else (row.effective_until.astimezone(zone) - timedelta(days=1)).date()
        )
        return PolicySummary(
            policy_id=row.id,
            label=row.label,
            age_band_label=row.age_band_label,
            max_children_per_staff=row.max_children_per_staff,
            minimum_staff=row.minimum_staff,
            maximum_group_size=row.maximum_group_size,
            effective_from=utc(row.effective_from),
            effective_until=None if row.effective_until is None else utc(row.effective_until),
            effective_from_date=row.effective_from.astimezone(zone).date(),
            effective_through_date=through,
            status=row.status,
            revision=row.revision,
            source_reference=row.source_reference,
            in_effect=policy_terms(row).applies_at(now),
            created_at=utc(row.created_at),
            updated_at=utc(row.updated_at),
        )

    async def _summary(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        area: Area,
        facility: Facility,
        now: datetime,
    ) -> ClassroomSummary:
        zone = facility_zone(facility.timezone)
        rows = await self._policies(session, principal.tenant_id, area.id)
        selection = select_policy([policy_terms(row) for row in rows], now)
        return ClassroomSummary(
            classroom_id=area.id,
            facility_id=facility.id,
            facility_name=facility.name,
            facility_timezone=facility.timezone,
            name=area.name,
            status=area.status,
            age_band_label=area.age_band_label,
            cameras=await self._cameras(session, principal.tenant_id, area.id),
            policies=tuple(self._policy_summary(row, zone, now) for row in rows),
            current_policy_id=None if selection.policy is None else selection.policy.policy_id,
            can_administer=self._can(principal, Permission.ADMINISTER_FACILITY, facility.id),
            created_at=utc(area.created_at),
            updated_at=utc(area.updated_at),
            presence_source_mode=area.presence_source_mode,
        )

    # ------------------------------------------------------------------------ facilities
    async def list_facilities(
        self, principal: AuthenticatedPrincipal
    ) -> tuple[FacilitySummary, ...]:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            rows = (
                await session.scalars(
                    select(Facility)
                    .where(Facility.tenant_id == principal.tenant_id)
                    .order_by(Facility.name, Facility.id)
                )
            ).all()
            return tuple(
                FacilitySummary(
                    row.id,
                    row.name,
                    row.timezone,
                    row.status,
                    self._can(principal, Permission.ADMINISTER_FACILITY, row.id),
                )
                for row in rows
                if self._can(principal, Permission.READ_OPERATIONAL, row.id)
            )

    # ------------------------------------------------------------------------ classrooms
    async def list_classrooms(
        self, principal: AuthenticatedPrincipal, *, now: datetime | None = None
    ) -> tuple[ClassroomSummary, ...]:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        moment = now or datetime.now(UTC)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            rows = (
                await session.execute(
                    select(Area, Facility)
                    .join(
                        Facility,
                        (Facility.id == Area.facility_id) & (Facility.tenant_id == Area.tenant_id),
                    )
                    .where(Area.tenant_id == principal.tenant_id, Area.kind == CLASSROOM_KIND)
                    .order_by(Facility.name, Area.name, Area.id)
                )
            ).all()
            return tuple(
                [
                    await self._summary(session, principal, area, facility, moment)
                    for area, facility in rows
                    if self._can(principal, Permission.READ_OPERATIONAL, facility.id)
                ]
            )

    async def get_classroom(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, *, now: datetime | None = None
    ) -> ClassroomSummary:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(session, principal, classroom_id)
            return await self._summary(session, principal, area, facility, now or datetime.now(UTC))

    async def create_classroom(
        self,
        principal: AuthenticatedPrincipal,
        facility_id: UUID,
        name: str,
        age_band_label: str | None,
        request_id: str,
    ) -> ClassroomSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        cleaned = clean_name(name, category="invalid_classroom_name")
        age_band = clean_optional(
            age_band_label,
            maximum=AGE_BAND_MAX,
            pattern=_AGE_BAND,
            category="invalid_age_band_label",
        )
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            facility = await self._facility(session, principal, facility_id)
            if not self._can(principal, Permission.ADMINISTER_FACILITY, facility.id):
                raise ClassroomError("access_denied")
            if facility.status != "ACTIVE":
                raise ClassroomError("facility_inactive")
            existing = await session.scalar(
                select(func.count())
                .select_from(Area)
                .where(
                    Area.tenant_id == principal.tenant_id,
                    Area.facility_id == facility.id,
                    Area.kind == CLASSROOM_KIND,
                )
            )
            if int(existing or 0) >= MAX_CLASSROOMS_PER_FACILITY:
                raise ClassroomError("classroom_limit_reached")
            area = Area(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=facility.id,
                name=cleaned,
                kind=CLASSROOM_KIND,
                status="ACTIVE",
                age_band_label=age_band,
            )
            session.add(area)
            try:
                await session.flush()
            except IntegrityError:
                raise ClassroomError("classroom_name_exists") from None
            self._audit(
                session,
                principal,
                "classroom",
                area.id,
                "classroom.created",
                request_id,
                {"facility_id": str(facility.id), "age_band_label": age_band},
            )
            await session.flush()
            await session.refresh(area)
            return await self._summary(session, principal, area, facility, datetime.now(UTC))

    async def update_classroom(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        request_id: str,
        *,
        name: str | None = None,
        age_band_label: str | None = None,
        set_age_band: bool = False,
    ) -> ClassroomSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        cleaned = None if name is None else clean_name(name, category="invalid_classroom_name")
        age_band = clean_optional(
            age_band_label,
            maximum=AGE_BAND_MAX,
            pattern=_AGE_BAND,
            category="invalid_age_band_label",
        )
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            changed: dict[str, Any] = {}
            if cleaned is not None and cleaned != area.name:
                area.name = cleaned
                changed["name"] = True
            if set_age_band and age_band != area.age_band_label:
                area.age_band_label = age_band
                changed["age_band_label"] = age_band
            if changed:
                try:
                    await session.flush()
                except IntegrityError:
                    raise ClassroomError("classroom_name_exists") from None
                self._audit(
                    session,
                    principal,
                    "classroom",
                    area.id,
                    "classroom.updated",
                    request_id,
                    changed,
                )
                await session.flush()
                await session.refresh(area)
            return await self._summary(session, principal, area, facility, datetime.now(UTC))

    async def set_classroom_active(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, active: bool, request_id: str
    ) -> ClassroomSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            target = "ACTIVE" if active else "ARCHIVED"
            if area.status != target:
                area.status = target
                self._audit(
                    session,
                    principal,
                    "classroom",
                    area.id,
                    "classroom.activated" if active else "classroom.deactivated",
                    request_id,
                    {"to_status": target},
                )
                await session.flush()
                await session.refresh(area)
            return await self._summary(session, principal, area, facility, datetime.now(UTC))

    async def set_presence_source_mode(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, mode: str, request_id: str
    ) -> ClassroomSummary:
        """Choose where the classroom's qualified-staff count comes from (V1-04C).

        Explicit and audited: nothing switches a classroom to the roster implicitly, and
        switching back to MANUAL_AGGREGATE does not borrow the roster's number - a roster-mode
        manual report carries no staff count, so the ratio reads STAFF_COUNT_MISSING until an
        operator reports one.
        """
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            target = PresenceSourceMode(mode)
        except ValueError:
            raise ClassroomError("invalid_presence_source_mode") from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            if area.presence_source_mode != target:
                before = area.presence_source_mode
                area.presence_source_mode = str(target)
                self._audit(
                    session,
                    principal,
                    "classroom",
                    area.id,
                    "classroom.presence_source_mode_changed",
                    request_id,
                    {"from": before, "to": str(target)},
                )
                await session.flush()
                await session.refresh(area)
            return await self._summary(session, principal, area, facility, datetime.now(UTC))

    # -------------------------------------------------------------------------- policies
    async def create_policy(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        payload: PolicyInput,
        request_id: str,
    ) -> ClassroomSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            if area.status != "ACTIVE":
                raise ClassroomError("classroom_inactive")
            values = validated_policy(payload, facility_zone(facility.timezone))
            await self._lock_classroom_policies(session, area.id)
            existing = await self._policies(session, principal.tenant_id, area.id)
            if len(existing) >= MAX_POLICIES_PER_CLASSROOM:
                raise ClassroomError("policy_limit_reached")
            row = ClassroomRatioPolicy(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                area_id=area.id,
                status="ACTIVE",
                revision=1,
                created_by_actor_id=principal.actor_id,
                **values,
            )
            if find_overlap(policy_terms(row), [policy_terms(other) for other in existing]):
                raise ClassroomError("policy_period_overlaps")
            session.add(row)
            await session.flush()
            self._audit(
                session,
                principal,
                "classroom_ratio_policy",
                row.id,
                "ratio_policy.created",
                request_id,
                self._numbers(row),
            )
            await session.flush()
            return await self._summary(session, principal, area, facility, datetime.now(UTC))

    async def update_policy(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        policy_id: UUID,
        payload: PolicyInput,
        request_id: str,
    ) -> ClassroomSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            if area.status != "ACTIVE":
                raise ClassroomError("classroom_inactive")
            values = validated_policy(payload, facility_zone(facility.timezone))
            await self._lock_classroom_policies(session, area.id)
            existing = await self._policies(session, principal.tenant_id, area.id)
            row = next((item for item in existing if item.id == policy_id), None)
            if row is None:
                raise ClassroomError("not_found")
            if row.status != "ACTIVE":
                raise ClassroomError("policy_inactive")
            before = self._numbers(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.revision += 1
            others = [policy_terms(item) for item in existing if item.id != row.id]
            if find_overlap(policy_terms(row), others):
                raise ClassroomError("policy_period_overlaps")
            await session.flush()
            self._audit(
                session,
                principal,
                "classroom_ratio_policy",
                row.id,
                "ratio_policy.updated",
                request_id,
                {"before": before, "after": self._numbers(row)},
            )
            await session.flush()
            return await self._summary(session, principal, area, facility, datetime.now(UTC))

    async def deactivate_policy(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        policy_id: UUID,
        request_id: str,
    ) -> ClassroomSummary:
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            await self._lock_classroom_policies(session, area.id)
            row = await session.scalar(
                select(ClassroomRatioPolicy).where(
                    ClassroomRatioPolicy.id == policy_id,
                    ClassroomRatioPolicy.tenant_id == principal.tenant_id,
                    ClassroomRatioPolicy.area_id == area.id,
                )
            )
            if row is None:
                raise ClassroomError("not_found")
            if row.status == "ACTIVE":
                row.status = "INACTIVE"
                row.revision += 1
                self._audit(
                    session,
                    principal,
                    "classroom_ratio_policy",
                    row.id,
                    "ratio_policy.deactivated",
                    request_id,
                    {"revision": row.revision},
                )
                await session.flush()
            return await self._summary(session, principal, area, facility, datetime.now(UTC))

    @staticmethod
    def _numbers(row: ClassroomRatioPolicy) -> dict[str, Any]:
        return {
            "max_children_per_staff": row.max_children_per_staff,
            "minimum_staff": row.minimum_staff,
            "maximum_group_size": row.maximum_group_size,
            "effective_from": row.effective_from.isoformat(),
            "effective_until": None
            if row.effective_until is None
            else row.effective_until.isoformat(),
            "revision": row.revision,
        }

    # ---------------------------------------------------------------------------- status
    # ------------------------------------------------------------------ manual presence
    @staticmethod
    def _presence_record(row: ClassroomPresenceSnapshot) -> ManualPresenceRecord:
        return ManualPresenceRecord(
            snapshot_id=row.id,
            classroom_id=row.area_id,
            child_count=row.child_count,
            qualified_staff_count=row.qualified_staff_count,
            visitor_count=row.visitor_count,
            observed_at=row.observed_at,
            valid_until=row.valid_until,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
        )

    @staticmethod
    async def _presence_rows(
        session: AsyncSession, tenant_id: UUID, classroom_id: UUID, limit: int
    ) -> list[ClassroomPresenceSnapshot]:
        """Newest first by the one authoritative ordering: observed_at, created_at, id - all
        descending (``ManualPresenceRecord.ordering_key``). The index serves exactly this."""
        return list(
            (
                await session.scalars(
                    select(ClassroomPresenceSnapshot)
                    .where(
                        ClassroomPresenceSnapshot.tenant_id == tenant_id,
                        ClassroomPresenceSnapshot.area_id == classroom_id,
                    )
                    .order_by(
                        ClassroomPresenceSnapshot.observed_at.desc(),
                        ClassroomPresenceSnapshot.created_at.desc(),
                        ClassroomPresenceSnapshot.id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        )

    async def _resolve_presence(
        self, session: AsyncSession, tenant_id: UUID, classroom_id: UUID, now: datetime
    ) -> PresenceResolution:
        latest = await self._presence_rows(session, tenant_id, classroom_id, 1)
        return resolve_manual_presence(
            [self._presence_record(row) for row in latest], classroom_id, now
        )

    def _report(
        self,
        row: ClassroomPresenceSnapshot,
        principal: AuthenticatedPrincipal,
        now: datetime,
        authoritative_id: UUID | None,
        *,
        latest: bool,
    ) -> PresenceReport:
        record = self._presence_record(row)
        if record.revoked:
            state = "REVOKED"
        elif not latest:
            # Perhaps not expired, but no longer the report that counts: supersession is
            # permanent, so an older row is never shown as if it were current.
            state = "SUPERSEDED"
        else:
            state = str(freshness(record.to_snapshot().children, now))
        return PresenceReport(
            snapshot_id=row.id,
            source=row.source,
            child_count=row.child_count,
            qualified_staff_count=row.qualified_staff_count,
            visitor_count=row.visitor_count,
            observed_at=utc(row.observed_at),
            valid_until=utc(row.valid_until),
            created_at=utc(row.created_at),
            revoked_at=None if row.revoked_at is None else utc(row.revoked_at),
            freshness=state,
            authoritative=row.id == authoritative_id,
            submitted_by_caller=row.submitted_by_actor_id == principal.actor_id,
        )

    async def _presence_summary(
        self,
        session: AsyncSession,
        principal: AuthenticatedPrincipal,
        area: Area,
        now: datetime,
    ) -> ClassroomPresence:
        rows = await self._presence_rows(
            session, principal.tenant_id, area.id, PRESENCE_HISTORY_LIMIT
        )
        resolution = resolve_manual_presence(
            [self._presence_record(row) for row in rows[:1]], area.id, now
        )
        authoritative = (
            resolution.record.snapshot_id
            if resolution.availability is PresenceAvailability.PRESENCE_FRESH
            and resolution.record is not None
            else None
        )
        history = tuple(
            self._report(row, principal, now, authoritative, latest=index == 0)
            for index, row in enumerate(rows)
        )
        return ClassroomPresence(
            classroom_id=area.id,
            availability=str(resolution.availability),
            current=history[0] if history else None,
            history=history,
        )

    async def get_presence(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, *, now: datetime | None = None
    ) -> ClassroomPresence:
        self._require_any(principal, Permission.READ_OPERATIONAL)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, _ = await self._classroom(session, principal, classroom_id)
            return await self._presence_summary(session, principal, area, now or datetime.now(UTC))

    async def submit_manual_presence(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        payload: ManualPresenceInput,
        request_id: str,
    ) -> ClassroomPresence:
        """Append one operator report. ``observed_at`` is the server's clock at submission: a
        client can neither backdate a count nor send one from the future."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        try:
            validate_manual_counts(
                payload.child_count, payload.qualified_staff_count, payload.visitor_count
            )
            validate_manual_validity(payload.valid_for_seconds)
        except RatioPolicyError as exc:
            raise ClassroomError(exc.category) from None
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(
                session, principal, classroom_id, administer=True
            )
            if area.status != "ACTIVE":
                raise ClassroomError("classroom_inactive")
            roster_mode = (
                area.presence_source_mode == PresenceSourceMode.ROSTER_STAFF_PLUS_MANUAL_CHILDREN
            )
            if roster_mode and payload.qualified_staff_count is not None:
                # Accepting it would store a second staff number beside the roster's.
                raise ClassroomError("staff_count_comes_from_roster")
            if not roster_mode and payload.qualified_staff_count is None:
                raise ClassroomError("invalid_qualified_staff_count")
            observed = datetime.now(UTC)
            row = ClassroomPresenceSnapshot(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                facility_id=facility.id,
                area_id=area.id,
                child_count=payload.child_count,
                qualified_staff_count=payload.qualified_staff_count,
                visitor_count=payload.visitor_count,
                source="MANUAL",
                observed_at=observed,
                valid_until=observed + timedelta(seconds=payload.valid_for_seconds),
                submitted_by_actor_id=principal.actor_id,
            )
            session.add(row)
            await session.flush()
            self._audit(
                session,
                principal,
                "classroom_presence_snapshot",
                row.id,
                "presence.manual_submitted",
                request_id,
                {
                    "classroom_id": str(area.id),
                    "source": "MANUAL",
                    "child_count": payload.child_count,
                    "qualified_staff_count": payload.qualified_staff_count,
                    "visitor_count": payload.visitor_count,
                    "valid_for_seconds": payload.valid_for_seconds,
                },
            )
            await session.flush()
            return await self._presence_summary(session, principal, area, datetime.now(UTC))

    async def revoke_manual_presence(
        self,
        principal: AuthenticatedPrincipal,
        classroom_id: UUID,
        snapshot_id: UUID,
        request_id: str,
    ) -> ClassroomPresence:
        """Withdraw one report. Idempotent: a second revoke changes nothing and is not audited
        again, and the first revoker and time are kept."""
        self._require_any(principal, Permission.ADMINISTER_FACILITY)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, _ = await self._classroom(session, principal, classroom_id, administer=True)
            row = await session.scalar(
                select(ClassroomPresenceSnapshot)
                .where(
                    ClassroomPresenceSnapshot.id == snapshot_id,
                    ClassroomPresenceSnapshot.tenant_id == principal.tenant_id,
                    ClassroomPresenceSnapshot.area_id == area.id,
                )
                .with_for_update()
            )
            if row is None:
                raise ClassroomError("not_found")
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                row.revoked_by_actor_id = principal.actor_id
                await session.flush()
                self._audit(
                    session,
                    principal,
                    "classroom_presence_snapshot",
                    row.id,
                    "presence.manual_revoked",
                    request_id,
                    {"classroom_id": str(area.id), "source": "MANUAL"},
                )
                await session.flush()
            return await self._presence_summary(session, principal, area, datetime.now(UTC))

    # ---------------------------------------------------------------------------- status
    async def ratio_status(
        self, principal: AuthenticatedPrincipal, classroom_id: UUID, *, now: datetime | None = None
    ) -> RatioStatus:
        """policy in force -> latest manual report (+ the staff roster in roster mode) -> the
        composite snapshot -> the pure engine.

        Missing, stale or revoked presence is INSUFFICIENT_DATA with the reason; no earlier
        report and no camera count ever stands in for it. Vision is not connected here, so the
        reconciliation diagnostic reports NOT_AVAILABLE.
        """
        self._require_any(principal, Permission.READ_OPERATIONAL)
        moment = now or datetime.now(UTC)
        async with self._factory() as session, session.begin():
            await self._set_tenant(session, principal.tenant_id)
            area, facility = await self._classroom(session, principal, classroom_id)
            rows = await self._policies(session, principal.tenant_id, area.id)
            resolution = await self._resolve_presence(session, principal.tenant_id, area.id, moment)
            mode = PresenceSourceMode(area.presence_source_mode)
            roster = (
                await resolve_roster(session, principal.tenant_id, facility.id, area.id, moment)
                if mode is PresenceSourceMode.ROSTER_STAFF_PLUS_MANUAL_CHILDREN
                else None
            )
        selection = select_policy([policy_terms(row) for row in rows], moment)
        composite = compose_presence(area.id, mode, resolution, roster, moment)
        fresh = (
            composite.snapshot
            if resolution.availability is PresenceAvailability.PRESENCE_FRESH
            else None
        )
        return RatioStatus(
            classroom_id=area.id,
            evaluation=evaluate_ratio(
                selection.policy,
                # A stale report goes in as-is so the engine itself reports it stale; a revoked
                # one has no snapshot and reads as missing.
                composite.snapshot,
                moment,
                classroom_active=area.status == "ACTIVE",
                policy_ambiguous=selection.ambiguous,
            ),
            reconciliation=reconcile_vision(fresh, None, moment),
            presence_connected=resolution.connected,
            vision_connected=False,
            presence=resolution,
            composite=composite,
        )
