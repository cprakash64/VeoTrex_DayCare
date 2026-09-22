"""V1-00A-PROD-R2: expiry of abandoned Ring pending links.

Production-equivalent rows (two expired ``RECEIVED`` with a failure category, one expired
``UNCLAIMED``, one sealed credential each, no provider connection) are shown to be invisible to
the claim path and untouched by the existing code, then converged by the janitor to exactly the
intended end state. Every state the janitor must never modify is seeded and proven unchanged,
including credentials a real connection owns and credentials of unrelated providers and owner
kinds. Concurrency, the ``access_expires_at`` boundary, the runtime-role refusal and the
counts-only output contract are covered as well.

``admin_settings`` is the maintenance identity the janitor is designed for. ``settings`` is
the restricted API runtime role, which must be refused. Credential rows are synthetic random
bytes; no Ring value of any kind appears here.
"""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from veotrex_api.config import Settings
from veotrex_api.ring_pending_expiry import (
    ATTENTION_STATES,
    DEFAULT_LIMIT,
    EXCLUDED_STATES,
    EXIT_ATTENTION,
    EXIT_OK,
    EXIT_REFUSED,
    EXPIRABLE_STATES,
    MAX_LIMIT,
    ExpiryRefused,
    ExpiryReport,
    apply,
    dry_run,
    parser,
    render,
)
from veotrex_api.ring_pending_expiry import main as expiry_main
from veotrex_api.ring_repository import PendingLinkState
from veotrex_api.runtime_role import psycopg_dsn

Connection = psycopg.Connection[tuple[object, ...]]


# ------------------------------------------------------------------------------- fixtures


def _connect(settings: Settings) -> Connection:
    return psycopg.connect(psycopg_dsn(settings.database_url.get_secret_value()), autocommit=False)


def _converge(admin: Connection) -> ExpiryReport:
    """Drain whatever earlier tests left behind so the assertions below can be exact."""
    report = apply(admin, limit=MAX_LIMIT)
    while report.candidates or report.archived_orphans_removed:
        report = apply(admin, limit=MAX_LIMIT)
    return dry_run(admin, limit=MAX_LIMIT)


def _tenant_and_actor(admin: Connection) -> tuple[UUID, UUID]:
    tenant_id, actor_id = uuid4(), uuid4()
    with admin.cursor() as cursor:
        cursor.execute(
            "INSERT INTO tenants (id, name, status) VALUES (%s, %s, 'ACTIVE')",
            (tenant_id, f"Expiry tenant {tenant_id.hex[:8]}"),
        )
        cursor.execute(
            "INSERT INTO actors (id, tenant_id, display_name, status) "
            "VALUES (%s, %s, 'Owner', 'ACTIVE')",
            (actor_id, tenant_id),
        )
    admin.commit()
    return tenant_id, actor_id


def _seed_link(
    admin: Connection,
    *,
    state: str,
    expires_in: timedelta,
    account: str | None = None,
    failure: str | None = None,
    claim: tuple[UUID, UUID] | None = None,
    credential: bool = True,
    archived: bool = False,
) -> tuple[UUID, UUID]:
    """One pending link plus (by default) one synthetic sealed credential bound to it."""
    link_id, credential_id = uuid4(), uuid4()
    tenant_id, actor_id = claim if claim else (None, None)
    with admin.cursor() as cursor:
        cursor.execute(
            "INSERT INTO ring_pending_links (id, credential_secret_ref, credential_generation, "
            "received_at, access_expires_at, state, ring_account_id, last_failure_category, "
            "claim_tenant_id, claim_actor_id, claim_started_at, claimed_at, archived_at) VALUES "
            "(%s, %s, 1, now() - interval '5 hours', now() + %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s::uuid IS NULL THEN NULL ELSE now() - interval '4 hours' END, "
            "CASE WHEN %s = 'CLAIMED' THEN now() - interval '4 hours' END, "
            "CASE WHEN %s THEN now() - interval '1 hour' END)",
            (
                link_id,
                f"vault://postgres/{credential_id}",
                expires_in,
                state,
                account,
                failure,
                tenant_id,
                actor_id,
                tenant_id,
                state,
                archived,
            ),
        )
        if credential:
            _seed_credential(cursor, credential_id, owner_id=link_id)
    admin.commit()
    return link_id, credential_id


