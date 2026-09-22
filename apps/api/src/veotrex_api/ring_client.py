import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
import structlog
from pydantic import SecretStr
from socksio.exceptions import SOCKSError

from veotrex_api.config import Settings
from veotrex_api.ring_inventory import (
    NormalizedComponent,
    NormalizedDevice,
    RingInventoryDocumentError,
    parse_configuration_document,
    parse_inventory_page,
)
from veotrex_api.secrets import SecretResolver

# A request that dies inside the optional Ring API proxy never reached Ring, so it is the same
# outcome as any other transport failure. HTTPCORE's own proxy errors already arrive as
# httpx.ProxyError, but a malformed SOCKS reply surfaces as a raw socksio error that would
# otherwise escape this client's error contract. socksio is a declared dependency (httpx[socks]).
_TRANSPORT_FAILURES = (httpx.TimeoutException, httpx.TransportError, SOCKSError)

# The documented Partner API request contract (developer.amazon.com/docs/ring, re-read
# 2026-09-22): every JSON API example - curl, JavaScript and Python, GET included - sends
# ``Authorization: Bearer <token>`` and ``Content-Type: application/json``; the reference states
# "Content Types - JSON APIs: application/json". These are the only media types this client
# ever declares. Media (SDP, MP4, JPEG) endpoints are not used.
JSON_MEDIA_TYPE = "application/json"
# A product token per RFC 9110 rather than the HTTP library's default. Bounded ASCII.
USER_AGENT_PRODUCT = "VeoTrex-ControlPlane"
# Safe diagnostics captured from a provider failure: a JSON:API error title/code (short,
# printable ASCII) and a correlation header if the gateway supplies one. Never the body.
_ERROR_TITLE_MAX = 80
_REQUEST_ID_MAX = 128
_REQUEST_ID_HEADERS = ("x-request-id", "x-amzn-requestid", "x-amz-request-id")


