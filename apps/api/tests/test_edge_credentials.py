"""V1-DEMO-03B: admin-only edge credential provisioning (``veotrex-edge-credential``).

Synthetic credentials only; nothing here is a production credential. The file tests need no
database. The issue/revoke tests use the ADMIN identity exactly as the console script does, and
then prove the issued credential authenticates through the runtime role's function.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from tests_edge_fixtures import add_node, seed_world, set_tenant

from veotrex_api.config import Settings
from veotrex_api.credential_vault import InMemoryCredentialVault
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.edge_auth import parse_edge_token
from veotrex_api.edge_credentials import (
    CredentialOutputRefused,
    EdgeCredentialError,
    create_credential_file,
    issue_credential,
    parser,
    revoke_credential,
    validate_output_path,
)
from veotrex_api.models import AuditEvent, EdgeNode, EdgeNodeCredential


# ---------------------------------------------------------------------------- file safety
@pytest.mark.parametrize("stream", ["-", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1"])
def test_streams_and_device_paths_are_refused(stream: str) -> None:
    with pytest.raises(CredentialOutputRefused):
        validate_output_path(stream)


def test_proc_and_sys_paths_are_refused() -> None:
    for path in ("/proc/self/fd/1", "/sys/kernel/x"):
        with pytest.raises(CredentialOutputRefused):
            validate_output_path(path)


def test_an_existing_file_is_refused_and_left_untouched(tmp_path: Path) -> None:
    existing = tmp_path / "edge.credential"
    existing.write_text("operator data")
    with pytest.raises(CredentialOutputRefused, match="already exists"):
        validate_output_path(str(existing))
    with pytest.raises(CredentialOutputRefused, match="already exists"):
        create_credential_file(existing)
    assert existing.read_text() == "operator data"


def test_symlinks_are_refused_whatever_they_point_at(tmp_path: Path) -> None:
    target = tmp_path / "target"
    live = tmp_path / "live-link"
    live.symlink_to(target)
    dangling = tmp_path / "dangling-link"
    dangling.symlink_to(tmp_path / "does-not-exist")
    for link in (live, dangling):
        with pytest.raises(CredentialOutputRefused):
            validate_output_path(str(link))
        with pytest.raises(CredentialOutputRefused):
            create_credential_file(link)
    assert not target.exists()


def test_a_missing_directory_and_blank_paths_are_refused(tmp_path: Path) -> None:
    for raw in ("", " ", f" {tmp_path}/x", str(tmp_path / "missing" / "edge.credential")):
        with pytest.raises(CredentialOutputRefused):
            validate_output_path(raw)


def test_the_credential_file_is_created_exclusively_at_0600(tmp_path: Path) -> None:
    old_umask = os.umask(0)
    try:
        path = validate_output_path(str(tmp_path / "edge.credential"))
        descriptor = create_credential_file(path)
        os.close(descriptor)
    finally:
        os.umask(old_umask)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert stat.S_ISREG(path.lstat().st_mode)


def test_the_cli_has_no_way_to_print_the_credential() -> None:
    issue = parser().parse_args(
        ["issue", "--tenant-id", str(uuid4()), "--edge-node-id", str(uuid4()), "--output", "x"]
    )
    assert issue.command == "issue" and issue.output == "x"
    with pytest.raises(SystemExit):
        parser().parse_args(["issue", "--tenant-id", str(uuid4()), "--edge-node-id", str(uuid4())])
    help_text = parser().format_help()
    assert "--stdout" not in help_text and "--print" not in help_text


# ------------------------------------------------------------------------- issue and revoke
async def test_issue_writes_the_token_once_stores_only_a_digest_and_prints_no_secret(
    settings: Settings, admin_settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin_engine, engine = make_engine(admin_settings), make_engine(settings)
    admin_factory, factory = make_session_factory(admin_engine), make_session_factory(engine)
    try:
        world = await seed_world(admin_factory, InMemoryCredentialVault())
        output = tmp_path / "jetson.credential"
        summary = await issue_credential(
            admin_factory, tenant_id=world.tenant_id, edge_node_id=world.node_id, output=str(output)
        )
        token = output.read_text()
        assert token.endswith("\n") and token.count("\n") == 1
        token = token.strip()
        credential_id, digest = parse_edge_token(token)
        assert credential_id == summary.credential_id and summary.output == output
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert token not in repr(summary)

        async with admin_factory() as session, session.begin():
            await set_tenant(session, world.tenant_id)
            row = await session.get(EdgeNodeCredential, credential_id)
            assert row is not None and row.status == "ACTIVE" and row.revoked_at is None
            assert row.secret_sha256 == digest and row.edge_node_id == world.node_id
            dumped = (
                await session.execute(
                    text("SELECT row_to_json(c)::text FROM edge_node_credentials c WHERE id = :id"),
                    {"id": credential_id},
                )
            ).scalar_one()
            assert token.rsplit(".", 1)[1] not in dumped
            audit = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == world.tenant_id,
                        AuditEvent.target_id == credential_id,
                    )
                )
            ).one()
            assert audit.action == "edge.credential.issued"
            assert token not in str(audit.metadata_)

        async with factory() as session, session.begin():
            resolved = (
                await session.execute(
                    text("SELECT * FROM authenticate_edge_node_credential(:id, :digest)"),
                    {"id": credential_id, "digest": digest},
                )
            ).one()
            assert resolved.edge_node_id == world.node_id

        assert await revoke_credential(
            admin_factory, tenant_id=world.tenant_id, credential_id=credential_id
        )
        assert not await revoke_credential(
            admin_factory, tenant_id=world.tenant_id, credential_id=credential_id
        )
        async with admin_factory() as session, session.begin():
            await set_tenant(session, world.tenant_id)
            revoked = await session.get(EdgeNodeCredential, credential_id)
            assert revoked is not None and revoked.status == "REVOKED"
            assert revoked.revoked_at is not None, "history is kept, never deleted"
            actions = set(
                (
                    await session.scalars(
                        select(AuditEvent.action).where(AuditEvent.target_id == credential_id)
                    )
                ).all()
            )
            assert actions == {"edge.credential.issued", "edge.credential.revoked"}
        async with factory() as session, session.begin():
            assert (
                await session.execute(
                    text("SELECT * FROM authenticate_edge_node_credential(:id, :digest)"),
                    {"id": credential_id, "digest": digest},
                )
            ).all() == []
        assert token not in capsys.readouterr().out
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_issue_refuses_unknown_disabled_or_foreign_nodes_before_writing(
    settings: Settings, admin_settings: Settings, tmp_path: Path
) -> None:
    admin_engine = make_engine(admin_settings)
    admin_factory = make_session_factory(admin_engine)
    try:
        world = await seed_world(admin_factory, InMemoryCredentialVault())
        other = await seed_world(admin_factory, InMemoryCredentialVault())
        disabled = await add_node(admin_factory, world.tenant_id, world.facility_id)
        async with admin_factory() as session, session.begin():
            await set_tenant(session, world.tenant_id)
            node = await session.get(EdgeNode, disabled)
            assert node is not None
            node.status = "DISABLED"
        for index, (tenant, node_id) in enumerate(
            (
                (world.tenant_id, uuid4()),
                (world.tenant_id, disabled),
                (world.tenant_id, other.node_id),
            )
        ):
            output = tmp_path / f"refused-{index}.credential"
            with pytest.raises(EdgeCredentialError):
                await issue_credential(
                    admin_factory, tenant_id=tenant, edge_node_id=node_id, output=str(output)
                )
            assert not output.exists(), "no file may exist for a refused issue"
        existing = tmp_path / "existing"
        existing.write_text("keep")
        with pytest.raises(CredentialOutputRefused):
            await issue_credential(
                admin_factory,
                tenant_id=world.tenant_id,
                edge_node_id=world.node_id,
                output=str(existing),
            )
        assert existing.read_text() == "keep"
        with pytest.raises(EdgeCredentialError):
            await revoke_credential(
                admin_factory, tenant_id=world.tenant_id, credential_id=other.credential_id
            )
    finally:
        await admin_engine.dispose()


async def test_the_console_script_prints_ids_and_path_but_never_the_credential(
    admin_settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from veotrex_api import config
    from veotrex_api.edge_credentials import run

    admin_engine = make_engine(admin_settings)
    try:
        world = await seed_world(make_session_factory(admin_engine), InMemoryCredentialVault())
    finally:
        await admin_engine.dispose()
    monkeypatch.setattr(config, "get_settings", lambda: admin_settings)
    output = tmp_path / "cli.credential"
    arguments = parser().parse_args(
        [
            "issue",
            "--tenant-id",
            str(world.tenant_id),
            "--edge-node-id",
            str(world.node_id),
            "--output",
            str(output),
        ]
    )
    assert await run(arguments) == 0
    printed = capsys.readouterr().out
    token = output.read_text().strip()
    credential_id, _ = parse_edge_token(token)
    assert token not in printed and token.rsplit(".", 1)[1] not in printed
    assert f"edge_node_id={world.node_id}" in printed
    assert f"credential_id={credential_id}" in printed
    assert str(output) in printed and "status=issued" in printed

    revoke = parser().parse_args(
        ["revoke", "--tenant-id", str(world.tenant_id), "--credential-id", str(credential_id)]
    )
    assert await run(revoke) == 0
    assert "status=revoked" in capsys.readouterr().out
