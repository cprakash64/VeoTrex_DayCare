"""Ring-facing control-plane security: readiness redaction, webhook HMAC, nonce, redirect safety.

No real Ring credential, key, token or nonce appears here; every value is obviously synthetic.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.public_origin import DEFAULT_REDIRECT_PATH, validate_public_origin
from veotrex_api.ring_nonce import (
    InvalidRingLink,
    compute_ring_nonce,
    ring_nonce_matches,
    validate_ring_timestamp,
)
from veotrex_api.ring_readiness import build_report, readiness_blockers
from veotrex_api.ring_webhook import RingWebhookError, parse_webhook, verify_signature
from veotrex_api.secrets import SecretResolutionError

SYNTHETIC_HMAC_KEY = "synthetic-hmac-signing-key-not-real"
SYNTHETIC_CLIENT_SECRET = "synthetic-ring-client-secret-not-real"  # noqa: S105 - synthetic
SYNTHETIC_MASTER_KEY = "c3ludGhldGljLXZhdWx0LW1hc3Rlci1rZXktMzJiISE="
ORIGIN = "https://ring.example.test"


class StubResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, secret_ref: str) -> SecretStr:
        if secret_ref not in self._values:
            raise SecretResolutionError("referenced secret is unavailable")
        return SecretStr(self._values[secret_ref])


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "test",
        "database_url": "postgresql+psycopg://u:p@localhost:5432/db",
        "app_version": "0.0.0-test",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def webhook_body(event_id: str = "synthetic-event-1", **meta_overrides: object) -> bytes:
    """Envelope shaped like the real Ring v1.1 contract enforced by WebhookEnvelope."""
    meta: dict[str, object] = {
        "version": "1.1",
        "time": "2026-09-12T12:00:00.000000Z",
        "request_id": "synthetic-request-1",
        "account_id": "synthetic-account-id",
    }
    meta.update(meta_overrides)
    return json.dumps(
        {
            "meta": meta,
            "data": {
                "id": event_id,
                "type": "motion_detected",
                "attributes": {
                    "source": "synthetic/device",
                    "source_type": "devices",
                    "timestamp": 1789000000000,
                    "sub_type": "human",
                    "component_ids": ["synthetic/component"],
                },
            },
        }
    ).encode()


def signature_for(body: bytes, key: str = SYNTHETIC_HMAC_KEY) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------- readiness reporting
def test_readiness_default_resolver_reads_file_backed_secrets(tmp_path: Path) -> None:
    """The DEFAULT resolver must understand every reference scheme Settings can carry.

    Deployed environments mount secrets as files and use `file:` references. build_report()
    previously defaulted to an env-only resolver, so a correctly configured deployment was
    reported as entirely unconfigured: the staging host had a working vault - proven by a real
    encrypt/decrypt round-trip against its database - while this command printed
    `vault_master_key_source: missing` and `credential_vault: unavailable`.

    Every other case here injects a StubResolver, so the default path was never exercised. This
    one deliberately passes no resolver.
    """
    key = tmp_path / "vault_master_key"
    key.write_text(SYNTHETIC_MASTER_KEY)
    report = build_report(settings(vault_master_key_ref=f"file:{key}"))
    assert report["vault_master_key_source"] == "configured"
    assert report["credential_vault"] == "ready"
    assert "credential vault master key is not configured" not in readiness_blockers(report)


def test_readiness_default_resolver_still_reports_a_missing_file(tmp_path: Path) -> None:
    """Fail-closed is preserved: an absent or empty reference is still reported missing."""
    absent = build_report(settings(vault_master_key_ref=f"file:{tmp_path / 'absent'}"))
    assert absent["vault_master_key_source"] == "missing"
    assert absent["credential_vault"] == "unavailable"
    empty_file = tmp_path / "empty"
    empty_file.write_text("")
    empty = build_report(settings(vault_master_key_ref=f"file:{empty_file}"))
    assert empty["vault_master_key_source"] == "missing"


def test_readiness_reports_missing_configuration_without_secrets() -> None:
    report = build_report(settings(), StubResolver({}))
    assert report["public_https_origin"] == "missing"
    assert report["credential_vault"] == "unavailable"
    assert report["vault_master_key_source"] == "missing"
    assert report["ring_client_id"] == "missing"  # placeholder is not "configured"
    assert report["ring_client_secret"] == "missing"  # noqa: S105 - a status word, not a secret
    assert report["ring_hmac_key"] == "missing"
    assert report["ring_account"] == "not linked"
    assert report["webrtc_runtime"] == "qualified"
    assert "one-way" in report["ring_linking_model"]
    assert readiness_blockers(report)


def test_readiness_reports_configured_state_and_never_prints_a_secret() -> None:
    resolver = StubResolver(
        {
            "env:VEOTREX_VAULT_MASTER_KEY": SYNTHETIC_MASTER_KEY,
            "env:RING_CLIENT_SECRET": SYNTHETIC_CLIENT_SECRET,
            "env:RING_HMAC_SIGNING_KEY": SYNTHETIC_HMAC_KEY,
        }
    )
    report = build_report(
        settings(public_origin=ORIGIN, ring_client_id="synthetic-client-id"), resolver
    )
    assert report["public_https_origin"] == ORIGIN
    assert report["account_link_url"] == f"{ORIGIN}/integrations/ring/link"
    assert report["webhook_url"] == f"{ORIGIN}/v1/providers/ring/webhooks"
    assert report["credential_vault"] == "ready"
    assert report["vault_master_key_source"] == "configured"
    assert report["ring_client_secret"] == "configured"  # noqa: S105 - a status word, not a secret
    assert report["ring_hmac_key"] == "configured"
    rendered = json.dumps(report)
    for secret in (SYNTHETIC_MASTER_KEY, SYNTHETIC_CLIENT_SECRET, SYNTHETIC_HMAC_KEY):
        assert secret not in rendered
    # Only the account link remains outstanding once configuration is present.
    assert readiness_blockers(report) == ["no Ring account is linked"]


def test_invalid_configured_origin_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drive the real entry point: the service fails closed, but the diagnostic must diagnose.

    An earlier version of this test hand-built a report dict and never exercised settings loading,
    so it passed while the command itself crashed with a traceback on a misconfigured origin.
    """
    from veotrex_api.config import get_settings
    from veotrex_api.ring_readiness import main

    monkeypatch.setenv("VEOTREX_ENVIRONMENT", "local")
    monkeypatch.setenv("VEOTREX_APP_VERSION", "0.0.0-test")
    monkeypatch.setenv("VEOTREX_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    for bad in ("http://localhost:8000", "not a url", "https://user:pw@host.example.test"):
        monkeypatch.setenv("VEOTREX_PUBLIC_ORIGIN", bad)
        get_settings.cache_clear()
        assert main([]) == 2  # reported, not raised
        printed = capsys.readouterr().out
        assert "READY_FOR_PORTAL_CONFIGURATION: no" in printed
        assert "invalid" in printed.lower()
        assert "Traceback" not in printed
    get_settings.cache_clear()
    # A valid origin still produces a normal report through the same entry point.
    monkeypatch.setenv("VEOTREX_PUBLIC_ORIGIN", ORIGIN)
    get_settings.cache_clear()
    assert main([]) == 0
    assert f"ACCOUNT_LINK_URL: {ORIGIN}/integrations/ring/link" in capsys.readouterr().out
    get_settings.cache_clear()


# --------------------------------------------------------------------- webhook security
def test_webhook_signature_matches_the_documented_contract() -> None:
    body = webhook_body()
    assert verify_signature(body, signature_for(body), SYNTHETIC_HMAC_KEY) is True


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "deadbeef",
        "sha1=" + "a" * 40,
        "sha256=" + "a" * 63,
        "sha256=" + "A" * 64,  # uppercase hex is not the documented encoding
        "sha256=" + "z" * 64,
        "sha256=",
    ],
)
def test_missing_or_malformed_signatures_are_rejected(signature: str | None) -> None:
    assert verify_signature(webhook_body(), signature, SYNTHETIC_HMAC_KEY) is False


