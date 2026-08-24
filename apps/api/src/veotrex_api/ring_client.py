import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
from pydantic import SecretStr

from veotrex_api.config import Settings
from veotrex_api.ring_inventory import (
    NormalizedComponent,
    NormalizedDevice,
    RingInventoryDocumentError,
    parse_configuration_document,
    parse_inventory_page,
)
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
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._secrets = secret_resolver
        self._owns_client = http_client is None
        self._sleeper = sleeper
        self._random = random_value
        self._read_gate = asyncio.Lock()
        self.rate_limit_limit: int | None = None
        self.rate_limit_remaining: int | None = None
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

    @staticmethod
    def _retry_after(response: httpx.Response, maximum: float) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return min(maximum, max(0.0, value))

    @staticmethod
    def _nonnegative_header(response: httpx.Response, name: str) -> int | None:
        raw = response.headers.get(name)
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None

    async def _safe_inventory_get(self, operation: str, url: str, token: SecretStr) -> object:
        attempts = self._settings.ring_inventory_retry_attempts
        for attempt in range(attempts):
            response: httpx.Response | None = None
            try:
                # This gate is deliberately process-wide for this Ring client: the documented
                # quota belongs to the partner client_id, not to an individual tenant.
                async with self._read_gate:
                    response = await self._http.get(url, headers=self._bearer(token))
                self.rate_limit_limit = self._nonnegative_header(response, "x-ratelimit-limit")
                self.rate_limit_remaining = self._nonnegative_header(
                    response, "x-ratelimit-remaining"
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 == attempts:
                    raise RingClientError(operation, "transport_failure") from exc
            else:
                if len(response.content) > self._settings.ring_max_response_bytes:
                    raise RingClientError(operation, "response_too_large", response.status_code)
                if response.status_code == 200:
                    return self._json_object(response, operation)
                if response.status_code in {401, 403, 404}:
                    category = {401: "unauthorized", 403: "forbidden", 404: "not_found"}[
                        response.status_code
                    ]
                    raise RingClientError(operation, category, response.status_code)
                if response.status_code != 429 and response.status_code < 500:
                    raise RingClientError(operation, "provider_rejected", response.status_code)
                if attempt + 1 == attempts:
                    category = (
                        "rate_limited" if response.status_code == 429 else "provider_unavailable"
                    )
                    raise RingClientError(operation, category, response.status_code)
            delay = min(
                self._settings.ring_inventory_backoff_max_seconds,
                (2**attempt) + self._random(),
            )
            if response is not None and response.status_code == 429:
                retry_after = self._retry_after(
                    response, self._settings.ring_inventory_backoff_max_seconds
                )
                if retry_after is not None:
                    delay = retry_after
            await self._sleeper(delay)
        raise AssertionError("bounded retry loop exhausted")

    def _expected_api_origin(self) -> tuple[str, str, int | None]:
        parsed = urlparse(self._settings.ring_api_base_url)
        return parsed.scheme, parsed.hostname or "", parsed.port

    def _safe_next(self, current: str, value: str) -> str:
        target = urljoin(current, value)
        parsed = urlparse(target)
        if (parsed.scheme, parsed.hostname or "", parsed.port) != self._expected_api_origin():
            raise RingClientError("device_discovery", "unsafe_pagination_link")
        return target

    async def discover_devices(self, access_token: SecretStr) -> tuple[NormalizedDevice, ...]:
        base = self._settings.ring_api_base_url.rstrip("/")
        next_url: str | None = (
            f"{base}/v1/devices?include=status,capabilities,location,configurations"
        )
        seen_links: set[str] = set()
        devices: list[NormalizedDevice] = []
        for _ in range(self._settings.ring_inventory_max_pages):
            if next_url is None:
                break
            if next_url in seen_links:
                raise RingClientError("device_discovery", "pagination_loop")
            seen_links.add(next_url)
            payload = await self._safe_inventory_get("device_discovery", next_url, access_token)
            try:
                page = parse_inventory_page(payload)
            except RingInventoryDocumentError as exc:
                raise RingClientError("device_discovery", "malformed_response") from exc
            devices.extend(page.devices)
            if len(devices) > self._settings.ring_inventory_max_devices:
                raise RingClientError("device_discovery", "device_limit_exceeded")
            next_url = self._safe_next(next_url, page.next_link) if page.next_link else None
        if next_url is not None:
            raise RingClientError("device_discovery", "page_limit_exceeded")
        return await self._enrich_component_configurations(access_token, tuple(devices))

    async def get_device(self, access_token: SecretStr, device_id: str) -> NormalizedDevice:
        if not device_id or len(device_id) > 512:
            raise RingClientError("device_detail", "invalid_device_id")
        encoded = quote(device_id, safe="")
        url = (
            f"{self._settings.ring_api_base_url.rstrip('/')}/v1/devices/{encoded}"
            "?include=status,capabilities,location,configurations"
        )
        payload = await self._safe_inventory_get("device_detail", url, access_token)
        try:
            page = parse_inventory_page(payload)
        except RingInventoryDocumentError as exc:
            raise RingClientError("device_detail", "malformed_response") from exc
        if len(page.devices) != 1 or page.devices[0].provider_device_id != device_id:
            raise RingClientError("device_detail", "malformed_response")
        enriched = await self._enrich_component_configurations(access_token, page.devices)
        return enriched[0]

    async def _enrich_component_configurations(
        self, access_token: SecretStr, devices: tuple[NormalizedDevice, ...]
    ) -> tuple[NormalizedDevice, ...]:
        required_reads = sum(
            len(device.components)
            for device in devices
            if device.components[0].provider_component_id
        )
        if required_reads > self._settings.ring_inventory_max_component_reads:
            raise RingClientError("component_configuration", "component_read_limit_exceeded")
        enriched_devices: list[NormalizedDevice] = []
        for device in devices:
            if device.components[0].provider_component_id is None:
                enriched_devices.append(device)
                continue
            components: list[NormalizedComponent] = []
            hashes: list[str] = []
            for component in device.components:
                assert component.provider_component_id is not None
                query = urlencode({"component_id": component.provider_component_id})
                encoded_device = quote(device.provider_device_id, safe="")
                url = (
                    f"{self._settings.ring_api_base_url.rstrip('/')}/v1/devices/"
                    f"{encoded_device}/configurations?{query}"
                )
                payload = await self._safe_inventory_get(
                    "component_configuration", url, access_token
                )
                try:
                    configuration = parse_configuration_document(payload)
                except RingInventoryDocumentError as exc:
                    raise RingClientError("component_configuration", "malformed_response") from exc
                hashes.append(configuration.configuration_sha256)
                components.append(
                    replace(
                        component,
                        privacy_zones_configured=configuration.privacy_zones_configured,
                        motion_zones_configured=configuration.motion_zones_configured,
                    )
                )
            enriched_devices.append(
                replace(
                    device,
                    configuration_sha256=hashlib.sha256("".join(hashes).encode()).hexdigest(),
                    components=tuple(components),
                )
            )
        return tuple(enriched_devices)

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
