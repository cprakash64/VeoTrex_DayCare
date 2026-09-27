"""Managed portal configuration from the control plane, with a last-known-good cache (V1-05B).

**What is fetched.** ``GET /v1/edge/runtime-config`` answers, for the authenticated node only,
each actively assigned camera's ACTIVE portals and a ``configuration_revision``. The document
carries geometry, labels, flags and revisions - no provider id, token, name or identity.

**Nothing is applied unless all of it is valid.** ``parse_runtime_config`` checks the whole
document strictly (types, bounds, counts, every portal through the same ``Portal`` rules the
local ``--portal`` path uses) and *recomputes* every ``configuration_revision`` and the
``config_version`` with the same canonical SHA-256 the control plane uses. One bad field, one
mismatched hash, and the whole refresh is refused: the previous configuration stays in force.
A camera's portals are replaced as one set, never portal by portal.

**Last-known-good.** Every validated document is written to a cache file (0600, in a 0700
directory, owned by this user, atomic replace). At startup the cache is used until the first
successful fetch; a cache that is missing, unreadable, oversized, a symlink, too permissive,
unparseable or whose hashes do not match is ignored - the portal feature then stays off until
the control plane answers. The cache holds geometry only, never a credential. It is validated,
not signed: anyone able to write files as this user could also read the credential itself.

**Failures never stop video.** A fetch failure keeps the current configuration and increments
a counter; the refresher simply tries again at the next interval. The refresher is one daemon
thread: it makes its first attempt as soon as it starts (so startup never waits on the network),
then one attempt per interval (default 60 s, bounded 30-600 s), each bounded by the client's
timeout. The pipeline thread picks a new portal set up between observations
(``PortalMonitor.apply_staged``); nothing here ever touches inference directly.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from veotrex_edge_agent.edge_control_plane import (
    RUNTIME_CONFIG_PATH,
    ControlPlaneError,
    EdgeControlPlaneClient,
)
from veotrex_edge_agent.live.portal_geometry import (
    MAX_PORTALS,
    InsideSide,
    Portal,
    PortalError,
    PortalSet,
)

SCHEMA_VERSION = 1
MAX_CAMERAS = 32
MAX_CACHE_BYTES = 262_144
DEFAULT_REFRESH_SECONDS = 60.0
MIN_REFRESH_SECONDS = 30.0
MAX_REFRESH_SECONDS = 600.0
CACHE_FILE_NAME = "runtime-config.json"
_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_PORTAL_KEYS = {
    "portal_id",
    "label",
    "x1",
    "y1",
    "x2",
    "y2",
    "inside",
    "enabled",
    "deadband",
    "revision",
}
_CAMERA_KEYS = {"camera_id", "assignment_id", "portals", "configuration_revision"}
_DOCUMENT_KEYS = {"schema_version", "edge_node_id", "config_version", "cameras"}


class ManagedConfigError(ValueError):
    """A configuration document was refused. ``category`` is fixed text."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def canonical_json(document: Any) -> bytes:
    """Byte-for-byte the control plane's canonical form (``veotrex_api.edge_runtime``)."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def configuration_revision(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class CameraRuntimeConfig:
    camera_id: str
    assignment_id: str
    revision: str
    portals: PortalSet


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    config_version: str
    edge_node_id: str
    cameras: dict[str, CameraRuntimeConfig]
    document: dict[str, Any] = field(repr=False)


def _uuid(value: object, category: str) -> str:
    if not isinstance(value, str):
        raise ManagedConfigError(category)
    try:
        parsed = UUID(value)
    except ValueError:
        raise ManagedConfigError(category) from None
    if str(parsed) != value:
        raise ManagedConfigError(category)
    return value


def _number(value: object, category: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ManagedConfigError(category)
    return float(value)


def _portal(item: object) -> tuple[Portal, dict[str, Any]]:
    if not isinstance(item, dict) or set(item) != _PORTAL_KEYS:
        raise ManagedConfigError("invalid_portal_document")
    portal_id = _uuid(item["portal_id"], "invalid_portal_id")
    revision = item["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ManagedConfigError("invalid_portal_revision")
    if not isinstance(item["enabled"], bool) or not isinstance(item["label"], str):
        raise ManagedConfigError("invalid_portal_document")
    inside = item["inside"]
    if not isinstance(inside, str) or inside not in InsideSide.__members__:
        raise ManagedConfigError("invalid_portal_inside")
    try:
        portal = Portal(
            portal_id,
            _number(item["x1"], "invalid_portal_geometry"),
            _number(item["y1"], "invalid_portal_geometry"),
            _number(item["x2"], "invalid_portal_geometry"),
            _number(item["y2"], "invalid_portal_geometry"),
            InsideSide(inside),
            item["label"],
            enabled=item["enabled"],
            deadband=_number(item["deadband"], "invalid_portal_geometry"),
        )
    except PortalError:
        raise ManagedConfigError("invalid_portal_geometry") from None
    return portal, item


def parse_runtime_config(document: object) -> RuntimeConfig:
    """Validate a whole runtime-config document, or refuse it whole."""
    if not isinstance(document, dict) or set(document) != _DOCUMENT_KEYS:
        raise ManagedConfigError("invalid_document")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ManagedConfigError("unsupported_schema_version")
    node = _uuid(document["edge_node_id"], "invalid_edge_node_id")
    cameras_raw = document["cameras"]
    if not isinstance(cameras_raw, list) or len(cameras_raw) > MAX_CAMERAS:
        raise ManagedConfigError("invalid_cameras")
    cameras: dict[str, CameraRuntimeConfig] = {}
    for camera in cameras_raw:
        if not isinstance(camera, dict) or set(camera) != _CAMERA_KEYS:
            raise ManagedConfigError("invalid_camera_document")
        camera_id = _uuid(camera["camera_id"], "invalid_camera_id")
        assignment_id = _uuid(camera["assignment_id"], "invalid_assignment_id")
        portals_raw = camera["portals"]
        if not isinstance(portals_raw, list) or len(portals_raw) > MAX_PORTALS:
            raise ManagedConfigError("too_many_portals")
        parsed = [_portal(item) for item in portals_raw]
        revision = camera["configuration_revision"]
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ManagedConfigError("invalid_configuration_revision")
        expected = configuration_revision(
            {
                "camera_id": camera_id,
                "assignment_id": assignment_id,
                "portals": sorted(
                    (item for _, item in parsed), key=lambda item: str(item["portal_id"])
                ),
            }
        )
        if expected != revision:
            raise ManagedConfigError("configuration_revision_mismatch")
        if camera_id in cameras:
            raise ManagedConfigError("duplicate_camera")
        try:
            portal_set = PortalSet(
                tuple(portal for portal, _ in sorted(parsed, key=lambda p: p[0].portal_id))
            )
        except PortalError:
            raise ManagedConfigError("invalid_portal_set") from None
        cameras[camera_id] = CameraRuntimeConfig(camera_id, assignment_id, revision, portal_set)
    version = document["config_version"]
    expected_version = configuration_revision(
        {
            "cameras": sorted(
                [{"camera_id": c.camera_id, "revision": c.revision} for c in cameras.values()],
                key=lambda item: item["camera_id"],
            )
        }
    )
    if not isinstance(version, str) or version != expected_version:
        raise ManagedConfigError("config_version_mismatch")
    return RuntimeConfig(version, node, cameras, document)


# ------------------------------------------------------------------------------ the cache
def ensure_private_directory(path: Path) -> Path:
    """Create (0700) or accept an existing private directory owned by this user."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        status = os.lstat(path)
    except OSError:
        raise ManagedConfigError("state_directory_unusable") from None
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ManagedConfigError("state_directory_unusable")
    if status.st_uid != os.geteuid() or status.st_mode & 0o077:
        raise ManagedConfigError("state_directory_not_private")
    return path


