from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import structlog

from veotrex_edge_agent.camera_transport.backend import (
    BackendEvent,
    BackendFactory,
    BackendFailed,
    MediaBackendHandle,
)
from veotrex_edge_agent.camera_transport.controller import (
    AcquireSession,
    Action,
    StartBackend,
    StopBackend,
    TransportController,
)
from veotrex_edge_agent.camera_transport.descriptor import LiveSessionLease, redact_text
from veotrex_edge_agent.camera_transport.errors import TransportError, TransportErrorCategory
from veotrex_edge_agent.camera_transport.provider import LiveSessionProvider

Observer = Callable[["CameraTransportRunner", float], None]


def safe_log_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: scrub every string before it reaches a log sink."""
    return {
        key: redact_text(value) if isinstance(value, str) else value
        for key, value in fields.items()
    }


class CameraTransportRunner:
    """Executes TransportController actions against a provider and media backends.

    It owns credential leases only between acquisition and backend start and never logs them.
    """

    def __init__(
        self,
        controller: TransportController,
        provider: LiveSessionProvider,
        backend_factory: BackendFactory,
        *,
        tick_interval_seconds: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        observer: Observer | None = None,
        logger: Any = None,
    ) -> None:
        if provider.kind is not controller.provider:
            raise ValueError("provider kind must match controller provider")
        if not 0.01 <= tick_interval_seconds <= 5:
            raise ValueError("tick interval is out of bounds")
        self.controller = controller
        self.provider = provider
        self._factory = backend_factory
        self._tick = tick_interval_seconds
        self._clock = clock
        self._observer = observer
        self._logger = logger or structlog.get_logger()
        self.handles: dict[int, MediaBackendHandle] = {}
        self.retired_handles: list[MediaBackendHandle] = []
        self._leases: dict[int, LiveSessionLease] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logged_transitions = 0
        self.events_processed = 0

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def emit(self, event: BackendEvent) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def run(self, stop: asyncio.Event) -> None:
        self._loop = asyncio.get_running_loop()
        await self._execute(self.controller.start(self._clock()))
        try:
            while not stop.is_set():
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=self._tick)
                except TimeoutError:
                    item = None
                now = self._clock()
                if item is not None:
                    await self._execute(self._dispatch(item, now))
                    while not self._queue.empty():
                        await self._execute(self._dispatch(self._queue.get_nowait(), now))
                await self._execute(self.controller.tick(self._clock()))
                self._log_transitions()
                if self._observer is not None:
                    self._observer(self, self._clock())
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        await self._execute(self.controller.stop(self._clock()))
        self._log_transitions()
        for task in list(self._tasks):
            if not task.done():
                await asyncio.wait({task}, timeout=10)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for generation in list(self.handles):
            handle = self.handles.pop(generation)
            await asyncio.to_thread(handle.stop)
            self.retired_handles.append(handle)
        self._leases.clear()

    def _dispatch(self, item: Any, now: float) -> list[Action]:
        self.events_processed += 1
        if isinstance(item, tuple) and item and item[0] == "acquired":
            _, generation, lease = item
            actions = self.controller.on_session_acquired(generation, lease.descriptor, now)
            if any(isinstance(a, StartBackend) and a.generation == generation for a in actions):
                self._leases[generation] = lease
            return actions
        if isinstance(item, tuple) and item and item[0] == "acquire_failed":
            _, generation, category = item
            return self.controller.on_session_failed(generation, category, now)
        return self.controller.on_event(item, now)

    async def _execute(self, actions: list[Action]) -> None:
        for action in actions:
            if isinstance(action, AcquireSession):
                self._spawn(self._acquire(action.generation))
            elif isinstance(action, StartBackend):
                lease = self._leases.pop(action.generation, None)
                if lease is None:
                    continue
                handle = self._factory(action.generation)
                self.handles[action.generation] = handle
                self._spawn(self._start_backend(action.generation, handle, lease))
            elif isinstance(action, StopBackend):
                self._leases.pop(action.generation, None)
                stopped = self.handles.pop(action.generation, None)
                if stopped is not None:
                    self.retired_handles.append(stopped)
                    self._spawn(asyncio.to_thread(stopped.stop))

    async def _acquire(self, generation: int) -> None:
        timeout = self.controller.config.acquire_timeout_seconds
        try:
            lease = await asyncio.wait_for(
                self.provider.acquire(self.controller.camera_id, generation, self._clock()),
                timeout=timeout,
            )
        except TimeoutError:
            self._queue.put_nowait(
                ("acquire_failed", generation, TransportErrorCategory.SESSION_ACQUIRE_TIMEOUT)
            )
        except TransportError as exc:
            self._queue.put_nowait(("acquire_failed", generation, exc.category))
        except Exception:
            self._queue.put_nowait(
                ("acquire_failed", generation, TransportErrorCategory.PROVIDER_UNAVAILABLE)
            )
        else:
            self._queue.put_nowait(("acquired", generation, lease))

    async def _start_backend(
        self, generation: int, handle: MediaBackendHandle, lease: LiveSessionLease
    ) -> None:
        try:
            await asyncio.to_thread(handle.start, lease, self.emit)
        except TransportError as exc:
            self._queue.put_nowait(BackendFailed(generation, self._clock(), exc.category))
        except Exception:
            self._queue.put_nowait(
                BackendFailed(
                    generation, self._clock(), TransportErrorCategory.INTERNAL_TRANSPORT_ERROR
                )
            )
        finally:
            del lease

    def _log_transitions(self) -> None:
        machine = self.controller.machine
        if machine.transition_count == self._logged_transitions:
            return
        fresh = machine.transition_count - self._logged_transitions
        for record in machine.history[-fresh:]:
            self._logger.info(
                "camera_transport_state_changed",
                **safe_log_fields(
                    {
                        "camera_id": str(self.controller.camera_id),
                        "provider": self.controller.provider.value,
                        "previous": record.previous.value,
                        "state": record.state.value,
                        "category": record.category,
                        "generation": record.generation,
                    }
                ),
            )
        self._logged_transitions = machine.transition_count