def _seed_credential(
    cursor: psycopg.Cursor[tuple[object, ...]],
    credential_id: UUID,
    *,
    owner_id: UUID,
    provider: str = "RING",
    owner_kind: str = "ring_pending_link",
) -> None:
    cursor.execute(
        "INSERT INTO encrypted_credentials (id, provider, owner_kind, owner_id, version, "
        "schema_version, nonce, ciphertext) VALUES (%s, %s, %s, %s, 1, 1, %s, %s)",
        (credential_id, provider, owner_kind, owner_id, os.urandom(12), os.urandom(32)),
    )


def _seed_connection(admin: Connection, tenant_id: UUID, owner_id: UUID) -> UUID:
    connection_id = uuid4()
    with admin.cursor() as cursor:
        cursor.execute(
            "INSERT INTO camera_provider_connections (id, tenant_id, name, provider_type, "
            "status, integration_state, credential_owner_id) VALUES "
            "(%s, %s, %s, 'RING', 'ACTIVE', 'ACTIVE', %s)",
            (connection_id, tenant_id, f"Ring {connection_id.hex[:8]}", owner_id),
        )
    admin.commit()
    return connection_id


def _link(admin: Connection, link_id: UUID) -> tuple[object, ...]:
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT state, archived_at, received_at, last_failure_category, ring_account_id, "
            "credential_secret_ref FROM ring_pending_links WHERE id = %s",
            (link_id,),
        )
        row = cursor.fetchone()
    admin.rollback()
    assert row is not None
    return row


def _credential_present(admin: Connection, credential_id: UUID) -> bool:
    with admin.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM encrypted_credentials WHERE id = %s", (credential_id,))
        count = int(str((cursor.fetchone() or (0,))[0]))
    admin.rollback()
    return count == 1


def _connections_referencing(admin: Connection, owner_ids: list[UUID]) -> int:
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM camera_provider_connections WHERE credential_owner_id = ANY(%s)",
            (owner_ids,),
        )
        count = int(str((cursor.fetchone() or (0,))[0]))
    admin.rollback()
    return count


EXPIRED = timedelta(hours=-1)
LIVE = timedelta(hours=1)


# --------------------------------------------------------------------------------- policy


def test_policy_partitions_every_pending_state_exactly_once() -> None:
    everything = EXPIRABLE_STATES + ATTENTION_STATES + EXCLUDED_STATES
    assert sorted(everything) == sorted(state.value for state in PendingLinkState)
    assert len(set(everything)) == len(everything)
    # The uncertain and in-flight states are never expirable, whatever the timestamp says.
    for state in ("CLAIMING", "RING_CONFIRMATION_UNCERTAIN", "RING_CONFIRMED_UNBOUND"):
        assert state in ATTENTION_STATES
    assert "CLAIMED" in EXCLUDED_STATES and "ARCHIVED" in EXCLUDED_STATES


def test_parser_defaults_to_a_bounded_dry_run() -> None:
    arguments = parser().parse_args(["dry-run"])
    assert arguments.action == "dry-run"
    assert arguments.limit == DEFAULT_LIMIT
    assert arguments.url_ref is None and arguments.json is False
    with pytest.raises(SystemExit):
        parser().parse_args(["purge"])


# ------------------------------------------------------------- production-equivalent rows


