from argparse import Namespace
from uuid import uuid4

import pytest

from veotrex_api import provision_cli
from veotrex_api.provisioning import ProvisioningError


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    def begin(self):
        return AsyncContext()


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def arguments(*, dry_run: bool = False) -> Namespace:
    return Namespace(
        command="bootstrap-owner",
        tenant_id=uuid4(),
        issuer="https://tenant.auth0.example/",
        organization_id="org_example",
        subject="auth0|example",
        display_name="Owner",
        dry_run=dry_run,
    )


def test_cli_parser_requires_explicit_bootstrap_parameters() -> None:
    parsed = provision_cli.parser().parse_args(
        [
            "bootstrap-owner",
            "--tenant-id",
            str(uuid4()),
            "--issuer",
            "https://tenant.auth0.example/",
            "--organization-id",
            "org_example",
            "--subject",
            "auth0|example",
            "--dry-run",
        ]
    )
    assert parsed.command == "bootstrap-owner"
    assert parsed.dry_run is True


@pytest.mark.parametrize("dry_run", [False, True])
async def test_cli_run_reports_safe_success(monkeypatch, capsys, dry_run: bool) -> None:
    engine = FakeEngine()

    async def fake_bootstrap(session, request, *, dry_run):
        assert session is not None
        assert request.subject == "auth0|example"
        return None if dry_run else uuid4()

    monkeypatch.setattr(provision_cli, "get_settings", lambda: object())
    monkeypatch.setattr(provision_cli, "make_engine", lambda settings: engine)
    monkeypatch.setattr(provision_cli, "make_session_factory", lambda engine: AsyncContext)
    monkeypatch.setattr(provision_cli, "bootstrap_owner", fake_bootstrap)

    assert await provision_cli.run(arguments(dry_run=dry_run)) == 0
    output = capsys.readouterr().out
    assert "auth0|example" not in output
    assert "secret" not in output.lower()
    assert engine.disposed


async def test_cli_refusal_is_nonzero_and_safe(monkeypatch, capsys) -> None:
    engine = FakeEngine()

    async def refuse(session, request, *, dry_run):
        raise ProvisioningError("duplicate binding")

    monkeypatch.setattr(provision_cli, "get_settings", lambda: object())
    monkeypatch.setattr(provision_cli, "make_engine", lambda settings: engine)
    monkeypatch.setattr(provision_cli, "make_session_factory", lambda engine: AsyncContext)
    monkeypatch.setattr(provision_cli, "bootstrap_owner", refuse)

    assert await provision_cli.run(arguments()) == 2
    assert "duplicate binding" in capsys.readouterr().out
    assert engine.disposed
