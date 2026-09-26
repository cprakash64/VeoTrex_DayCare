"""Where a live run's doorway lines come from, and the managed edge session (V1-05B).

**Portal source precedence** (``resolve_portal_plan``, decided before any camera or GPU work):

* ``local`` / ``development`` / ``test`` / ``ci`` - exactly one of:
  ``--portal`` (static lines, local evaluation only, events stay on this device) or
  ``--managed-portals`` (the control plane's lines for one camera, events uploaded). Both at once
  is refused rather than merged, so there is never a question of which one won.
* every other environment (``staging``, ``production``, anything unrecognised) - ``--portal`` is
  refused outright; the control plane is the only source. Without ``--managed-portals`` the run
  simply has no doorway lines and reports no room events.

**The managed session** (``ManagedEdgeSession``) owns everything managed mode needs and nothing
else: one ``EdgeControlPlaneClient`` (the node's existing machine credential, re-read per
request - no second authentication mechanism), the last-known-good config cache, the durable
outbox, and the two bounded daemon threads (config refresher, event uploader). ``open`` does the
local I/O (private state directory, cache load, outbox open) and is called before the detector
starts; ``start`` stages the cached lines and starts the threads; ``close`` stops both threads
and closes the outbox, and is safe to call more than once or without ``start``.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from veotrex_edge_agent.edge_control_plane import (
    ConnectionFactory,
    ControlPlaneEndpoint,
    ControlPlaneError,
    EdgeControlPlaneClient,
    resolve_control_plane,
)
from veotrex_edge_agent.live.managed_config import (
    DEFAULT_REFRESH_SECONDS,
    MAX_REFRESH_SECONDS,
    MIN_REFRESH_SECONDS,
    ConfigCache,
    ConfigRefresher,
    ManagedConfigError,
    ManagedPortalConfig,
    ensure_private_directory,
)
from veotrex_edge_agent.live.portal_crossing import RoomTransition
from veotrex_edge_agent.live.portal_geometry import PortalSet
from veotrex_edge_agent.live.room_event_outbox import (
    DEFAULT_CAPACITY,
    MAX_CAPACITY,
    RoomEventOutbox,
    RoomEventUploader,
    event_payload,
)
from veotrex_edge_agent.recorded.yolox import EVALUATION_ENVIRONMENTS


class PortalPlanError(ValueError):
    """The portal options cannot be used together or here. ``category`` is fixed text."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class ManagedPlan:
    camera_id: str
    endpoint: ControlPlaneEndpoint
    credential_file: str
    state_dir: Path
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS
    outbox_capacity: int = DEFAULT_CAPACITY


@dataclass(frozen=True, slots=True)
class PortalPlan:
    """``mode`` is ``none``, ``static`` (``--portal``) or ``managed``."""

    mode: str
    managed: ManagedPlan | None = None


def resolve_portal_plan(
    *,
    environment: str,
    static_portals: bool,
    managed: bool,
    camera_id: str | None,
    control_plane_url: str | None,
    credential_file: str | None,
    state_dir: str | None,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    outbox_capacity: int = DEFAULT_CAPACITY,
) -> PortalPlan:
    """Validate every portal-source option. Reads the credential file's metadata and content
    once (to fail early), never keeps the value, and starts nothing."""
    if static_portals and environment not in EVALUATION_ENVIRONMENTS:
        raise PortalPlanError("portal_override_refused_in_this_environment")
    if static_portals and managed:
        raise PortalPlanError("portal_sources_conflict")
    if not managed:
        return PortalPlan("static" if static_portals else "none")
    if not camera_id:
        raise PortalPlanError("managed_camera_required")
    try:
        parsed = UUID(camera_id)
    except ValueError:
        raise PortalPlanError("managed_camera_must_be_a_veotrex_camera_uuid") from None
    if str(parsed) != camera_id:
        raise PortalPlanError("managed_camera_must_be_a_veotrex_camera_uuid")
    try:
        endpoint = resolve_control_plane(
            control_plane_url, credential_file, environment=environment
        )
    except ControlPlaneError as exc:
        raise PortalPlanError(exc.category) from None
    if credential_file is None:  # already refused above; narrows the type
        raise PortalPlanError("credential_file_not_configured")
    if not state_dir:
        raise PortalPlanError("state_dir_not_configured")
    if not Path(state_dir).is_absolute():
        raise PortalPlanError("state_dir_path_not_absolute")
    if not MIN_REFRESH_SECONDS <= refresh_seconds <= MAX_REFRESH_SECONDS:
        raise PortalPlanError("config_refresh_seconds_out_of_range")
    if not 1 <= outbox_capacity <= MAX_CAPACITY:
        raise PortalPlanError("outbox_capacity_out_of_range")
    return PortalPlan(
        "managed",
        ManagedPlan(
            camera_id,
            endpoint,
            credential_file,
            Path(state_dir),
            refresh_seconds=refresh_seconds,
            outbox_capacity=outbox_capacity,
        ),
    )