def test_production_equivalent_expired_rows_are_stale_until_the_janitor_converges_them(
    admin_settings: Settings,
) -> None:
    with _connect(admin_settings) as admin:
        baseline = _converge(admin)
        received_a, credential_a = _seed_link(
            admin, state="RECEIVED", expires_in=EXPIRED, failure="fixture_lookup_failed"
        )
        received_b, credential_b = _seed_link(
            admin, state="RECEIVED", expires_in=EXPIRED, failure="fixture_lookup_timeout"
        )
        unclaimed, credential_c = _seed_link(
            admin, state="UNCLAIMED", expires_in=EXPIRED, account=f"acct-{uuid4().hex}"
        )
        links = [received_a, received_b, unclaimed]
        credentials = [credential_a, credential_b, credential_c]
        assert _connections_referencing(admin, links) == 0
        before = {link: _link(admin, link) for link in links}

        # The existing code leaves them stale: invisible to the candidate query, refused by
        # the atomic claim, and no code path archives them or removes the credential.
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM list_ring_pending_candidates(now() - interval '30 days') "
                "WHERE id = ANY(%s)",
                (links,),
            )
            assert int(str((cursor.fetchone() or (0,))[0])) == 0
            cursor.execute(
                "SELECT start_ring_pending_claim(%s, %s, %s)", (unclaimed, uuid4(), uuid4())
            )
            assert (cursor.fetchone() or (None,))[0] is False
        admin.rollback()
        assert all(_credential_present(admin, credential) for credential in credentials)

        # Dry run: exact candidate counts, and nothing changes.
        preview = dry_run(admin, limit=100)
        assert preview.mode == "dry-run"
        assert preview.candidates == 3 and preview.archived == 3
        assert preview.credentials_removed == 3 and preview.already_clean == 0
        assert preview.examined - baseline.examined == 3
        assert (
            preview.expired_by_state.get("RECEIVED", 0)
            - baseline.expired_by_state.get("RECEIVED", 0)
            == 2
        )
        assert (
            preview.expired_by_state.get("UNCLAIMED", 0)
            - baseline.expired_by_state.get("UNCLAIMED", 0)
            == 1
        )
        assert preview.remaining == 0 and preview.archived_orphans_removed == 0
        assert preview.inconsistent == baseline.inconsistent
        assert {link: _link(admin, link) for link in links} == before
        assert all(_credential_present(admin, credential) for credential in credentials)

        # Apply: one transaction, exact end state.
        result = apply(admin, limit=100)
        assert result.mode == "apply"
        assert result.archived == 3 and result.credentials_removed == 3
        assert result.already_clean == 0 and result.archived_orphans_removed == 0
        assert result.remaining == 0 and result.inconsistent == baseline.inconsistent
        for link in links:
            state, archived_at, received_at, failure, account, secret_ref = _link(admin, link)
            assert state == "ARCHIVED" and archived_at is not None
            # Non-secret lifecycle evidence is preserved exactly.
            assert received_at == before[link][2]
            assert failure == before[link][3]
            assert account == before[link][4]
            assert secret_ref == before[link][5]
        assert not any(_credential_present(admin, credential) for credential in credentials)

        # Idempotent: a second run finds nothing to do.
        again = apply(admin, limit=100)
        assert again.candidates == 0 and again.archived == 0
        assert again.credentials_removed == 0 and again.archived_orphans_removed == 0
        assert again.examined == baseline.examined


# ------------------------------------------------------------------------- negative safety


def test_every_protected_state_and_unrelated_credential_is_preserved(
    admin_settings: Settings,
) -> None:
    with _connect(admin_settings) as admin:
        baseline = _converge(admin)
        tenant_id, actor_id = _tenant_and_actor(admin)
        claim = (tenant_id, actor_id)
        seeded = {
            "received_live": _seed_link(
                admin, state="RECEIVED", expires_in=LIVE, failure="fixture_lookup_failed"
            ),
            "unclaimed_live": _seed_link(
                admin, state="UNCLAIMED", expires_in=LIVE, account=f"acct-{uuid4().hex}"
            ),
            "claiming_expired": _seed_link(
                admin,
                state="CLAIMING",
                expires_in=EXPIRED,
                account=f"acct-{uuid4().hex}",
                claim=claim,
            ),
            "uncertain_expired": _seed_link(
                admin,
                state="RING_CONFIRMATION_UNCERTAIN",
                expires_in=EXPIRED,
                account=f"acct-{uuid4().hex}",
                claim=claim,
                failure="fixture_lookup_timeout",
            ),
            "unbound_expired": _seed_link(
                admin,
                state="RING_CONFIRMED_UNBOUND",
                expires_in=EXPIRED,
                account=f"acct-{uuid4().hex}",
                claim=claim,
                failure="binding_conflict",
            ),
            "claimed_expired": _seed_link(
                admin,
                state="CLAIMED",
                expires_in=EXPIRED,
                account=f"acct-{uuid4().hex}",
                claim=claim,
            ),
        }
        # The CLAIMED link's credential now belongs to a real, ACTIVE tenant connection.
        _seed_connection(admin, tenant_id, seeded["claimed_expired"][0])
        # Unrelated provider, and unrelated owner kind, bound to an otherwise-expirable link.
        other_provider, other_kind = uuid4(), uuid4()
        expired_received, _ = _seed_link(
            admin, state="RECEIVED", expires_in=EXPIRED, credential=False
        )
        with admin.cursor() as cursor:
            _seed_credential(cursor, other_provider, owner_id=uuid4(), provider="OTHER")
            _seed_credential(
                cursor, other_kind, owner_id=expired_received, owner_kind="camera_provider_link"
            )
        admin.commit()
        before = {name: _link(admin, link) for name, (link, _) in seeded.items()}

        result = apply(admin, limit=100)

        # Only the bare expired RECEIVED link (no RING credential) was archived; everything
        # else is byte-for-byte as seeded.
        assert result.archived == 1 and result.already_clean == 1
        assert result.credentials_removed == 0 and result.archived_orphans_removed == 0
        assert _link(admin, expired_received)[0] == "ARCHIVED"
        assert {name: _link(admin, link) for name, (link, _) in seeded.items()} == before
        for _, credential in seeded.values():
            assert _credential_present(admin, credential)
        assert _credential_present(admin, other_provider)
        assert _credential_present(admin, other_kind)

        # The three protected expired states are reported, not modified.
        assert result.skipped_by_state - baseline.skipped_by_state == 3
        assert result.needs_attention
        for state in ATTENTION_STATES:
            assert (
                result.expired_by_state.get(state, 0) - baseline.expired_by_state.get(state, 0) == 1
            )
        # CLAIMED is not examined at all, expired timestamp or not.
        assert "CLAIMED" not in result.expired_by_state
        assert "ARCHIVED" not in result.expired_by_state