class ConfigCache:
    """The last validated runtime-config document, geometry only."""

    def __init__(self, directory: Path) -> None:
        self.directory = ensure_private_directory(directory)
        self.path = self.directory / CACHE_FILE_NAME
        self.last_rejection: str | None = None

    def load(self) -> tuple[RuntimeConfig, float] | None:
        """(config, fetched_at unix time), or None. A bad cache is ignored, never trusted."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            self.last_rejection = None if exc.errno == errno.ENOENT else "cache_unreadable"
            return None
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_mode & 0o077
                or status.st_size > MAX_CACHE_BYTES
            ):
                self.last_rejection = "cache_not_private_or_too_large"
                return None
            content = os.read(descriptor, MAX_CACHE_BYTES + 1)
        except OSError:
            self.last_rejection = "cache_unreadable"
            return None
        finally:
            os.close(descriptor)
        try:
            wrapper = json.loads(content.decode("utf-8"))
            if not isinstance(wrapper, dict) or set(wrapper) != {"fetched_at", "runtime_config"}:
                raise ManagedConfigError("cache_malformed")
            fetched_at = wrapper["fetched_at"]
            if isinstance(fetched_at, bool) or not isinstance(fetched_at, int | float):
                raise ManagedConfigError("cache_malformed")
            config = parse_runtime_config(wrapper["runtime_config"])
        except (UnicodeDecodeError, json.JSONDecodeError, ManagedConfigError):
            self.last_rejection = "cache_corrupt"
            return None
        self.last_rejection = None
        return config, float(fetched_at)

    def store(self, config: RuntimeConfig, fetched_at: float) -> bool:
        """Atomically replace the cache. False (never an exception) if the write fails."""
        payload = json.dumps(
            {"fetched_at": fetched_at, "runtime_config": config.document},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_CACHE_BYTES:
            return False
        temporary = self.directory / f".{CACHE_FILE_NAME}.{os.getpid()}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.path)
            directory = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
        return True


# ------------------------------------------------------------------------- the manager
@dataclass(slots=True)
class ConfigMetrics:
    config_fetch_success_total: int = 0
    config_fetch_failure_total: int = 0
    config_rejected_total: int = 0
    config_changes_total: int = 0
    config_cache_write_failures_total: int = 0
    last_failure_category: str | None = None


class ManagedPortalConfig:
    """The current portal configuration for one camera, kept last-known-good."""

    def __init__(
        self,
        client: EdgeControlPlaneClient,
        cache: ConfigCache | None,
        camera_id: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._cache = cache
        self.camera_id = camera_id
        self._clock = clock
        self._lock = threading.Lock()
        self._current: RuntimeConfig | None = None
        self._fetched_at: float | None = None
        self.source = "none"
        self.metrics = ConfigMetrics()

    def start_from_cache(self) -> bool:
        if self._cache is None:
            return False
        loaded = self._cache.load()
        if loaded is None:
            return False
        with self._lock:
            self._current, self._fetched_at = loaded
            self.source = "cache"
        return True

    def refresh(self) -> bool:
        """Fetch, validate and swap. Returns True when this camera's portals changed."""
        try:
            config = parse_runtime_config(self._client.get_json(RUNTIME_CONFIG_PATH))
        except ControlPlaneError as exc:
            with self._lock:
                self.metrics.config_fetch_failure_total += 1
                self.metrics.last_failure_category = exc.category
            return False
        except ManagedConfigError as exc:
            with self._lock:
                self.metrics.config_fetch_failure_total += 1
                self.metrics.config_rejected_total += 1
                self.metrics.last_failure_category = exc.category
            return False
        now = self._clock()
        with self._lock:
            before = self._camera_revision(self._current)
            previous = None if self._current is None else self._current.config_version
            persist = previous != config.config_version
            self._current, self._fetched_at = config, now
            self.source = "control_plane"
            self.metrics.config_fetch_success_total += 1
            self.metrics.last_failure_category = None
            changed = before != self._camera_revision(config)
            if changed:
                self.metrics.config_changes_total += 1
        # Written only when the document changed: a steady configuration is not rewritten to
        # flash once a minute. (Its fetch time in the cache is therefore the first fetch.)
        if self._cache is not None and persist and not self._cache.store(config, now):
            with self._lock:
                self.metrics.config_cache_write_failures_total += 1
        return changed

    def record_internal_failure(self) -> None:
        with self._lock:
            self.metrics.config_fetch_failure_total += 1
            self.metrics.last_failure_category = "internal_error"

    def _camera_revision(self, config: RuntimeConfig | None) -> str | None:
        if config is None:
            return None
        camera = config.cameras.get(self.camera_id)
        return None if camera is None else camera.revision

    def portals(self) -> tuple[PortalSet, str | None]:
        """This camera's portals and revision; an empty set when there is no valid config or the
        camera is not assigned to this node - the feature fails closed."""
        with self._lock:
            config = self._current
        if config is None:
            return PortalSet(), None
        camera = config.cameras.get(self.camera_id)
        return (PortalSet(), None) if camera is None else (camera.portals, camera.revision)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            config, fetched = self._current, self._fetched_at
            metrics = self.metrics
            return {
                "source": self.source,
                "config_version": None if config is None else config.config_version,
                "camera_revision": self._camera_revision(config),
                "config_age_seconds": None
                if fetched is None
                else round(max(0.0, self._clock() - fetched), 1),
                "config_fetched_at": None
                if fetched is None
                else datetime.fromtimestamp(fetched, UTC).isoformat(),
                "config_fetch_success_total": metrics.config_fetch_success_total,
                "config_fetch_failure_total": metrics.config_fetch_failure_total,
                "config_rejected_total": metrics.config_rejected_total,
                "config_changes_total": metrics.config_changes_total,
                "config_cache_write_failures_total": metrics.config_cache_write_failures_total,
                "last_failure_category": metrics.last_failure_category,
                "cache_rejection": None if self._cache is None else self._cache.last_rejection,
            }