class ManagedEdgeSession:
    """Client, cache, outbox, refresher and uploader for one managed camera run."""

    def __init__(
        self,
        plan: ManagedPlan,
        *,
        connection_factory: ConnectionFactory | None = None,
        clock: Callable[[], float] = time.time,
        uploader_options: dict[str, Any] | None = None,
    ) -> None:
        self.plan = plan
        self._clock = clock
        client_options: dict[str, Any] = {}
        if connection_factory is not None:
            client_options["connection_factory"] = connection_factory
        self.client = EdgeControlPlaneClient(plan.endpoint, plan.credential_file, **client_options)
        try:
            ensure_private_directory(plan.state_dir)
            self.cache = ConfigCache(plan.state_dir / "config")
            self.outbox = RoomEventOutbox(plan.state_dir / "outbox", capacity=plan.outbox_capacity)
        except ManagedConfigError as exc:
            raise PortalPlanError(exc.category) from None
        except sqlite3.Error:
            raise PortalPlanError("outbox_unusable") from None
        self.config = ManagedPortalConfig(self.client, self.cache, plan.camera_id, clock=clock)
        self.config.start_from_cache()
        self.uploader = RoomEventUploader(
            self.outbox, self.client, clock=clock, **(uploader_options or {})
        )
        self._refresher: ConfigRefresher | None = None
        self._closed = False

    @classmethod
    def open(cls, plan: ManagedPlan, **options: Any) -> ManagedEdgeSession:
        return cls(plan, **options)

    def current(self) -> tuple[PortalSet, str | None]:
        return self.config.portals()

    def submit(self, transition: RoomTransition, stream_instance_id: str) -> bool:
        """The runtime's transition sink: persist before any upload. Never blocks, never raises
        for a full or closed outbox (the refusal is counted)."""
        return self.outbox.enqueue(
            event_payload(
                transition,
                camera_id=self.plan.camera_id,
                stream_instance_id=stream_instance_id,
                occurred_at_unix=self._clock(),
            )
        )

    def start(self, stage: Callable[[PortalSet, str | None], None]) -> None:
        """Hand the last-known-good lines to the pipeline, then start both threads. The first
        config fetch happens on the refresher thread, so this never waits on the network."""
        if self._closed or self._refresher is not None:
            return
        stage(*self.current())
        self._refresher = ConfigRefresher(
            self.config, stage, interval_seconds=self.plan.refresh_seconds
        )
        self._refresher.start()
        self.uploader.start()

    @property
    def threads_running(self) -> bool:
        return self.uploader.running or (self._refresher is not None and self._refresher.running)

    def snapshot(self) -> dict[str, Any]:
        """Config state and queue counters. No credential, path, event content or track."""
        return {
            "camera_id": self.plan.camera_id,
            "config": self.config.snapshot(),
            "events": self.outbox.snapshot(),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._refresher is not None:
            self._refresher.stop()
        self.uploader.stop()
        self.outbox.close()