class RingClientError(Exception):
    def __init__(
        self,
        operation: str,
        category: str,
        status_code: int | None = None,
        *,
        error_title: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(f"Ring operation failed: {operation}/{category}")
        self.operation = operation
        self.category = category
        self.status_code = status_code
        # Safe, bounded provider diagnostics (see ``safe_error_summary``); never a secret.
        self.error_title = error_title
        self.provider_request_id = provider_request_id


class RingAmbiguousResult(RingClientError):
    pass


@dataclass(frozen=True, slots=True)
class RingTokenSet:
    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int
    scopes: tuple[str, ...]


def user_agent(app_version: str) -> str:
    """``VeoTrex-ControlPlane/<version>``: printable ASCII, bounded, no host or secret."""
    version = "".join(
        ch for ch in app_version if 33 <= ord(ch) <= 126 and ch not in '()<>@,;:\\"/[]?={}'
    )
    return f"{USER_AGENT_PRODUCT}/{version[:32] or 'unknown'}"


def safe_error_summary(response: httpx.Response) -> tuple[str | None, str | None]:
    """The only two things ever kept from a failed provider response.

    ``title``: ``errors[0].title`` (or ``code``) of a JSON:API error document, restricted to
    printable ASCII and bounded; anything else, including a body that is not an error document,
    yields None. ``request_id``: the first correlation header Ring's gateway supplies, bounded.
    Tokens, nonces, account ids and profile attributes never appear in either field.
    """
    title: str | None = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        errors = payload.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else None
        if isinstance(first, dict):
            for key in ("title", "code"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    title = "".join(ch for ch in value if 32 <= ord(ch) <= 126)[:_ERROR_TITLE_MAX]
                    break
    request_id: str | None = None
    for header in _REQUEST_ID_HEADERS:
        value = response.headers.get(header)
        if value:
            request_id = "".join(ch for ch in value if 33 <= ord(ch) <= 126)[:_REQUEST_ID_MAX]
            break
    return title or None, request_id or None


class RingClient:
    def __init__(
        self,
        settings: Settings,
        secret_resolver: SecretResolver,
        http_client: httpx.AsyncClient | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
        api_http_client: httpx.AsyncClient | None = None,
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
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        self._default_headers = {"User-Agent": user_agent(settings.app_version)}
        self._http = http_client or httpx.AsyncClient(
            timeout=timeout, limits=limits, headers=self._default_headers
        )
        # Requests to the configured Ring API origin may leave through a separate client so an
        # optional egress proxy applies to them and to nothing else - Ring OAuth token exchange
        # keeps using ``self._http`` and stays direct. With no proxy configured the two names
        # refer to one client, which is exactly the single-pool behaviour that existed before.
        # An injected client is always honoured as-is for every request: it is the caller's
        # explicit transport, and re-routing part of it through a proxy would defeat that.
        self._api_http = self._http
        self._owns_api_client = False
        if api_http_client is not None:
            self._api_http = api_http_client
        elif http_client is None and settings.ring_api_proxy_url:
            self._api_http = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                proxy=settings.ring_api_proxy_url,
                headers=self._default_headers,
            )
            self._owns_api_client = True

    async def aclose(self) -> None:
        if self._owns_api_client:
            await self._api_http.aclose()
        if self._owns_client:
            await self._http.aclose()

    def _client_for(self, url: str) -> httpx.AsyncClient:
        """Pick the transport for ``url``: the Ring API client only for the Ring API origin.

        Routing on the resolved origin rather than on the call site means a future Ring API
        call cannot forget to opt in, and a non-Ring URL cannot accidentally opt in either.
        """
        if self._api_http is self._http:
            return self._http
        parsed = urlparse(url)
        if (parsed.scheme, parsed.hostname or "", parsed.port) == self._expected_api_origin():
            return self._api_http
        return self._http

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
            response = await self._client_for(url).request(
                method, url, headers=headers, data=data, json=json_body
            )
        except _TRANSPORT_FAILURES as exc:
            error_type = RingAmbiguousResult if ambiguous_on_transport_failure else RingClientError
            raise error_type(operation, "transport_failure") from exc
        if len(response.content) > self._settings.ring_max_response_bytes:
            raise self._failure(operation, "response_too_large", response)
        if response.status_code == 429:
            raise self._failure(operation, "rate_limited", response)
        if 400 <= response.status_code < 500:
            raise self._failure(operation, "provider_rejected", response)
        if response.status_code >= 500:
            error_type = RingAmbiguousResult if ambiguous_on_transport_failure else RingClientError
            raise self._failure(operation, "provider_unavailable", response, error_type)
        if response.status_code != 200:
            raise self._failure(operation, "unexpected_status", response)
        return response

    @staticmethod
    def _failure(
        operation: str,
        category: str,
        response: httpx.Response,
        error_type: type[RingClientError] = RingClientError,
    ) -> RingClientError:
        """Build the error for a non-2xx provider response and log its safe summary.

        The log event carries the operation, category, HTTP status, the JSON:API error title or
        code when Ring supplies one, and a gateway correlation id. It never carries the request
        or response headers, the body, a token, a nonce, an account id or any profile field.
        """
        title, request_id = safe_error_summary(response)
        structlog.get_logger().warning(
            "ring_provider_request_failed",
            operation=operation,
            category=category,
            status_code=response.status_code,
            error_title=title,
            provider_request_id=request_id,
        )
        return error_type(
            operation,
            category,
            response.status_code,
            error_title=title,
            provider_request_id=request_id,
        )

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
        """Exactly the documented JSON API request headers, for GET and for JSON bodies alike.

        The Authorization value is the raw token text after ``Bearer `` - no quoting, escaping,
        masking or whitespace. Accept and Content-Type are the reference's single JSON media
        type; the reference's own GET examples send Content-Type as well.
        """
        return {
            "Authorization": f"Bearer {access_token.get_secret_value()}",
            "Accept": JSON_MEDIA_TYPE,
            "Content-Type": JSON_MEDIA_TYPE,
        }

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
                    response = await self._client_for(url).get(url, headers=self._bearer(token))
                self.rate_limit_limit = self._nonnegative_header(response, "x-ratelimit-limit")
                self.rate_limit_remaining = self._nonnegative_header(
                    response, "x-ratelimit-remaining"
                )
            except _TRANSPORT_FAILURES as exc:
                if attempt + 1 == attempts:
                    raise RingClientError(operation, "transport_failure") from exc
            else:
                if len(response.content) > self._settings.ring_max_response_bytes:
                    raise self._failure(operation, "response_too_large", response)
                if response.status_code == 200:
                    return self._json_object(response, operation)
                if response.status_code in {401, 403, 404}:
                    category = {401: "unauthorized", 403: "forbidden", 404: "not_found"}[
                        response.status_code
                    ]
                    raise self._failure(operation, category, response)
                if response.status_code != 429 and response.status_code < 500:
                    raise self._failure(operation, "provider_rejected", response)
                if attempt + 1 == attempts:
                    category = (
                        "rate_limited" if response.status_code == 429 else "provider_unavailable"
                    )
                    raise self._failure(operation, category, response)
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

    async def confirm_app_integration(
        self, access_token: SecretStr, nonce: str, account_identifier: str | None = None
    ) -> None:
        """POST /v1/accounts/me/app-integrations: ``nonce`` (required) plus the optional
        obfuscated partner account identifier Ring shows the user. Expects ``awaiting``."""
        operation = "app_integration_post"
        body: dict[str, str] = {"nonce": nonce}
        if account_identifier:
            body = {"account_identifier": account_identifier, "nonce": nonce}
        response = await self._request(
            operation,
            "POST",
            f"{self._settings.ring_api_base_url.rstrip('/')}/v1/accounts/me/app-integrations",
            ambiguous_on_transport_failure=True,
            headers=self._bearer(access_token),
            json_body=body,
        )
        self._require_integration_status(response, operation, "awaiting")

    async def complete_app_integration(
        self, access_token: SecretStr, account_identifier: str | None = None
    ) -> None:
        """PATCH /v1/accounts/me/app-integrations with ``status: completed`` (mandatory after
        POST) and the same obfuscated identifier. Expects ``completed``."""
        operation = "app_integration_patch"
        body: dict[str, str] = {"status": "completed"}
        if account_identifier:
            body = {"account_identifier": account_identifier, "status": "completed"}
        response = await self._request(
            operation,
            "PATCH",
            f"{self._settings.ring_api_base_url.rstrip('/')}/v1/accounts/me/app-integrations",
            ambiguous_on_transport_failure=True,
            headers=self._bearer(access_token),
            json_body=body,
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
