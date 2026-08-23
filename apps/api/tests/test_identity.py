import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.identity import Auth0IdentityVerifier, IdentityVerificationError

ISSUER = "https://tenant.auth0.example/"
AUDIENCE = "https://api.veotrex.example"


class StubFetcher:
    def __init__(self, *responses: Mapping[str, Any] | Exception) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def fetch(self) -> Mapping[str, Any]:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr("postgresql+psycopg://unused"),
        app_version="test",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_allowed_algorithms="RS256",
        oidc_jwks_cache_ttl_seconds=300,
    )


def signing_material(kid: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return private_key, jwk


def token(
    private_key: rsa.RSAPrivateKey,
    kid: str,
    *,
    algorithm: str = "RS256",
    overrides: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "auth0|user-123",
        "org_id": "org_customer_a",
        "iat": now,
        "nbf": now - timedelta(seconds=1),
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides or {})
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})


async def test_valid_token_is_normalized_and_cached() -> None:
    private_key, jwk = signing_material("key-one")
    fetcher = StubFetcher({"keys": [jwk]})
    verifier = Auth0IdentityVerifier(settings(), fetcher)

    first = await verifier.verify(token(private_key, "key-one"))
    second = await verifier.verify(token(private_key, "key-one"))

    assert first == second
    assert first.provider == "auth0"
    assert first.issuer == ISSUER
    assert first.subject == "auth0|user-123"
    assert first.external_organization_id == "org_customer_a"
    assert fetcher.calls == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"nbf": datetime.now(UTC) + timedelta(minutes=5)},
        {"iss": "https://wrong-issuer.example/"},
        {"aud": "https://wrong-audience.example"},
        {"sub": None},
        {"org_id": None},
    ],
)
async def test_invalid_registered_or_required_claims_fail_closed(
    overrides: dict[str, Any],
) -> None:
    private_key, jwk = signing_material("claim-key")
    verifier = Auth0IdentityVerifier(settings(), StubFetcher({"keys": [jwk]}))
    with pytest.raises(IdentityVerificationError):
        await verifier.verify(token(private_key, "claim-key", overrides=overrides))


async def test_wrong_key_malformed_token_and_algorithm_confusion_are_rejected() -> None:
    trusted_private, trusted_jwk = signing_material("shared-kid")
    attacker_private, _ = signing_material("attacker")
    verifier = Auth0IdentityVerifier(settings(), StubFetcher({"keys": [trusted_jwk]}))

    with pytest.raises(IdentityVerificationError):
        await verifier.verify(token(attacker_private, "shared-kid"))
    with pytest.raises(IdentityVerificationError):
        await verifier.verify("not-a-jwt")

    now = datetime.now(UTC)
    confused = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "attacker",
            "org_id": "org_customer_a",
            "exp": now + timedelta(minutes=5),
        },
        "attacker-controlled-secret",
        algorithm="HS256",
        headers={"kid": "shared-kid"},
    )
    with pytest.raises(IdentityVerificationError):
        await verifier.verify(confused)
    assert trusted_private is not None


async def test_new_kid_forces_refresh_and_supports_key_rotation() -> None:
    old_private, old_jwk = signing_material("old-key")
    new_private, new_jwk = signing_material("new-key")
    fetcher = StubFetcher({"keys": [old_jwk]}, {"keys": [old_jwk, new_jwk]})
    verifier = Auth0IdentityVerifier(settings(), fetcher)

    await verifier.verify(token(old_private, "old-key"))
    rotated = await verifier.verify(token(new_private, "new-key"))

    assert rotated.subject == "auth0|user-123"
    assert fetcher.calls == 2


async def test_unknown_kid_and_jwks_outage_fail_closed() -> None:
    private_key, jwk = signing_material("known-key")
    unknown_verifier = Auth0IdentityVerifier(
        settings(), StubFetcher({"keys": [jwk]}, {"keys": [jwk]})
    )
    with pytest.raises(IdentityVerificationError):
        await unknown_verifier.verify(token(private_key, "unknown-key"))

    unavailable = Auth0IdentityVerifier(settings(), StubFetcher(RuntimeError("offline")))
    with pytest.raises(IdentityVerificationError):
        await unavailable.verify(token(private_key, "known-key"))


async def test_unknown_kid_forced_refresh_is_rate_limited() -> None:
    private_key, jwk = signing_material("known-key")
    fetcher = StubFetcher({"keys": [jwk]}, {"keys": [jwk]}, {"keys": [jwk]})
    verifier = Auth0IdentityVerifier(settings(), fetcher)
    await verifier.verify(token(private_key, "known-key"))

    with pytest.raises(IdentityVerificationError):
        await verifier.verify(token(private_key, "random-key-one"))
    with pytest.raises(IdentityVerificationError):
        await verifier.verify(token(private_key, "random-key-two"))
    assert fetcher.calls == 2


async def test_expired_jwks_cache_is_not_trusted_indefinitely() -> None:
    private_key, jwk = signing_material("cached-key")
    fetcher = StubFetcher({"keys": [jwk]}, RuntimeError("offline"))
    verifier = Auth0IdentityVerifier(settings(), fetcher)
    await verifier.verify(token(private_key, "cached-key"))

    verifier._jwks._expires_at = 0.0
    with pytest.raises(IdentityVerificationError):
        await verifier.verify(token(private_key, "cached-key"))
