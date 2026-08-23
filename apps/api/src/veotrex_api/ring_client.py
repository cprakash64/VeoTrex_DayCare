from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.secrets import SecretResolver


class RingClientError(Exception):
    def __init__(self, operation: str, category: str, status_code: int | None = None) -> None:
        super().__init__(f"Ring operation failed: {operation}/{category}")
        self.operation = operation
        self.category = category
        self.status_code = status_code


class RingAmbiguousResult(RingClientError):
    pass


@dataclass(frozen=True, slots=True)
class RingTokenSet:
    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int
    scopes: tuple[str, ...]


class RingClient:
    def __init__(
        self,
        settings: Settings,
        secret_resolver: SecretResolver,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secret_resolver
        self._owns_client = http_client is None
        timeout = httpx.Timeout(
            connect=settings.ring_connect_timeout_seconds,
            read=settings.ring_read_timeout_seconds,
            write=settings.ring_write_timeout_seconds,
            pool=settings.ring_connect_timeout_seconds,
        )
        self._http = http_client or httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def _client_secret(self) -> str:
        return self._secrets.resolve(self._settings.ring_client_secret_ref).get_secret_value()

    async def _request(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        ambiguous_on_transport_failure: bool,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        json_body: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(
                method, url, headers=headers, data=data, json=json_body
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            error_type = RingAmbiguousResult if ambiguous_on_transport_failure else RingClientError
            raise error_type(operation, "transport_failure") from exc
        if len(response.content) > self._settings.ring_max_response_bytes:
            raise RingClientError(operation, "response_too_large", response.status_code)
        if response.status_code == 429:
            raise RingClientError(operation, "rate_limited", 429)
        if 400 <= response.status_code < 500:
            raise RingClientError(operation, "provider_rejected", response.status_code)
        if response.status_code >= 500:
            error_type = RingAmbiguousResult if ambiguous_on_transport_failure else RingClientError
            raise error_type(operation, "provider_unavailable", response.status_code)
        if response.status_code != 200:
            raise RingClientError(operation, "unexpected_status", response.status_code)
        return response

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RingClientError(operation, "malformed_response") from exc
        if not isinstance(payload, dict):
            raise RingClientError(operation, "malformed_response")
        return payload

    def _parse_token(self, response: httpx.Response, operation: str) -> RingTokenSet:
        payload = self._json_object(response, operation)
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type")
        expires_in = payload.get("expires_in")
        raw_scope = payload.get("scope", "")
        if not isinstance(access_token, str) or not access_token:
            raise RingClientError(operation, "malformed_token_response")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RingClientError(operation, "malformed_token_response")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise RingClientError(operation, "malformed_token_response")
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 1 <= expires_in <= self._settings.ring_max_access_token_lifetime_seconds
        ):
            raise RingClientError(operation, "malformed_token_response")
        if isinstance(raw_scope, str):
            scopes = tuple(scope for scope in raw_scope.split() if scope)
        elif isinstance(raw_scope, list) and all(isinstance(scope, str) for scope in raw_scope):
            scopes = tuple(str(scope) for scope in raw_scope)
        else:
            raise RingClientError(operation, "malformed_token_response")
        return RingTokenSet(
            access_token=SecretStr(access_token),
            refresh_token=SecretStr(refresh_token),
            expires_in=expires_in,
            scopes=scopes,
        )

    async def exchange_authorization_code(self, code: SecretStr) -> RingTokenSet:
        operation = "authorization_code_exchange"
        response = await self._request(
            operation,
            "POST",
            self._settings.ring_oauth_token_url,
            ambiguous_on_transport_failure=True,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "client_id": self._settings.ring_client_id,
                "code": code.get_secret_value(),
                "client_secret": self._client_secret(),
            },
        )
        return self._parse_token(response, operation)

    async def refresh(self, refresh_token: SecretStr) -> RingTokenSet:
        operation = "refresh_token_exchange"
        response = await self._request(
            operation,
            "POST",
            self._settings.ring_oauth_token_url,
            ambiguous_on_transport_failure=True,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.get_secret_value(),
                "client_id": self._settings.ring_client_id,
                "client_secret": self._client_secret(),
            },
        )
        return self._parse_token(response, operation)

    @staticmethod
    def _bearer(access_token: SecretStr) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token.get_secret_value()}"}

    async def get_account_id(self, access_token: SecretStr) -> str:
        operation = "users_me"
        response = await self._request(
            operation,
            "GET",
            f"{self._settings.ring_api_base_url.rstrip('/')}/v1/users/me",
            ambiguous_on_transport_failure=False,
            headers=self._bearer(access_token),
        )
        payload = self._json_object(response, operation)
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("type") != "users":
            raise RingClientError(operation, "malformed_response")
        account_id = data.get("id")
        if not isinstance(account_id, str) or not account_id or len(account_id) > 512:
            raise RingClientError(operation, "malformed_response")
        return account_id

    async def confirm_app_integration(self, access_token: SecretStr, nonce: str) -> None:
        operation = "app_integration_post"
        response = await self._request(
            operation,
            "POST",
            f"{self._settings.ring_api_base_url.rstrip('/')}/v1/accounts/me/app-integrations",
            ambiguous_on_transport_failure=True,
            headers=self._bearer(access_token),
            json_body={"nonce": nonce},
        )
        self._require_integration_status(response, operation, "awaiting")

    async def complete_app_integration(self, access_token: SecretStr) -> None:
        operation = "app_integration_patch"
        response = await self._request(
            operation,
            "PATCH",
            f"{self._settings.ring_api_base_url.rstrip('/')}/v1/accounts/me/app-integrations",
            ambiguous_on_transport_failure=True,
            headers=self._bearer(access_token),
            json_body={"status": "completed"},
        )
        self._require_integration_status(response, operation, "completed")

    def _require_integration_status(
        self, response: httpx.Response, operation: str, expected: str
    ) -> None:
        payload = self._json_object(response, operation)
        data = payload.get("data")
        attributes = data.get("attributes") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("type") != "app-integrations"
            or not isinstance(attributes, dict)
            or attributes.get("status") != expected
        ):
            raise RingClientError(operation, "malformed_response")