def test_body_tampering_and_wrong_key_invalidate_the_signature() -> None:
    body = webhook_body()
    good = signature_for(body)
    assert verify_signature(body + b" ", good, SYNTHETIC_HMAC_KEY) is False
    assert (
        verify_signature(
            body.replace(b"motion_detected", b"device_removed"), good, SYNTHETIC_HMAC_KEY
        )
        is False
    )
    assert verify_signature(body, good, "synthetic-other-key") is False
    # A signature for a different body must not validate this one.
    assert (
        verify_signature(body, signature_for(webhook_body("synthetic-event-2")), SYNTHETIC_HMAC_KEY)
        is False
    )


def test_malformed_and_unsupported_envelopes_are_rejected_safely() -> None:
    for raw in (b"", b"not json", b"[]", json.dumps({"meta": {}}).encode()):
        with pytest.raises(RingWebhookError) as caught:
            parse_webhook(raw)
        assert "synthetic" not in str(caught.value).lower()
    # A complete envelope carrying an unknown version must reach the version check, not be
    # rejected earlier as malformed.
    with pytest.raises(RingWebhookError, match="unsupported_version"):
        parse_webhook(webhook_body(version="2.0"))


def test_duplicate_events_carry_a_stable_identifier_for_idempotency() -> None:
    first = parse_webhook(webhook_body("synthetic-event-9"))
    second = parse_webhook(webhook_body("synthetic-event-9"))
    assert first.event_id == second.event_id == "synthetic-event-9"


