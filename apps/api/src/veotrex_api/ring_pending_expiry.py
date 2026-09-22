"""Expire abandoned Ring pending links and remove their credential material (V1-00A-PROD-R2).

Ring one-way linking stores a credential *before* any tenant is known: token receipt seals the
access and refresh token into ``encrypted_credentials`` and creates a ``ring_pending_links`` row
in ``RECEIVED``; ``UNCLAIMED`` follows once the Ring Account ID is known, and the browser claim
must arrive before ``access_expires_at``. The candidate query and ``start_ring_pending_claim``
both refuse a link once that instant has passed, so an expired ``RECEIVED``/``UNCLAIMED`` link
can never be claimed again - but nothing removed it. The docs required expired records to
"have vault material deleted and be archived" and deferred the operator command; production
accumulated expired rows, each still holding a sealed **refresh token** that Ring honours for
roughly thirty days. ``access_expires_at`` bounds the access token only.

This module is that command. It is a bounded, idempotent maintenance job and it runs with the
**admin/maintenance identity** (the same one the ``migrate`` and ``runtime-role`` jobs use),
never as the API runtime role:

* The runtime role holds no table privilege on ``encrypted_credentials`` or
  ``ring_pending_links`` (FUNCTION_ONLY), and the vault delete predicate authorises a
  pre-tenant delete only while a link is still ``RECEIVED``. An ``UNCLAIMED`` credential is
  therefore unreachable from the API by design, and widening the role to reach it would let a
  compromised API process delete pending credentials in bulk. Nothing here changes a grant.
* The safety guard "never delete a credential a ``camera_provider_connections`` row owns"
  must see every tenant's connections. A role Row Level Security filters would see none and
  conclude every credential is unowned, so the job refuses to run as any role RLS applies to.

Policy (state + age; the age condition is ``access_expires_at <= now()`` on the database clock,
the exact complement of the claim predicate ``access_expires_at > now()``):

==============================  =====================================================
``RECEIVED``, ``UNCLAIMED``     expired -> credential deleted, link ``ARCHIVED``
``FAILED``                      expired -> credential deleted, link ``ARCHIVED``
                                (definitive failure; the state machine allows only ->ARCHIVED)
``CLAIMING``                    never touched: an ownership transaction may be in flight and
                                a Ring App Integrations POST may already have been sent
``RING_CONFIRMATION_UNCERTAIN`` never touched: uncertain remote side effect; evidence
``RING_CONFIRMED_UNBOUND``      never touched: Ring confirmed, tenant binding pending
``CLAIMED``                     never a candidate: the credential belongs to the connection
``ARCHIVED``                    terminal; a credential still attached to one, and owned by no
                                connection, is an orphan and is removed
==============================  =====================================================

Expired rows in the three "never touched" states are counted and reported for operator
attention; ``apply`` exits with status 3 so a scheduler notices, but changes nothing about them.

Every ``apply`` is one database transaction: candidates are locked with
``FOR UPDATE SKIP LOCKED``, their credential rows are deleted, and the links are archived, or
none of it happens. Two workers cannot select the same row; a run that dies mid-way leaves the
database exactly as it was; and a claim that started first is in ``CLAIMING`` and excluded.
``dry-run`` executes in a ``READ ONLY`` transaction: PostgreSQL itself refuses any write.

Output is counts only. No identifier, reference, ciphertext, nonce, token or DSN is printed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg
from psycopg import sql

from veotrex_api.ring_repository import PendingLinkState
from veotrex_api.runtime_role import psycopg_dsn

PROVIDER = "RING"
OWNER_KIND = "ring_pending_link"

#: Expired links in these states are converged: credential deleted, link archived.
EXPIRABLE_STATES: tuple[str, ...] = (
    PendingLinkState.RECEIVED.value,
    PendingLinkState.UNCLAIMED.value,
    PendingLinkState.FAILED.value,
)
#: Expired links in these states are reported and never modified.
ATTENTION_STATES: tuple[str, ...] = (
    PendingLinkState.CLAIMING.value,
    PendingLinkState.RING_CONFIRMATION_UNCERTAIN.value,
    PendingLinkState.RING_CONFIRMED_UNBOUND.value,
)
#: Never examined for expiry at all.
EXCLUDED_STATES: tuple[str, ...] = (
    PendingLinkState.CLAIMED.value,
    PendingLinkState.ARCHIVED.value,
)

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "60s"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_ATTENTION = 3


class ExpiryRefused(RuntimeError):
    """The job will not run: wrong identity, unusable configuration, or a broken invariant."""


@dataclass(frozen=True, slots=True)
class ExpiryReport:
    """Bounded, non-identifying summary of one run. Safe to print and to ship to a log."""

    mode: str
    limit: int
    database_time: str
    #: Non-archived links in an examinable state whose access has expired.
    examined: int
    expired_by_state: dict[str, int] = field(default_factory=dict)
    #: Expired, expirable, consistent links selected within the limit.
    candidates: int = 0
    #: Links moved to ARCHIVED (apply) or that would be (dry-run).
    archived: int = 0
    #: Credential rows deleted for those links (apply) or that would be (dry-run).
    credentials_removed: int = 0
    #: Candidates whose credential row was already absent.
    already_clean: int = 0
    #: Credential rows still attached to an already-ARCHIVED link and owned by no connection.
    archived_orphans_removed: int = 0
    #: Expired links in CLAIMING / RING_CONFIRMATION_UNCERTAIN / RING_CONFIRMED_UNBOUND.
    skipped_by_state: int = 0
    #: Expired expirable links whose credential a connection references. Never modified.
    inconsistent: int = 0
    #: Expirable expired links beyond this run's limit; rerun to converge.
    remaining: int = 0

    @property
    def needs_attention(self) -> bool:
        return self.skipped_by_state > 0 or self.inconsistent > 0

    def as_dict(self) -> dict[str, Any]:
        rendered = asdict(self)
        rendered["needs_attention"] = self.needs_attention
        return rendered


# ------------------------------------------------------------------------------------ SQL
# Plain literals with typed parameters only; no caller input is ever interpolated. The one
# variable part - whether candidate rows are locked - is composed with psycopg.sql from two
# constant fragments.

_IDENTITY_SQL = (
    "SELECT r.rolname, r.rolsuper, r.rolbypassrls, "
    "has_table_privilege(current_user, 'public.encrypted_credentials', 'DELETE'), "
    "has_table_privilege(current_user, 'public.ring_pending_links', 'UPDATE'), "
    "has_table_privilege(current_user, 'public.camera_provider_connections', 'SELECT') "
    "FROM pg_roles r WHERE r.rolname = current_user"
)

_EXPIRED_BY_STATE_SQL = (
    "SELECT link.state, count(*) FROM public.ring_pending_links AS link "
    "WHERE link.archived_at IS NULL AND link.state = ANY(%(states)s) "
    "AND link.access_expires_at <= now() GROUP BY link.state"
)

_INCONSISTENT_SQL = (
    "SELECT count(*) FROM public.ring_pending_links AS link "
    "WHERE link.archived_at IS NULL AND link.state = ANY(%(states)s) "
    "AND link.access_expires_at <= now() "
    "AND EXISTS (SELECT 1 FROM public.camera_provider_connections AS connection "
    "            WHERE connection.credential_owner_id = link.id)"
)

_CANDIDATE_SQL = sql.SQL(
    "SELECT link.id, link.state, "
    "EXISTS (SELECT 1 FROM public.encrypted_credentials AS credential "
    "        WHERE credential.provider = %(provider)s AND credential.owner_kind = %(kind)s "
    "          AND credential.owner_id = link.id) "
    "FROM public.ring_pending_links AS link "
    "WHERE link.archived_at IS NULL AND link.state = ANY(%(states)s) "
    "AND link.access_expires_at <= now() "
    "AND NOT EXISTS (SELECT 1 FROM public.camera_provider_connections AS connection "
    "                WHERE connection.credential_owner_id = link.id) "
    "ORDER BY link.access_expires_at, link.id LIMIT %(limit)s {lock}"
)
_CANDIDATE_LOCK = sql.SQL("FOR UPDATE OF link SKIP LOCKED")

_DELETE_CREDENTIALS_SQL = (
    "DELETE FROM public.encrypted_credentials AS credential "
    "WHERE credential.provider = %(provider)s AND credential.owner_kind = %(kind)s "
    "AND credential.owner_id = ANY(%(ids)s) "
    "AND NOT EXISTS (SELECT 1 FROM public.camera_provider_connections AS connection "
    "                WHERE connection.credential_owner_id = credential.owner_id)"
)

_ARCHIVE_SQL = (
    "UPDATE public.ring_pending_links AS link "
    "SET state = %(archived)s, archived_at = now() "
    "WHERE link.id = ANY(%(ids)s) AND link.archived_at IS NULL "
    "AND link.state = ANY(%(states)s)"
)

_ORPHAN_SQL = sql.SQL(
    "SELECT credential.id FROM public.encrypted_credentials AS credential "
    "JOIN public.ring_pending_links AS link ON link.id = credential.owner_id "
    "WHERE credential.provider = %(provider)s AND credential.owner_kind = %(kind)s "
    "AND link.state = %(archived)s "
    "AND NOT EXISTS (SELECT 1 FROM public.camera_provider_connections AS connection "
    "                WHERE connection.credential_owner_id = credential.owner_id) "
    "ORDER BY credential.created_at, credential.id LIMIT %(limit)s {lock}"
)
_ORPHAN_LOCK = sql.SQL("FOR UPDATE OF credential SKIP LOCKED")

_DELETE_ORPHANS_SQL = (
    "DELETE FROM public.encrypted_credentials AS credential "
    "WHERE credential.id = ANY(%(ids)s) AND credential.provider = %(provider)s "
    "AND credential.owner_kind = %(kind)s "
    "AND NOT EXISTS (SELECT 1 FROM public.camera_provider_connections AS connection "
    "                WHERE connection.credential_owner_id = credential.owner_id)"
)


def _scalar_int(row: tuple[object, ...] | None, index: int = 0) -> int:
    if row is None or row[index] is None:
        return 0
    return int(str(row[index]))


def require_maintenance_identity(connection: psycopg.Connection[tuple[object, ...]]) -> str:
    """Refuse every identity the job is not designed for; return the role name otherwise.

    The connection-ownership guard reads ``camera_provider_connections``, which ``FORCE``s Row
    Level Security. A role the policy filters sees no connection at all and would conclude
    that every credential is unowned, so the job runs only as a role RLS does not apply to.
    The API runtime role is refused here, before any table is touched.
    """
    with connection.cursor() as cursor:
        cursor.execute(_IDENTITY_SQL)
        row = cursor.fetchone()
    connection.rollback()
    if row is None:
        raise ExpiryRefused("cannot determine the connected database role")
    role = str(row[0])
    if not (bool(row[1]) or bool(row[2])):
        raise ExpiryRefused(
            f"database role {role!r} is subject to Row Level Security; this maintenance job "
            "must run with the admin/migration identity, never the API runtime role"
        )
    if not (bool(row[3]) and bool(row[4]) and bool(row[5])):
        raise ExpiryRefused(
            f"database role {role!r} lacks the table privileges this maintenance job requires"
        )
    return role


def _examine(
    cursor: psycopg.Cursor[tuple[object, ...]], *, limit: int, lock: bool
) -> tuple[str, dict[str, int], int, list[tuple[object, str, bool]]]:
    cursor.execute("SELECT now()::text")
    database_time = str((cursor.fetchone() or ("",))[0])
    cursor.execute(_EXPIRED_BY_STATE_SQL, {"states": list(EXPIRABLE_STATES + ATTENTION_STATES)})
    expired_by_state = {str(state): _scalar_int((count,)) for state, count in cursor.fetchall()}
    cursor.execute(_INCONSISTENT_SQL, {"states": list(EXPIRABLE_STATES)})
    inconsistent = _scalar_int(cursor.fetchone())
    cursor.execute(
        _CANDIDATE_SQL.format(lock=_CANDIDATE_LOCK if lock else sql.SQL("")),
        {
            "provider": PROVIDER,
            "kind": OWNER_KIND,
            "states": list(EXPIRABLE_STATES),
            "limit": limit,
        },
    )
    candidates = [(row[0], str(row[1]), bool(row[2])) for row in cursor.fetchall()]
    return database_time, expired_by_state, inconsistent, candidates


def _orphans(cursor: psycopg.Cursor[tuple[object, ...]], *, limit: int, lock: bool) -> list[object]:
    cursor.execute(
        _ORPHAN_SQL.format(lock=_ORPHAN_LOCK if lock else sql.SQL("")),
        {
            "provider": PROVIDER,
            "kind": OWNER_KIND,
            "archived": PendingLinkState.ARCHIVED.value,
            "limit": limit,
        },
    )
    return [row[0] for row in cursor.fetchall()]


def _build_report(
    *,
    mode: str,
    limit: int,
    database_time: str,
    expired_by_state: dict[str, int],
    inconsistent: int,
    candidates: list[tuple[object, str, bool]],
    credentials_removed: int,
    archived: int,
    orphans_removed: int,
) -> ExpiryReport:
    expirable_expired = sum(expired_by_state.get(state, 0) for state in EXPIRABLE_STATES)
    skipped = sum(expired_by_state.get(state, 0) for state in ATTENTION_STATES)
    return ExpiryReport(
        mode=mode,
        limit=limit,
        database_time=database_time,
        examined=sum(expired_by_state.values()),
        expired_by_state=dict(sorted(expired_by_state.items())),
        candidates=len(candidates),
        archived=archived,
        credentials_removed=credentials_removed,
        already_clean=sum(1 for _, _, present in candidates if not present),
        archived_orphans_removed=orphans_removed,
        skipped_by_state=skipped,
        inconsistent=inconsistent,
        remaining=max(expirable_expired - inconsistent - len(candidates), 0),
    )


def dry_run(connection: psycopg.Connection[tuple[object, ...]], *, limit: int) -> ExpiryReport:
    """Report what ``apply`` would do. Runs in a READ ONLY transaction and takes no row lock."""
    _check_limit(limit)
    require_maintenance_identity(connection)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        database_time, by_state, inconsistent, candidates = _examine(
            cursor, limit=limit, lock=False
        )
        orphans = _orphans(cursor, limit=limit, lock=False)
    connection.rollback()
    return _build_report(
        mode="dry-run",
        limit=limit,
        database_time=database_time,
        expired_by_state=by_state,
        inconsistent=inconsistent,
        candidates=candidates,
        credentials_removed=sum(1 for _, _, present in candidates if present),
        archived=len(candidates),
        orphans_removed=len(orphans),
    )


def apply(connection: psycopg.Connection[tuple[object, ...]], *, limit: int) -> ExpiryReport:
    """Converge up to ``limit`` expired links in one transaction.

    Lock candidates (``SKIP LOCKED``), delete their credential rows, archive the links, remove
    credentials orphaned on already-archived links, commit. Any failure rolls back everything:
    there is no ordering in which a link is archived while its credential survives, or a
    credential is deleted while its link stays claimable.
    """
    _check_limit(limit)
    require_maintenance_identity(connection)
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        database_time, by_state, inconsistent, candidates = _examine(cursor, limit=limit, lock=True)
        ids = [identifier for identifier, _, _ in candidates]
        removed = 0
        archived = 0
        if ids:
            cursor.execute(
                _DELETE_CREDENTIALS_SQL, {"provider": PROVIDER, "kind": OWNER_KIND, "ids": ids}
            )
            removed = cursor.rowcount
            expected = sum(1 for _, _, present in candidates if present)
            if removed != expected:
                # A connection appeared for a locked pre-claim link, or a credential vanished
                # between examination and deletion. Neither can happen under the row lock; if
                # it did, leave the database untouched rather than reason about half of it.
                raise ExpiryRefused("credential ownership changed under lock; nothing applied")
            cursor.execute(
                _ARCHIVE_SQL,
                {
                    "archived": PendingLinkState.ARCHIVED.value,
                    "ids": ids,
                    "states": list(EXPIRABLE_STATES),
                },
            )
            archived = cursor.rowcount
            if archived != len(ids):
                raise ExpiryRefused("pending-link state changed under lock; nothing applied")
        orphans = _orphans(cursor, limit=limit, lock=True)
        orphans_removed = 0
        if orphans:
            cursor.execute(
                _DELETE_ORPHANS_SQL, {"ids": orphans, "provider": PROVIDER, "kind": OWNER_KIND}
            )
            orphans_removed = cursor.rowcount
            if orphans_removed != len(orphans):
                raise ExpiryRefused("orphan ownership changed under lock; nothing applied")
    return _build_report(
        mode="apply",
        limit=limit,
        database_time=database_time,
        expired_by_state=by_state,
        inconsistent=inconsistent,
        candidates=candidates,
        credentials_removed=removed,
        archived=archived,
        orphans_removed=orphans_removed,
    )


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ExpiryRefused(f"--limit must be between 1 and {MAX_LIMIT}")


# -------------------------------------------------------------------------------- console


def _resolve_url(reference: str | None) -> str:
    if reference:
        from veotrex_api.secrets import DefaultSecretResolver, SecretResolutionError

        try:
            return DefaultSecretResolver().resolve(reference).get_secret_value()
        except SecretResolutionError as exc:
            raise ExpiryRefused(f"database URL reference is unusable: {exc}") from None
    try:
        from veotrex_api.config import get_settings

        return get_settings().database_url.get_secret_value()
    except ValueError:
        raise ExpiryRefused("no database URL configured; pass --url-ref") from None


def render(report: ExpiryReport, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(report.as_dict(), indent=2, sort_keys=True)
    lines = [
        f"{key}={value}" for key, value in report.as_dict().items() if key != "expired_by_state"
    ]
    for state, count in report.expired_by_state.items():
        lines.append(f"expired[{state}]={count}")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="veotrex-ring-pending-expiry",
        description=(
            "Archive expired, abandoned Ring pending links and delete their credential "
            "material. Bounded, idempotent, one transaction. Connects with the ADMIN/"
            "maintenance identity; refuses the API runtime role. Prints counts only."
        ),
    )
    command.add_argument("action", choices=("dry-run", "apply"))
    command.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"maximum links (and orphaned credentials) per run, 1-{MAX_LIMIT}",
    )
    command.add_argument(
        "--url-ref",
        default=None,
        help=(
            "secret reference (env:NAME or file:/abs) to the admin DSN; defaults to the "
            "configured VEOTREX_DATABASE_URL / VEOTREX_DATABASE_URL_REF"
        ),
    )
    command.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        url = _resolve_url(arguments.url_ref)
        with psycopg.connect(psycopg_dsn(url), autocommit=False) as connection:
            if arguments.action == "apply":
                report = apply(connection, limit=arguments.limit)
            else:
                report = dry_run(connection, limit=arguments.limit)
    except ExpiryRefused as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    except psycopg.Error as exc:
        # SQLSTATE only: a driver message can carry row detail, and none is needed here.
        print(f"ERROR: database operation failed ({exc.sqlstate or 'unknown'})")
        return EXIT_ERROR
    print(render(report, as_json=arguments.json))
    return EXIT_ATTENTION if report.needs_attention else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