def test_a_credential_owned_by_a_connection_is_never_deleted_even_from_an_expirable_link(
    admin_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-claim link whose credential a connection references cannot arise from the state
    machine. If it ever does, the janitor reports it and touches neither row."""
    with _connect(admin_settings) as admin:
        baseline = _converge(admin)
        tenant_id, _ = _tenant_and_actor(admin)
        link, credential = _seed_link(
            admin, state="UNCLAIMED", expires_in=EXPIRED, account=f"acct-{uuid4().hex}"
        )
        _seed_connection(admin, tenant_id, link)
        before = _link(admin, link)

        result = apply(admin, limit=100)
        assert result.inconsistent - baseline.inconsistent == 1
        assert result.candidates == 0 and result.archived == 0
        assert result.credentials_removed == 0
        assert result.needs_attention
        assert _link(admin, link) == before
        assert _credential_present(admin, credential)

        monkeypatch.setenv(
            "VEOTREX_EXPIRY_TEST_ADMIN_URL", admin_settings.database_url.get_secret_value()
        )
        assert (
            expiry_main(["apply", "--url-ref", "env:VEOTREX_EXPIRY_TEST_ADMIN_URL"])
            == EXIT_ATTENTION
        )
        assert "inconsistent=" in capsys.readouterr().out
        assert _credential_present(admin, credential)


def test_credentials_orphaned_on_archived_links_are_removed_unless_a_connection_owns_them(
    admin_settings: Settings,
) -> None:
    with _connect(admin_settings) as admin:
        _converge(admin)
        tenant_id, actor_id = _tenant_and_actor(admin)
        orphan_link, orphan_credential = _seed_link(
            admin, state="ARCHIVED", expires_in=EXPIRED, archived=True
        )
        owned_link, owned_credential = _seed_link(
            admin,
            state="ARCHIVED",
            expires_in=EXPIRED,
            account=f"acct-{uuid4().hex}",
            claim=(tenant_id, actor_id),
            archived=True,
        )
        _seed_connection(admin, tenant_id, owned_link)
        before = (_link(admin, orphan_link), _link(admin, owned_link))

        preview = dry_run(admin, limit=100)
        assert preview.archived_orphans_removed == 1 and preview.candidates == 0
        assert _credential_present(admin, orphan_credential)

        result = apply(admin, limit=100)
        assert result.archived_orphans_removed == 1
        assert result.archived == 0 and result.credentials_removed == 0
        assert not _credential_present(admin, orphan_credential)
        assert _credential_present(admin, owned_credential)
        assert (_link(admin, orphan_link), _link(admin, owned_link)) == before


# -------------------------------------------------------------------------- boundary time


def test_expiry_predicate_is_the_exact_complement_of_the_claim_predicate(
    admin_settings: Settings,
) -> None:
    with _connect(admin_settings) as admin:
        _converge(admin)
        soon, credential = _seed_link(
            admin,
            state="UNCLAIMED",
            expires_in=timedelta(seconds=2),
            account=f"acct-{uuid4().hex}",
        )
        # Still claimable: the candidate query lists it and the janitor leaves it alone.
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM list_ring_pending_candidates(now() - interval '1 day') "
                "WHERE id = %s",
                (soon,),
            )
            assert int(str((cursor.fetchone() or (0,))[0])) == 1
        admin.rollback()
        early = apply(admin, limit=100)
        assert early.candidates == 0 and _link(admin, soon)[0] == "UNCLAIMED"
        assert _credential_present(admin, credential)

        time.sleep(2.5)
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM list_ring_pending_candidates(now() - interval '1 day') "
                "WHERE id = %s",
                (soon,),
            )
            assert int(str((cursor.fetchone() or (0,))[0])) == 0
        admin.rollback()
        late = apply(admin, limit=100)
        assert late.candidates == 1 and late.archived == 1 and late.credentials_removed == 1
        assert _link(admin, soon)[0] == "ARCHIVED"
        assert not _credential_present(admin, credential)

        # At the instant of equality PostgreSQL evaluates both predicates on the same clock:
        # the link is expired for the janitor and, in the same breath, refused to a claim.
        with admin.cursor() as cursor:
            cursor.execute(
                "WITH link AS (SELECT now() AS access_expires_at) "
                "SELECT access_expires_at <= now(), access_expires_at > now() FROM link"
            )
            assert cursor.fetchone() == (True, False)
        admin.rollback()


# ----------------------------------------------------------------------------- concurrency


def test_two_workers_never_take_the_same_row_and_the_next_pass_converges(
    admin_settings: Settings,
) -> None:
    with _connect(admin_settings) as first, _connect(admin_settings) as second:
        _converge(first)
        held, held_credential = _seed_link(
            first, state="UNCLAIMED", expires_in=EXPIRED, account=f"acct-{uuid4().hex}"
        )
        free, free_credential = _seed_link(
            first, state="RECEIVED", expires_in=EXPIRED, failure="fixture_lookup_failed"
        )
        # Worker one is mid-transaction on the first row (as an apply that has selected it).
        with first.cursor() as cursor:
            cursor.execute("SELECT id FROM ring_pending_links WHERE id = %s FOR UPDATE", (held,))
            assert cursor.fetchone() is not None
        try:
            report = apply(second, limit=100)
            assert report.candidates == 1 and report.archived == 1
            assert report.credentials_removed == 1
            assert report.remaining == 1, "the locked row is still expired and still pending"
            assert _link(second, free)[0] == "ARCHIVED"
            assert not _credential_present(second, free_credential)
            assert _link(second, held)[0] == "UNCLAIMED"
            assert _credential_present(second, held_credential)
        finally:
            first.rollback()
        follow_up = apply(second, limit=100)
        assert follow_up.archived == 1 and follow_up.credentials_removed == 1
        assert _link(second, held)[0] == "ARCHIVED"
        assert not _credential_present(second, held_credential)


def test_a_claim_that_started_first_never_loses_its_credential(admin_settings: Settings) -> None:
    """The claim moves UNCLAIMED -> CLAIMING atomically before it opens the credential. Once in
    CLAIMING the link is outside the janitor's reach however long the claim takes."""
    with _connect(admin_settings) as admin:
        baseline = _converge(admin)
        tenant_id, actor_id = _tenant_and_actor(admin)
        link, credential = _seed_link(
            admin, state="UNCLAIMED", expires_in=LIVE, account=f"acct-{uuid4().hex}"
        )
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT start_ring_pending_claim(%s, %s, %s)", (link, tenant_id, actor_id)
            )
            assert (cursor.fetchone() or (None,))[0] is True
            # Time passes while the claim talks to Ring; the access expiry elapses.
            cursor.execute(
                "UPDATE ring_pending_links SET access_expires_at = now() - interval '1 second' "
                "WHERE id = %s",
                (link,),
            )
        admin.commit()

        result = apply(admin, limit=100)
        assert result.archived == 0 and result.credentials_removed == 0
        assert result.skipped_by_state - baseline.skipped_by_state == 1
        assert _link(admin, link)[0] == "CLAIMING"
        assert _credential_present(admin, credential)
        # The claim still completes through the existing state machine.
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT transition_ring_pending_link(%s, 'CLAIMING', 'CLAIMED', NULL)", (link,)
            )
            assert (cursor.fetchone() or (None,))[0] is True
        admin.commit()
        assert _credential_present(admin, credential)


