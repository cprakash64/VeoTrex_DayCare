"""Ring control-plane readiness report. Prints status only, never a secret value.

Answers one question: can real Ring credentials be introduced safely yet? Every field is either a
non-secret configured URL or the word ``configured``/``missing``. Secret *values* are never read
into the report, and the vault master key is never printed, hashed, or fingerprinted, because a
key-derived identifier would help an offline attacker.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from pydantic import ValidationError

from veotrex_api.config import Settings, get_settings
from veotrex_api.encrypted_vault import VaultKeyProvider
from veotrex_api.public_origin import InvalidPublicOrigin, validate_public_origin
from veotrex_api.secrets import DefaultSecretResolver, SecretResolutionError, SecretResolver

PLACEHOLDER_CLIENT_ID = "replace-with-ring-client-id"
# config.py defaults the OIDC authority to *.example.invalid so the suite can build Settings
# without a tenant. A deployment that kept those defaults verifies tokens against nothing.
PLACEHOLDER_AUTHORITY = ".invalid"


def _authority_state(value: str) -> str:
    """Placeholder authorities are reported missing: they are worse than unset, they look set."""
    if not value or PLACEHOLDER_AUTHORITY in value:
        return "missing"
    return "configured"


def _secret_state(resolver: SecretResolver, reference: str) -> str:
    """Presence only. The resolved value is never returned, logged, or compared to anything."""
    try:
        resolver.resolve(reference)
    except SecretResolutionError:
        return "missing"
    return "configured"


def build_report(
    settings: Settings | None = None, resolver: SecretResolver | None = None
) -> dict[str, Any]:
    resolved = settings or get_settings()
    # DefaultSecretResolver, not EnvironmentSecretResolver: deployed environments carry
    # `file:` references (orchestrator secret mounts), and an env-only resolver reports every
    # one of them as missing however well configured it is. This tool gates the Ring portal
    # step, so a false 'missing' here is worse than no report at all.
    secrets = resolver or DefaultSecretResolver()
    report: dict[str, Any] = {
        "public_https_origin": "missing",
        "account_link_url": None,
        "default_redirect_url": None,
        "token_exchange_url": None,
        "webhook_url": None,
        "credential_vault": "unavailable",
        "vault_master_key_source": _secret_state(secrets, resolved.vault_master_key_ref),
        "database": "configured" if resolved.database_url.get_secret_value() else "missing",
        "ring_client_id": (
            "missing"
            if not resolved.ring_client_id or resolved.ring_client_id == PLACEHOLDER_CLIENT_ID
            else "configured"
        ),
        "ring_client_secret": _secret_state(secrets, resolved.ring_client_secret_ref),
        "ring_hmac_key": _secret_state(secrets, resolved.ring_hmac_signing_key_ref),
        "oidc_issuer": _authority_state(resolved.oidc_issuer),
        "oidc_audience": _authority_state(resolved.oidc_audience),
        "webrtc_runtime": "qualified",
        "ring_account": "not linked",
        "ring_linking_model": "one-way account linking (Ring-driven)",
    }
    if resolved.public_origin:
        try:
            origin = validate_public_origin(resolved.public_origin)
        except InvalidPublicOrigin as exc:
            report["public_https_origin"] = f"invalid: {exc}"
        else:
            report["public_https_origin"] = origin.origin
            report.update(origin.callback_urls())
    key_provider = VaultKeyProvider(secrets, resolved.vault_master_key_ref)
    if key_provider.available():
        report["credential_vault"] = "ready"
    return report


def prerequisite_blockers(report: dict[str, Any]) -> list[str]:
    """What must hold BEFORE the Ring portal can be configured at all.

    Deliberately excludes the Ring client id, client secret, HMAC key and the account link: those
    are OUTPUTS of the portal step, not inputs to it. Requiring them here made the report circular
    - it reported the deployment as not ready to begin portal configuration precisely because
    portal configuration had not happened yet, which is true of every deployment before the step
    and therefore tells an operator nothing.

    Backup, encryption and restore rehearsal are also prerequisites for storing real credentials,
    but they are host-level facts this process cannot observe from inside its container. They are
    qualified out of band and must not be asserted here on the strength of a guess.
    """
    blockers: list[str] = []
    if not str(report["public_https_origin"]).startswith("https://"):
        blockers.append("public HTTPS origin is not configured")
    for field, label in (
        ("account_link_url", "account link URL"),
        ("default_redirect_url", "default redirect URL"),
        ("token_exchange_url", "token exchange URL"),
        ("webhook_url", "webhook URL"),
    ):
        if not report[field]:
            blockers.append(f"{label} is not derivable from the public origin")
    if report["database"] != "configured":
        blockers.append("database is not configured")
    if report["credential_vault"] != "ready":
        blockers.append("credential vault master key is not configured")
    for field in ("oidc_issuer", "oidc_audience"):
        if report[field] != "configured":
            blockers.append(f"{field} is not configured")
    return blockers


def portal_configuration_blockers(report: dict[str, Any]) -> list[str]:
    """What the Ring portal step itself must produce. Absent before that step is expected."""
    return [
        f"{field} is not configured"
        for field in ("ring_client_id", "ring_client_secret", "ring_hmac_key")
        if report[field] != "configured"
    ]


def account_link_blockers(report: dict[str, Any]) -> list[str]:
    """Ring-driven one-way linking: only a real linked account satisfies this."""
    if report["ring_account"] != "linked":
        return ["no Ring account is linked"]
    return []


def readiness_blockers(report: dict[str, Any]) -> list[str]:
    """Everything outstanding for a fully operational Ring integration, in dependency order."""
    return (
        prerequisite_blockers(report)
        + portal_configuration_blockers(report)
        + account_link_blockers(report)
    )


def _settings_or_none() -> tuple[Settings | None, str | None]:
    """Load settings, converting a configuration error into a reportable message.

    The service itself fails closed on an invalid origin, which is correct: it must never derive
    Ring callbacks from a malformed value. A *diagnostic* tool that dies on the very
    misconfiguration it exists to explain is useless, so this command reports instead.
    """
    try:
        return get_settings(), None
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        return None, reasons or "settings are invalid"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="veotrex-ring-readiness",
        description="Report Ring control-plane readiness. Never prints secret values.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    arguments = parser.parse_args(argv)
    settings, configuration_error = _settings_or_none()
    if settings is None:
        # Pydantic reports the offending field and reason, never the value of a secret field
        # (secret references are declared repr=False and are not echoed here).
        report = {"configuration": f"invalid: {configuration_error}"}
        blockers = [f"control-plane configuration is invalid ({configuration_error})"]
        if arguments.json:
            print(json.dumps({**report, "blockers": blockers}, indent=2, sort_keys=True))
        else:
            print(f"CONFIGURATION: invalid: {configuration_error}")
            print("READY_TO_BEGIN_RING_PORTAL_CONFIGURATION: no")
            print("RING_PORTAL_CONFIGURATION_COMPLETE: no")
            print("RING_ACCOUNT_LINKED: no")
            for blocker in blockers:
                print(f"  - {blocker}")
        return 2
    report = build_report(settings)
    # Three distinct states in dependency order. Reporting one combined verdict conflated
    # "this deployment cannot host Ring" with "Ring has not been set up yet", which are
    # opposite situations requiring opposite actions.
    prerequisites = prerequisite_blockers(report)
    portal = portal_configuration_blockers(report)
    account = account_link_blockers(report)
    blockers = prerequisites + portal + account
    if arguments.json:
        print(
            json.dumps(
                {
                    **report,
                    "ready_to_begin_ring_portal_configuration": not prerequisites,
                    "ring_portal_configuration_complete": not prerequisites and not portal,
                    "ring_account_linked": not account,
                    "prerequisite_blockers": prerequisites,
                    "portal_configuration_blockers": portal,
                    "account_link_blockers": account,
                    "blockers": blockers,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for key, value in report.items():
            print(f"{key.upper()}: {value if value is not None else 'missing'}")
        print("READY_TO_BEGIN_RING_PORTAL_CONFIGURATION:", "no" if prerequisites else "yes")
        for blocker in prerequisites:
            print(f"  - {blocker}")
        complete = not prerequisites and not portal
        print("RING_PORTAL_CONFIGURATION_COMPLETE:", "yes" if complete else "no")
        for blocker in portal:
            print(f"  - {blocker} (expected before the Ring portal step)")
        print("RING_ACCOUNT_LINKED:", "no" if account else "yes")
        for blocker in account:
            print(f"  - {blocker} (expected before account linking)")
    return 0


if __name__ == "__main__":  # pragma: no cover - console wrapper
    sys.exit(main())
