"""The ``/v1/edge`` namespace: machine-authenticated routes for edge nodes (V1-DEMO-03B).

These routes accept ONLY an edge machine credential (``EdgePrincipalDependency``) and the human
routes accept only an Auth0 identity, so neither credential opens the other's surface.

``POST /v1/edge/cameras/{camera_id}/whep`` behaves like a WHEP endpoint, so the edge's existing
WHEP client consumes it unchanged in shape: ``Content-Type: application/sdp`` offer in,
``201 Created`` with an ``application/sdp`` answer and a ``Location`` out. That Location is an
opaque VeoTrex lease; Ring's session resource never appears. The body limit and media type are
enforced by the request middleware before this code runs, and authentication runs before the
camera id is even parsed, so an unauthenticated caller learns nothing from a malformed id.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status

from veotrex_api.access import AuthenticationFailureLogLimiter
from veotrex_api.edge_auth import EdgePrincipalDependency
from veotrex_api.edge_whep import EdgeBrokerError, EdgeWhepBroker
from veotrex_api.ring_client import SDP_MEDIA_TYPE

EDGE_WHEP_PREFIX = "/v1/edge/cameras/"
EDGE_WHEP_SUFFIX = "/whep"
_PUBLIC_DETAIL = {
    400: "invalid offer",
    403: "provider refused the session",
    404: "not found",
    429: "request limit exceeded",
    502: "provider request failed",
    503: "service unavailable",
}


def is_edge_whep_offer(method: str, path: str) -> bool:
    """The route that carries a raw SDP body, matched for the body-limit guard."""
    return (
        method == "POST" and path.startswith(EDGE_WHEP_PREFIX) and path.endswith(EDGE_WHEP_SUFFIX)
    )


def _http_error(exc: EdgeBrokerError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code, detail=_PUBLIC_DETAIL.get(exc.status_code, "request failed")
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_PUBLIC_DETAIL[404])


def register_edge_routes(
    app: FastAPI, broker: EdgeWhepBroker, session_limiter: AuthenticationFailureLogLimiter
) -> None:
    @app.post("/v1/edge/cameras/{camera_id}/whep", status_code=status.HTTP_201_CREATED)
    async def edge_whep_offer(
        camera_id: str, request: Request, principal: EdgePrincipalDependency
    ) -> Response:
        try:
            parsed_camera_id = UUID(camera_id)
        except ValueError:
            raise _not_found() from None
        if str(parsed_camera_id) != camera_id.lower():
            raise _not_found()
        if not session_limiter.allow():
            raise HTTPException(status_code=429, detail=_PUBLIC_DETAIL[429])
        try:
            lease, answer = await broker.open_session(
                principal, parsed_camera_id, await request.body()
            )
        except EdgeBrokerError as exc:
            raise _http_error(exc) from None
        return Response(
            content=answer.encode("utf-8"),
            status_code=status.HTTP_201_CREATED,
            media_type=SDP_MEDIA_TYPE,
            headers={"Location": lease.location, "Cache-Control": "no-store"},
        )

    @app.delete("/v1/edge/whep-leases/{lease_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def edge_whep_release(lease_id: str, principal: EdgePrincipalDependency) -> Response:
        try:
            await broker.close_session(principal, lease_id)
        except EdgeBrokerError as exc:
            raise _http_error(exc) from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)
