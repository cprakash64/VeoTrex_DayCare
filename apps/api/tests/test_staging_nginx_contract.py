"""The committed nginx templates must load on the ingress they target. No nginx needed.

The Hostinger VPS runs nginx 1.24.0. `http2 on;` is a separate directive only from nginx 1.25.1;
on 1.24.0 it is an unknown directive and `nginx -t` fails the WHOLE configuration, not just this
site. That shipped: installing the HTTPS template took the config from valid to invalid, and only
an explicit rollback kept the four existing sites on that shared host serving.

HTTP/2 must therefore be requested on the listen directive, which is what every other site on the
host already does.
"""

import re
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
NGINX_DIR = REPOSITORY / "infra" / "staging" / "hostinger" / "nginx"
TEMPLATES = sorted(NGINX_DIR.glob("*.conf"))
TARGET_NGINX = (1, 24, 0)

# Directive -> first nginx version that accepts it. Extend as templates grow.
POST_1_24_DIRECTIVES = {"http2": (1, 25, 1)}


def _active_lines(path: Path) -> list[str]:
    """Executable lines only: a comment naming a directive is not a use of it."""
    return [
        ln for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]


def test_templates_exist() -> None:
    assert TEMPLATES, "expected committed nginx templates to guard"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_directive_newer_than_the_target_ingress(template: Path) -> None:
    for line in _active_lines(template):
        directive = line.strip().split()[0].rstrip(";")
        introduced = POST_1_24_DIRECTIVES.get(directive)
        assert introduced is None or introduced <= TARGET_NGINX, (
            f"{template.name}: {directive!r} needs nginx {introduced}, target {TARGET_NGINX}; "
            "an unknown directive fails nginx -t for every site on the shared host"
        )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_tls_listeners_request_http2_inline(template: Path) -> None:
    tls_listens = [ln for ln in _active_lines(template) if re.search(r"^\s*listen\s+443\b", ln)]
    for line in tls_listens:
        assert "http2" in line, (
            f"{template.name}: TLS listener should enable http2 inline: {line.strip()}"
        )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_templates_never_claim_default_server(template: Path) -> None:
    """A default_server here would capture traffic belonging to the pre-existing sites."""
    for line in _active_lines(template):
        assert "default_server" not in line, f"{template.name}: {line.strip()}"
