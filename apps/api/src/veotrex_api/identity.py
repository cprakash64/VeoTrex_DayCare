import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

import httpx
import jwt

from veotrex_api.config import Settings

AUTH0_PROVIDER = "auth0"


class IdentityVerificationError(Exception):
    """An access token could not be authenticated without exposing validation details."""


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    provider: str
    issuer: str
    subject: str
    external_organization_id: str


class IdentityVerifier(Protocol):
    async def verify(self, token: str) -> ExternalIdentity: ...


class JWKSetFetcher(Protocol):
    async def fetch(self) -> Mapping[str, Any]: ...


class HTTPJWKSetFetcher:
    def __init__(self, jwks_url: str, timeout_seconds: float) -> None:
        self._jwks_url = jwks_url
        self._timeout_seconds = timeout_seconds

    async def fetch(self) -> Mapping[str, Any]:
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.get(self._jwks_url)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise IdentityVerificationError("invalid JWKS response")
        return payload


class CachedJWKSet:
    def __init__(
        self, fetcher: JWKSetFetcher, ttl_seconds: int, forced_refresh_cooldown_seconds: int
    ) -> None:
        self._fetcher = fetcher
        self._ttl_seconds = ttl_seconds
        self._keys: dict[str, Mapping[str, Any]] = {}
        self._expires_at = 0.0
        self._last_forced_refresh_at = 0.0
        self._forced_refresh_cooldown_seconds = forced_refresh_cooldown_seconds
        self._lock = asyncio.Lock()

    @staticmethod
    def _index(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list):
            raise IdentityVerificationError("invalid JWKS response")
        indexed: dict[str, Mapping[str, Any]] = {}
        for key in raw_keys:
            if isinstance(key, dict) and isinstance(key.get("kid"), str):
                indexed[key["kid"]] = key
        if not indexed:
            raise IdentityVerificationError("JWKS contains no signing keys")
        return indexed

    async def _refresh(self) -> None:
        try:
            payload = await self._fetcher.fetch()
            keys = self._index(payload)
        except IdentityVerificationError:
            raise
        except Exception as exc:
            raise IdentityVerificationError("JWKS unavailable") from exc
        self._keys = keys
        self._expires_at = monotonic() + self._ttl_seconds

    async def get(self, kid: str) -> Mapping[str, Any]:
        async with self._lock:
            now = monotonic()
            if not self._keys or now >= self._expires_at:
                await self._refresh()
            key = self._keys.get(kid)
            if key is None:
                # A new kid is the normal rotation signal. Refresh immediately, even
                # while the cache is otherwise fresh. Throttle subsequent forced
                # refreshes so random-kid traffic cannot amplify requests to Auth0.
                if now < self._last_forced_refresh_at + self._forced_refresh_cooldown_seconds:
                    raise IdentityVerificationError("unknown signing key")
                self._last_forced_refresh_at = now
                await self._refresh()
                key = self._keys.get(kid)
            if key is None:
                raise IdentityVerificationError("unknown signing key")
            return key


class Auth0IdentityVerifier:
    def __init__(self, settings: Settings, fetcher: JWKSetFetcher | None = None) -> None:
        self._issuer = settings.oidc_issuer
        self._audience = settings.oidc_audience
        self._algorithms = settings.oidc_algorithms
        self._leeway_seconds = settings.oidc_clock_skew_seconds
        jwks_url = f"{self._issuer.rstrip('/')}/.well-known/jwks.json"
        resolved_fetcher = fetcher or HTTPJWKSetFetcher(
            jwks_url, settings.oidc_http_timeout_seconds
        )
        self._jwks = CachedJWKSet(
            resolved_fetcher,
            settings.oidc_jwks_cache_ttl_seconds,
            settings.oidc_jwks_forced_refresh_cooldown_seconds,
        )

    async def verify(self, token: str) -> ExternalIdentity:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = header.get("kid")
            if algorithm not in self._algorithms or not isinstance(kid, str) or not kid:
                raise IdentityVerificationError("token header rejected")
            jwk = await self._jwks.get(kid)
            signing_key = jwt.PyJWK.from_dict(dict(jwk), algorithm=algorithm).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={"require": ["exp", "iss", "aud", "sub", "org_id"]},
            )
            subject = claims["sub"]
            organization = claims["org_id"]
            if not isinstance(subject, str) or not subject:
                raise IdentityVerificationError("subject missing")
            if not isinstance(organization, str) or not organization:
                raise IdentityVerificationError("organization missing")
        except IdentityVerificationError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise IdentityVerificationError("token rejected") from exc
        return ExternalIdentity(
            provider=AUTH0_PROVIDER,
            issuer=self._issuer,
            subject=subject,
            external_organization_id=organization,
        )
