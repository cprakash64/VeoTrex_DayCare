"""The ``/v1/edge`` namespace: machine-authenticated routes for edge nodes (V1-DEMO-03B).

These routes accept ONLY an edge machine credential (``EdgePrincipalDependency``) and the human
routes accept only an Auth0 identity, so neither credential opens the other's surface.

``POST /v1/edge/cameras/{camera_id}/whep`` behaves like a WHEP endpoint, so the edge's existing
WHEP client consumes it unchanged in shape: ``Content-Type: application/sdp`` offer in,
``201 Created`` with an ``application/sdp`` answer and a ``Location`` out. That Location is an
opaque VeoTrex lease; Ring's session resource never appears. The body limit and media type are
enforced by the request middleware before this code runs, and authentication runs before the
camera id is even parsed, so an unauthenticated caller learns nothing from a malformed id.

V1-05B adds two more machine routes, equally narrow: ``GET /v1/edge/runtime-config`` (the portals
of this node's own assigned cameras, see ``edge_runtime``) and ``POST
/v1/edge/events/room-transitions`` (anonymous room entry/exit events, idempotent per event id).
Neither takes a node or tenant as input; both answer only for the authenticated credential.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from veotrex_api.access import AuthenticationFailureLogLimiter
from veotrex_api.edge_auth import EdgePrincipalDependency
from veotrex_api.edge_runtime import (
    MAX_EVENTS_PER_BATCH,
    EdgeRuntimeService,
    EdgeRuntimeUnavailable,
    RoomTransitionInput,
    canonical_json,
)
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


# ------------------------------------------------ runtime config and room events (V1-05B)
EDGE_EVENTS_PATH = "/v1/edge/events/room-transitions"
_STREAM_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


def is_edge_event_upload(method: str, path: str) -> bool:
    """The route that carries an event batch, matched for the body-limit guard."""
    return method == "POST" and path == EDGE_EVENTS_PATH


class RoomTransitionEventRequest(BaseModel):
    """One anonymous crossing. Ids, a type, a time and geometry - no person, no image."""

    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    camera_id: UUID
    portal_id: UUID
    event_type: Literal["PERSON_ENTERED_ROOM", "PERSON_EXITED_ROOM"]
    occurred_at: AwareDatetime
    ephemeral_track_id: int = Field(strict=True, ge=1, le=2_147_483_647)
    stream_instance_id: str = Field(pattern=_STREAM_ID_PATTERN)
    crossing_x: float = Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)
    crossing_y: float = Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_observations: int = Field(strict=True, ge=1, le=100)


class RoomTransitionBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[RoomTransitionEventRequest] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


class RoomTransitionResult(BaseModel):
    event_id: str
    status: Literal["ACCEPTED", "DUPLICATE", "REJECTED"]
    category: str | None = None


class RoomTransitionBatchResponse(BaseModel):
    results: list[RoomTransitionResult]


def register_edge_runtime_routes(
    app: FastAPI, service: EdgeRuntimeService, limiter: AuthenticationFailureLogLimiter
) -> None:
    @app.get("/v1/edge/runtime-config")
    async def edge_runtime_config(principal: EdgePrincipalDependency) -> Response:
        """Portal configuration for this node's own assigned cameras. Nothing is a parameter:
        the node is the authenticated credential."""
        if not limiter.allow():
            raise HTTPException(status_code=429, detail=_PUBLIC_DETAIL[429])
        try:
            document = await service.runtime_config(principal)
        except EdgeRuntimeUnavailable:
            raise HTTPException(status_code=503, detail=_PUBLIC_DETAIL[503]) from None
        return Response(
            content=canonical_json(document),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @app.post(EDGE_EVENTS_PATH, response_model=RoomTransitionBatchResponse)
    async def edge_room_transitions(
        payload: RoomTransitionBatchRequest, principal: EdgePrincipalDependency
    ) -> RoomTransitionBatchResponse:
        if not limiter.allow():
            raise HTTPException(status_code=429, detail=_PUBLIC_DETAIL[429])
        try:
            results = await service.ingest(
                principal,
                [
                    RoomTransitionInput(
                        event_id=item.event_id,
                        camera_id=item.camera_id,
                        portal_id=item.portal_id,
                        event_type=item.event_type,
                        occurred_at=item.occurred_at,
                        ephemeral_track_id=item.ephemeral_track_id,
                        stream_instance_id=item.stream_instance_id,
                        crossing_x=item.crossing_x,
                        crossing_y=item.crossing_y,
                        evidence_observations=item.evidence_observations,
                    )
                    for item in payload.events
                ],
            )
        except EdgeRuntimeUnavailable:
            raise HTTPException(status_code=503, detail=_PUBLIC_DETAIL[503]) from None
        return RoomTransitionBatchResponse(
            results=[
                RoomTransitionResult(
                    event_id=str(result.event_id), status=result.status, category=result.category
                )
                for result in results
            ]
        )
