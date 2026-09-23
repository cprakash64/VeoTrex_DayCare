"""Recognition package for the edge (V1-02A contract, consumed by V1-02B).

The only way a face template leaves the database. It is deliberately NOT an HTTP route: the
edge agent has no authenticated identity toward the control plane yet, and the dashboard must
never receive templates. Until an edge credential model exists, the package is produced by
the privileged console script ``veotrex-staff-recognition-package`` (admin identity, like
``veotrex-provision``) and carried to the Jetson by the operator.

Contents: one tenant; only profiles that are ACTIVE and READY; only ACTIVE templates whose
model identity and version match the requested backend; opaque ids and display names;
deterministic ordering; a ``revision`` derived from every included row so a consumer can
detect change without polling row by row; bounded size. No enrollment image bytes.

V1-02B0 hardened how the package leaves the process, because from this stage on the templates
in it are real adult biometrics rather than a fake backend's hashes. The package is written to
a file the tool creates itself, exclusively, following no symlink, at mode 0600; it refuses a
path that already exists, a character device and a symlink, so ``--output /dev/stdout`` or
``--output ~/notes.json`` cannot spill template material into a terminal, a shell history, a
pipeline or a file someone else can read. Only counts and a truncated revision are printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veotrex_api.models import StaffFaceTemplate, StaffProfile

SCHEMA_VERSION = 1
MAX_STAFF = 500
MAX_TEMPLATES_PER_STAFF = 5


async def build_recognition_package(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    *,
    model_id: str,
    model_version: str,
    template_version: int,
) -> dict[str, Any]:
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        profiles = (
            await session.scalars(
                select(StaffProfile)
                .where(
                    StaffProfile.tenant_id == tenant_id,
                    StaffProfile.status == "ACTIVE",
                    StaffProfile.enrollment_state == "READY",
                )
                .order_by(StaffProfile.id)
                .limit(MAX_STAFF + 1)
            )
        ).all()
        if len(profiles) > MAX_STAFF:
            raise ValueError("package bound exceeded")
        entries: list[dict[str, Any]] = []
        digest = hashlib.sha256()
        for profile in profiles:
            templates = (
                await session.scalars(
                    select(StaffFaceTemplate)
                    .where(
                        StaffFaceTemplate.tenant_id == tenant_id,
                        StaffFaceTemplate.staff_profile_id == profile.id,
                        StaffFaceTemplate.status == "ACTIVE",
                        StaffFaceTemplate.model_id == model_id,
                        StaffFaceTemplate.model_version == model_version,
                        StaffFaceTemplate.template_version == template_version,
                    )
                    .order_by(StaffFaceTemplate.id)
                    .limit(MAX_TEMPLATES_PER_STAFF)
                )
            ).all()
            if not templates:
                continue
            digest.update(f"{profile.id}:{profile.updated_at.isoformat()}:".encode())
            rendered = []
            for template in templates:
                digest.update(f"{template.id}:".encode())
                rendered.append(
                    {
                        "template_id": str(template.id),
                        "dimensions": template.dimensions,
                        "dtype": template.dtype,
                        "quality": template.quality,
                        "data_base64": base64.b64encode(template.template).decode("ascii"),
                    }
                )
            entries.append(
                {
                    "staff_id": str(profile.id),
                    "display_name": profile.display_name,
                    "templates": rendered,
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "tenant_id": str(tenant_id),
        "model_id": model_id,
        "model_version": model_version,
        "template_version": template_version,
        "revision": digest.hexdigest(),
        "generated_at": datetime.now(UTC).isoformat(),
        "staff": entries,
    }


class PackageOutputRefused(Exception):
    """The requested destination cannot hold biometric material safely."""


def write_package(package: dict[str, Any], output: Path) -> None:
    """Write the package to a file this process creates, or refuse.

    ``O_CREAT | O_EXCL`` means the file did not exist a moment ago and this process made it, so
    there is no pre-existing mode, owner or symlink to inherit and no ``/dev/stdout`` to write
    through: every one of those fails here rather than after the templates have been written.
    ``O_NOFOLLOW`` closes the same hole on the final path component for platforms where a
    symlink could otherwise be created between the check and the open.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except FileExistsError:
        raise PackageOutputRefused(
            "the output path already exists; choose a new path rather than overwriting one"
        ) from None
    except OSError as exc:
        raise PackageOutputRefused(f"the output path cannot be created: {exc.strerror}") from None
    try:
        # The descriptor is a regular file this process just created; chmod is belt-and-braces
        # against a umask that somehow widened the creation mode.
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(package, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # A partial file holding real templates must not survive a failed write.
        output.unlink(missing_ok=True)
        raise


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="veotrex-staff-recognition-package",
        description=(
            "Privileged export of the ACTIVE+READY staff face templates of one tenant for the "
            "edge recogniser. Writes a 0600 JSON file; never prints template data."
        ),
    )
    command.add_argument("--tenant-id", required=True, type=UUID)
    command.add_argument(
        "--output",
        required=True,
        help=(
            "path of the JSON package to write. Must not already exist; it is created 0600. "
            "Writing to a terminal, a pipe or /dev/stdout is refused."
        ),
    )
    command.add_argument("--model-id", required=True)
    command.add_argument("--model-version", required=True)
    command.add_argument("--template-version", required=True, type=int)
    return command


async def run(arguments: argparse.Namespace) -> int:
    from veotrex_api.config import get_settings
    from veotrex_api.db import make_engine, make_session_factory

    engine = make_engine(get_settings())
    try:
        package = await build_recognition_package(
            make_session_factory(engine),
            arguments.tenant_id,
            model_id=arguments.model_id,
            model_version=arguments.model_version,
            template_version=arguments.template_version,
        )
    finally:
        await engine.dispose()
    write_package(package, Path(arguments.output))
    print(
        f"wrote {arguments.output}: staff={len(package['staff'])} "
        f"revision={package['revision'][:16]}"
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(parser().parse_args())))
    except PackageOutputRefused as exc:
        raise SystemExit(f"refused: {exc}") from None
