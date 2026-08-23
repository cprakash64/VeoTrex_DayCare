import argparse
import asyncio
from uuid import UUID, uuid4

from veotrex_api.config import get_settings
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.provisioning import BootstrapOwnerRequest, ProvisioningError, bootstrap_owner


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="veotrex-provision",
        description="Privileged, non-public VeoTrex identity provisioning",
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    bootstrap = subcommands.add_parser(
        "bootstrap-owner", description="Atomically bind an Auth0 organization and first owner"
    )
    bootstrap.add_argument("--tenant-id", required=True, type=UUID)
    bootstrap.add_argument("--issuer", required=True)
    bootstrap.add_argument("--organization-id", required=True)
    bootstrap.add_argument("--subject", required=True)
    bootstrap.add_argument("--display-name")
    bootstrap.add_argument("--dry-run", action="store_true")
    return command


async def run(arguments: argparse.Namespace) -> int:
    settings = get_settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    request = BootstrapOwnerRequest(
        tenant_id=arguments.tenant_id,
        issuer=arguments.issuer,
        external_organization_id=arguments.organization_id,
        subject=arguments.subject,
        display_name=arguments.display_name,
        request_id=f"admin-cli:{uuid4()}",
    )
    try:
        async with factory() as session, session.begin():
            actor_id = await bootstrap_owner(session, request, dry_run=arguments.dry_run)
        if arguments.dry_run:
            print("Dry run passed; no changes made.")
        else:
            print(f"Tenant owner provisioned with actor ID {actor_id}.")
    except ProvisioningError as exc:
        print(f"Provisioning refused: {exc}")
        return 2
    finally:
        await engine.dispose()
    return 0


def main() -> None:
    arguments = parser().parse_args()
    raise SystemExit(asyncio.run(run(arguments)))
