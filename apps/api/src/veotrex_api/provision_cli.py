import argparse
import asyncio
from uuid import UUID, uuid4

from veotrex_api.config import get_settings
from veotrex_api.db import make_engine, make_session_factory
from veotrex_api.provisioning import (
    BootstrapOwnerRequest,
    CreateTenantRequest,
    ProvisioningError,
    bootstrap_owner,
    ensure_tenant,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="veotrex-provision",
        description="Privileged, non-public VeoTrex identity provisioning",
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    tenant = subcommands.add_parser(
        "create-tenant",
        description="Create one tenant, or confirm the identical one already exists",
    )
    tenant.add_argument("--tenant-id", required=True, type=UUID)
    tenant.add_argument("--name", required=True)
    tenant.add_argument("--dry-run", action="store_true")
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
    try:
        if arguments.command == "create-tenant":
            tenant_request = CreateTenantRequest(
                tenant_id=arguments.tenant_id,
                name=arguments.name,
                request_id=f"admin-cli:{uuid4()}",
            )
            async with factory() as session, session.begin():
                created = await ensure_tenant(session, tenant_request, dry_run=arguments.dry_run)
            if arguments.dry_run:
                print("Dry run passed; no changes made.")
            elif created:
                print(f"Tenant {arguments.tenant_id} created.")
            else:
                print(f"Tenant {arguments.tenant_id} already exists; no changes made.")
            return 0

        request = BootstrapOwnerRequest(
            tenant_id=arguments.tenant_id,
            issuer=arguments.issuer,
            external_organization_id=arguments.organization_id,
            subject=arguments.subject,
            display_name=arguments.display_name,
            request_id=f"admin-cli:{uuid4()}",
        )
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