class ConfigRefresher:
    """One bounded daemon thread: an attempt at start, then one per interval; never busy-loops.

    ``run_once`` is the whole of one attempt and never raises, so a surprise in a callback cannot
    end the thread; the tests drive it directly instead of waiting on a clock.
    """

    def __init__(
        self,
        managed: ManagedPortalConfig,
        on_change: Callable[[PortalSet, str | None], None],
        *,
        interval_seconds: float = DEFAULT_REFRESH_SECONDS,
        join_timeout_seconds: float = 5.0,
    ) -> None:
        if not MIN_REFRESH_SECONDS <= interval_seconds <= MAX_REFRESH_SECONDS:
            raise ValueError(
                f"config refresh interval must be {MIN_REFRESH_SECONDS:g}-"
                f"{MAX_REFRESH_SECONDS:g} seconds"
            )
        self._managed = managed
        self._on_change = on_change
        self._interval = interval_seconds
        self._join_timeout = join_timeout_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> bool:
        """One refresh; True when this camera's portals changed and were handed on."""
        try:
            if not self._managed.refresh():
                return False
            self._on_change(*self._managed.portals())
        except Exception:
            self._managed.record_internal_failure()
            return False
        return True

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="veotrex-config-refresh", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self._interval):
                return

    def stop(self) -> None:
        """Signal and wait briefly. An attempt still in flight ends within the client timeout;
        the thread is a daemon, so it can never hold the process open."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._join_timeout)
