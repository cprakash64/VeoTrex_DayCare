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


def readiness_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if not str(report["public_https_origin"]).startswith("https://"):
        blockers.append("public HTTPS origin is not configured")
    if report["credential_vault"] != "ready":
        blockers.append("credential vault master key is not configured")
    for field in ("ring_client_id", "ring_client_secret", "ring_hmac_key"):
        if report[field] != "configured":
            blockers.append(f"{field} is not configured")
    if report["ring_account"] != "linked":
        blockers.append("no Ring account is linked")
    return blockers


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
            print("READY_FOR_PORTAL_CONFIGURATION: no")
            for blocker in blockers:
                print(f"  - {blocker}")
        return 2
    report = build_report(settings)
    blockers = readiness_blockers(report)
    if arguments.json:
        print(json.dumps({**report, "blockers": blockers}, indent=2, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key.upper()}: {value if value is not None else 'missing'}")
        print("READY_FOR_PORTAL_CONFIGURATION:", "no" if blockers else "yes")
        for blocker in blockers:
            print(f"  - {blocker}")
    return 0


if __name__ == "__main__":  # pragma: no cover - console wrapper
    sys.exit(main())
