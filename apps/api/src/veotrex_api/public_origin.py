"""Canonical public HTTPS origin and deterministic Ring callback URLs.

Ring validates the URLs configured in its developer portal against what it is sent, so those URLs
must be derived from one configured external origin plus constant application-owned routes. They
are never built from an incoming ``Host``/``X-Forwarded-Host`` header: a request-controlled origin
would let any caller move Ring's callbacks to a host they control.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_ORIGIN_LENGTH = 255
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

# Application-owned constants. Nothing caller-controlled ever contributes to these paths.
ACCOUNT_LINK_PATH = "/integrations/ring/link"
DEFAULT_REDIRECT_PATH = "/app/integrations/ring/devices"
TOKEN_EXCHANGE_PATH = "/v1/integrations/ring/token-exchange"  # noqa: S105 - a route, not a secret
WEBHOOK_PATH = "/v1/providers/ring/webhooks"


class InvalidPublicOrigin(ValueError):
    """The configured origin is not a usable public HTTPS origin."""


@dataclass(frozen=True, slots=True)
class PublicOrigin:
    scheme: str
    host: str
    port: int | None

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}" + (f":{self.port}" if self.port else "")

    def url_for(self, path: str) -> str:
        # Callbacks are built only from the module constants below. This guard is the safety net:
        # a path carrying a query, fragment, traversal, empty segment, or authority change must
        # fail closed rather than reach a URL that Ring will call.
        if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", path) or ".." in path:
            raise InvalidPublicOrigin("callback paths are fixed application constants")
        return f"{self.origin}{path}"

    @property
    def account_link_url(self) -> str:
        return self.url_for(ACCOUNT_LINK_PATH)

    @property
    def default_redirect_url(self) -> str:
        return self.url_for(DEFAULT_REDIRECT_PATH)

    @property
    def token_exchange_url(self) -> str:
        return self.url_for(TOKEN_EXCHANGE_PATH)

    @property
    def webhook_url(self) -> str:
        return self.url_for(WEBHOOK_PATH)

    def callback_urls(self) -> dict[str, str]:
        return {
            "account_link_url": self.account_link_url,
            "default_redirect_url": self.default_redirect_url,
            "token_exchange_url": self.token_exchange_url,
            "webhook_url": self.webhook_url,
        }


def validate_public_origin(raw: object) -> PublicOrigin:
    """Accept only a bare, public, HTTPS origin: scheme + host + optional port, nothing else."""
    if not isinstance(raw, str) or not 0 < len(raw) <= MAX_ORIGIN_LENGTH:
        raise InvalidPublicOrigin("origin must be a bounded string")
    if any(not 33 <= ord(character) <= 126 for character in raw):
        raise InvalidPublicOrigin("origin must be printable ASCII without whitespace")
    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise InvalidPublicOrigin("origin must use https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidPublicOrigin("origin must not contain credentials")
    if parts.path not in {"", "/"}:
        raise InvalidPublicOrigin("origin must not contain a path")
    if parts.query or "?" in raw:
        raise InvalidPublicOrigin("origin must not contain a query")
    if parts.fragment or "#" in raw:
        raise InvalidPublicOrigin("origin must not contain a fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidPublicOrigin("origin must contain a hostname")
    if "*" in host:
        raise InvalidPublicOrigin("wildcard hosts are not allowed")
    try:
        port = parts.port
    except ValueError:
        raise InvalidPublicOrigin("origin port is invalid") from None
    if port is not None and not 1 <= port <= 65_535:
        raise InvalidPublicOrigin("origin port is out of range")
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        # Ring must reach this origin from the public Internet, and a literal address cannot carry
        # a publicly trusted certificate for TLS the way a hostname does.
        raise InvalidPublicOrigin("origin must be a hostname, not an IP literal")
    if host in {"localhost", "localhost."} or host.endswith((".localhost", ".local", ".local.")):
        raise InvalidPublicOrigin("origin must not be a local hostname")
    labels = host.rstrip(".").split(".")
    if len(labels) < 2:
        raise InvalidPublicOrigin("origin must be a fully qualified hostname")
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise InvalidPublicOrigin("origin hostname is malformed")
    return PublicOrigin("https", host, port)


def callback_urls_for(raw: object) -> dict[str, str]:
    return validate_public_origin(raw).callback_urls()
