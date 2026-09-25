"""Admin-only edge node credential provisioning (V1-DEMO-03B).

Not an HTTP route. Like ``veotrex-provision`` and ``veotrex-staff-recognition-package`` this is a
console script run with the admin database identity, by an operator, for one existing EdgeNode.

``issue`` generates a 256-bit credential, stores only its digest, and writes the plaintext ONCE
to a new regular file that this process creates itself: ``O_CREAT | O_EXCL | O_NOFOLLOW`` at
mode 0600. Standard output, ``/dev/*``, ``/proc/*``, an existing path and a symlink are all
refused, so the credential cannot land in a terminal, a shell history, a pipeline, a log
collector or a file someone else can already read. The credential itself is never printed;
only the node id, the credential id and the destination path are.

``revoke`` marks a credential REVOKED with a timestamp. Rows are never deleted, so the audit
history of which credential existed for which node, and when it stopped working, survives.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.edge_auth import issue_edge_credential
from veotrex_api.models import AuditEvent, EdgeNode, EdgeNodeCredential

# Paths that are streams or kernel views rather than files an operator can protect.
_REFUSED_PREFIXES = ("/dev/", "/proc/", "/sys/")
_REFUSED_EXACT = frozenset({"-", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/null"})


class CredentialOutputRefused(Exception):
    """The destination cannot hold a machine credential safely. Never carries the secret."""


class EdgeCredentialError(Exception):
    """Provisioning refused for a stated, secret-free reason."""


@dataclass(frozen=True, slots=True)
class IssuedCredentialSummary:
    edge_node_id: UUID
    credential_id: UUID
    output: Path


def validate_output_path(raw: str) -> Path:
    """Refuse every destination that is not a brand-new regular file in an existing directory.

    The final authority is the exclusive, no-follow ``open`` in ``create_credential_file``; this
    check exists so a refusal happens before anything is generated or stored.
    """
    if not raw or raw != raw.strip():
        raise CredentialOutputRefused("an explicit output file path is required")
    if raw in _REFUSED_EXACT:
        raise CredentialOutputRefused(
            "writing the credential to a stream is refused; name a new file"
        )
    absolute = os.path.abspath(raw)
    if absolute in _REFUSED_EXACT or absolute.startswith(_REFUSED_PREFIXES):
        raise CredentialOutputRefused(
            "writing the credential to a device or kernel path is refused; name a new file"
        )
    path = Path(absolute)
    # lexists is true for a dangling symlink too: a link is refused whatever it points at.
    if os.path.lexists(path):
        raise CredentialOutputRefused(
            "the output path already exists; choose a new path rather than overwriting one"
        )
    if not path.parent.is_dir():
        raise CredentialOutputRefused("the output directory does not exist")
    return path


def create_credential_file(path: Path) -> int:
    """Create ``path`` exclusively at 0600 without following a symlink; return the descriptor."""
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise CredentialOutputRefused(
            "the output path already exists; choose a new path rather than overwriting one"
        ) from None
    except OSError as exc:
        raise CredentialOutputRefused(
            f"the output path cannot be created: {exc.strerror}"
        ) from None
    try:
        # Belt and braces against a umask or filesystem that widened the creation mode.
        os.fchmod(descriptor, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CredentialOutputRefused("the output path is not a regular file")
    except BaseException:
        os.close(descriptor)
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return descriptor


def write_credential(descriptor: int, token: SecretStr) -> None:
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(token.get_secret_value())
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


async def _require_enabled_node(session: AsyncSession, tenant_id: UUID, edge_node_id: UUID) -> None:
    node_status = await session.scalar(
        select(EdgeNode.status).where(EdgeNode.id == edge_node_id, EdgeNode.tenant_id == tenant_id)
    )
    if node_status is None:
        raise EdgeCredentialError("edge node not found in this tenant")
    if node_status == "DISABLED":
        raise EdgeCredentialError("edge node is DISABLED; enable it before issuing a credential")


async def issue_credential(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    edge_node_id: UUID,
    output: str,
) -> IssuedCredentialSummary:
    path = validate_output_path(output)
    # Refuse an unknown or disabled node before a file or a secret exists.
    async with factory() as session, session.begin():
        await _set_tenant(session, tenant_id)
        await _require_enabled_node(session, tenant_id, edge_node_id)
    descriptor = create_credential_file(path)
    issued = issue_edge_credential()
    try:
        write_credential(descriptor, issued.token)
        async with factory() as session, session.begin():
            await _set_tenant(session, tenant_id)
            await _require_enabled_node(session, tenant_id, edge_node_id)
            session.add(
                EdgeNodeCredential(
                    id=issued.credential_id,
                    tenant_id=tenant_id,
                    edge_node_id=edge_node_id,
                    secret_sha256=issued.secret_sha256,
                    status="ACTIVE",
                )
            )
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    actor_id=None,
                    action="edge.credential.issued",
                    target_type="edge_node_credential",
                    target_id=issued.credential_id,
                    request_id=f"admin-cli:{uuid4()}",
                    metadata_={"edge_node_id": str(edge_node_id)},
                )
            )
    except BaseException:
        # A credential file whose digest was never stored must not survive: it would look
        # usable and is not. The digest insert either committed or did not happen at all.
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return IssuedCredentialSummary(edge_node_id, issued.credential_id, path)


async def revoke_credential(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: UUID, credential_id: UUID
) -> bool:
    """Revoke one credential. True when this call revoked it, False when it already was."""
    async with factory() as session, session.begin():
        await _set_tenant(session, tenant_id)
        revoked = await session.execute(
            update(EdgeNodeCredential)
            .where(
                EdgeNodeCredential.id == credential_id,
                EdgeNodeCredential.tenant_id == tenant_id,
                EdgeNodeCredential.status == "ACTIVE",
            )
            .values(status="REVOKED", revoked_at=datetime.now(UTC))
            .returning(EdgeNodeCredential.edge_node_id)
        )
        edge_node_id = revoked.scalar_one_or_none()
        if edge_node_id is None:
            exists = await session.scalar(
                select(EdgeNodeCredential.id).where(
                    EdgeNodeCredential.id == credential_id,
                    EdgeNodeCredential.tenant_id == tenant_id,
                )
            )
            if exists is None:
                raise EdgeCredentialError("credential not found in this tenant")
            return False
        session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_id=None,
                action="edge.credential.revoked",
                target_type="edge_node_credential",
                target_id=credential_id,
                request_id=f"admin-cli:{uuid4()}",
                metadata_={"edge_node_id": str(edge_node_id)},
            )
        )
    return True


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="veotrex-edge-credential",
        description=(
            "Privileged edge node machine-credential provisioning. Issues a credential into a "
            "new 0600 file (never printed) or revokes one. Admin database identity only."
        ),
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    issue = subcommands.add_parser("issue", description="Issue a credential for one edge node")
    issue.add_argument("--tenant-id", required=True, type=UUID)
    issue.add_argument("--edge-node-id", required=True, type=UUID)
    issue.add_argument(
        "--output",
        required=True,
        help=(
            "path of the credential file to create. Must not exist; created 0600. Standard "
            "output, /dev, /proc and symlinks are refused."
        ),
    )
    revoke = subcommands.add_parser("revoke", description="Revoke one edge node credential")
    revoke.add_argument("--tenant-id", required=True, type=UUID)
    revoke.add_argument("--credential-id", required=True, type=UUID)
    return command


async def run(arguments: argparse.Namespace) -> int:
    from veotrex_api.config import get_settings
    from veotrex_api.db import make_engine, make_session_factory

    engine = make_engine(get_settings())
    factory = make_session_factory(engine)
    try:
        if arguments.command == "issue":
            summary = await issue_credential(
                factory,
                tenant_id=arguments.tenant_id,
                edge_node_id=arguments.edge_node_id,
                output=arguments.output,
            )
            print(f"edge_node_id={summary.edge_node_id}")
            print(f"credential_id={summary.credential_id}")
            print(f"written={summary.output} (mode 0600; the credential is not displayed)")
            print("status=issued")
            return 0
        changed = await revoke_credential(
            factory, tenant_id=arguments.tenant_id, credential_id=arguments.credential_id
        )
        print(f"credential_id={arguments.credential_id}")
        print("status=revoked" if changed else "status=already_revoked (no change)")
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(parser().parse_args())))
    except (CredentialOutputRefused, EdgeCredentialError) as exc:
        raise SystemExit(f"refused: {exc}") from None