# ----------------------------------------------------------------------- security boundary


def test_the_api_runtime_role_is_refused_before_any_table_is_touched(
    settings: Settings,
    admin_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _connect(admin_settings) as admin:
        link, credential = _seed_link(
            admin, state="RECEIVED", expires_in=EXPIRED, failure="fixture_lookup_failed"
        )
        before = _link(admin, link)
        with _connect(settings) as runtime:
            with pytest.raises(ExpiryRefused, match="Row Level Security"):
                dry_run(runtime, limit=10)
            with pytest.raises(ExpiryRefused, match="Row Level Security"):
                apply(runtime, limit=10)
        assert _link(admin, link) == before
        assert _credential_present(admin, credential)

        runtime_url = settings.database_url.get_secret_value()
        monkeypatch.setenv("VEOTREX_EXPIRY_TEST_RUNTIME_URL", runtime_url)
        assert (
            expiry_main(["apply", "--url-ref", "env:VEOTREX_EXPIRY_TEST_RUNTIME_URL"])
            == EXIT_REFUSED
        )
        out = capsys.readouterr().out
        assert out.startswith("REFUSED:") and "runtime role" in out
        assert runtime_url not in out and "postgresql" not in out
        assert _credential_present(admin, credential)


def test_cli_prints_counts_only_and_bounds_the_limit(
    admin_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admin_url = admin_settings.database_url.get_secret_value()
    monkeypatch.setenv("VEOTREX_EXPIRY_TEST_ADMIN_URL", admin_url)
    reference = ["--url-ref", "env:VEOTREX_EXPIRY_TEST_ADMIN_URL"]
    with _connect(admin_settings) as admin:
        link, credential = _seed_link(
            admin, state="UNCLAIMED", expires_in=EXPIRED, account=f"acct-{uuid4().hex}"
        )
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT credential_secret_ref, encode(c.nonce, 'hex'), encode(c.ciphertext, 'hex') "
                "FROM ring_pending_links l JOIN encrypted_credentials c ON c.owner_id = l.id "
                "WHERE l.id = %s",
                (link,),
            )
            secret_ref, nonce_hex, ciphertext_hex = cursor.fetchone() or ("", "", "")
        admin.rollback()

        assert expiry_main(["dry-run", "--limit", "0", *reference]) == EXIT_REFUSED
        assert expiry_main(["dry-run", "--limit", str(MAX_LIMIT + 1), *reference]) == EXIT_REFUSED
        capsys.readouterr()

        code = expiry_main(["dry-run", "--json", *reference])
        assert code in (EXIT_OK, EXIT_ATTENTION)
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["mode"] == "dry-run" and parsed["candidates"] >= 1
        assert set(parsed) == set(dry_run(admin, limit=1).as_dict())
        for forbidden in (
            str(link),
            str(credential),
            str(secret_ref),
            "vault://",
            str(nonce_hex),
            str(ciphertext_hex),
            admin_url,
            "postgresql",
        ):
            assert forbidden not in out
        assert _credential_present(admin, credential)

        code = expiry_main(["apply", "--limit", "5", *reference])
        assert code in (EXIT_OK, EXIT_ATTENTION)
        out = capsys.readouterr().out
        assert "mode=apply" in out and "archived=" in out and "credentials_removed=" in out
        assert str(link) not in out and str(credential) not in out and "vault://" not in out
        assert "expired[" in out or "archived=" in out


def test_report_rendering_carries_no_identifier_fields() -> None:
    report = ExpiryReport(mode="dry-run", limit=5, database_time="t", examined=1)
    rendered = report.as_dict()
    assert all(isinstance(value, int | str | bool | dict) for value in rendered.values())
    assert "id" not in rendered and "ids" not in rendered and "secret_ref" not in rendered
    text = render(report, as_json=False)
    assert "mode=dry-run" in text and "needs_attention=False" in text