# --------------------------------------------------------------------- account link nonce
def test_nonce_matches_documented_hmac_and_rejects_replayed_or_expired_links() -> None:
    now = datetime.now(UTC)
    timestamp_ms = int(now.timestamp() * 1000)
    account = "synthetic-account-id"
    nonce = compute_ring_nonce(timestamp_ms, account, SYNTHETIC_HMAC_KEY)
    assert len(nonce) == 43 and "=" not in nonce
    assert ring_nonce_matches(nonce, timestamp_ms, account, SYNTHETIC_HMAC_KEY)
    # Bound to time and account: a nonce cannot be replayed under different parameters.
    assert not ring_nonce_matches(nonce, timestamp_ms + 1000, account, SYNTHETIC_HMAC_KEY)
    assert not ring_nonce_matches(
        nonce, timestamp_ms, "synthetic-other-account", SYNTHETIC_HMAC_KEY
    )
    assert not ring_nonce_matches(nonce, timestamp_ms, account, "synthetic-other-key")
    validate_ring_timestamp(timestamp_ms, now=now, validation_window_seconds=600)
    stale = int((now - timedelta(seconds=601)).timestamp() * 1000)
    with pytest.raises(InvalidRingLink, match="expired"):
        validate_ring_timestamp(stale, now=now, validation_window_seconds=600)
    future = int((now + timedelta(seconds=120)).timestamp() * 1000)
    with pytest.raises(InvalidRingLink, match="future"):
        validate_ring_timestamp(future, now=now, validation_window_seconds=600)


# --------------------------------------------------------------------- redirect safety
def test_default_redirect_is_a_fixed_first_party_path() -> None:
    origin = validate_public_origin(ORIGIN)
    assert origin.default_redirect_url == f"{ORIGIN}{DEFAULT_REDIRECT_PATH}"
    # There is no parameter through which a caller could supply a redirect target.
    assert "?" not in origin.default_redirect_url and "@" not in origin.default_redirect_url
    for hostile in ("https://attacker.example.test", "//attacker.example.test", "/../admin"):
        assert hostile not in origin.default_redirect_url
