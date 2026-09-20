"""Device-local HTTP surface for the monitoring pipeline.

Bound to loopback by default. This is an edge-device surface, not a tenant-scoped API: it
carries no authentication of its own, so the dashboard reaches it through an authenticated
server-side proxy rather than the browser talking to it directly. Nothing here reads or
writes the control-plane database.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseServer
from typing import Any

import structlog

from veotrex_edge_agent.monitoring.owner_demo import OWNER_DEMO_HTML
from veotrex_edge_agent.monitoring.pipeline import MonitoringPipeline, PipelineSnapshot

BOUNDARY = "veotrexframe"
STREAM_IDLE_SLEEP_SECONDS = 0.02


def snapshot_payload(snapshot: PipelineSnapshot) -> dict[str, Any]:
    """The exact contract the dashboard renders. Absent measurements stay null."""
    occupancy = snapshot.occupancy
    return {
        "source": {
            "kind": str(snapshot.source_kind),
            "health": str(snapshot.source_health),
            "is_live": snapshot.source_kind is not snapshot.source_kind.RECORDED_DEMO,
            "loops_completed": snapshot.loops_completed,
            "media_timestamp_seconds": snapshot.media_timestamp_seconds,
            "error_category": snapshot.source_error_category,
        },
        "area": {"label": snapshot.area_label, "camera_label": snapshot.camera_label},
        "coverage": {
            "state": str(occupancy.coverage),
            "seconds_since_last_frame": snapshot.seconds_since_last_frame,
        },
        "occupancy": {
            "certainty": str(occupancy.certainty),
            "people_detected": occupancy.people_detected,
            "confirmed_track_ids": list(occupancy.confirmed_track_ids),
        },
        "demo_threshold": {
            "state": str(occupancy.threshold_state),
            "permitted_people": occupancy.permitted_people,
        },
        "telemetry": {
            "measured_fps": snapshot.measured_fps,
            "inference_latency_ms": snapshot.inference_latency_ms,
            "active_tracks": snapshot.active_track_count,
            "frames_processed": snapshot.frames_processed,
            "decode_failures": snapshot.decode_failures,
        },
        "events": [asdict(event) | {"kind": str(event.kind)} for event in snapshot.events],
    }


def owner_status_payload(snapshot: PipelineSnapshot) -> dict[str, Any]:
    """Flat telemetry for the owner page. No identity, no paths, no configuration values."""
    occupancy = snapshot.occupancy
    measured = occupancy.certainty is occupancy.certainty.MEASURED
    return {
        "source": str(snapshot.source_kind),
        "is_live": snapshot.source_kind is not snapshot.source_kind.RECORDED_DEMO,
        "area": snapshot.area_label,
        "coverage": str(occupancy.coverage),
        # Null rather than zero whenever the count is not measured.
        "occupancy": occupancy.people_detected if measured else None,
        "active_tracks": snapshot.active_track_count,
        "peak_occupancy": snapshot.peak_occupancy,
        "tracks_observed": snapshot.tracks_observed,
        "longest_track_seconds": snapshot.longest_track_seconds,
        "session_seconds": snapshot.session_seconds,
        "fps": snapshot.measured_fps,
        "inference_latency_ms": snapshot.inference_latency_ms,
        "frames_processed": snapshot.frames_processed,
        "events": [asdict(event) | {"kind": str(event.kind)} for event in snapshot.events],
    }


class MonitoringRequestHandler(BaseHTTPRequestHandler):
    server_version = "VeoTrexDemoRuntime"
    sys_version = ""
    pipeline: MonitoringPipeline

    def log_message(self, format: str, *args: Any) -> None:
        structlog.get_logger().debug("demo_runtime_request", path=self.path)

    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/healthz":
            self._json({"status": "ok"})
        elif route == "/state":
            self._json(snapshot_payload(self.pipeline.snapshot()))
        elif route == "/api/demo/status":
            self._json(owner_status_payload(self.pipeline.snapshot()))
        elif route in ("/owner-demo", "/owner-demo/", "/"):
            self._html(OWNER_DEMO_HTML)
        elif route == "/stream.mjpg":
            self._stream()
        else:
            self._json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/control/restart":
            self.pipeline.restart_source()
            self._json({"status": "restarted"})
        else:
            self._json({"error": "not_found"}, status=404)

    def _html(self, markup: str) -> None:
        body = markup.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        last: bytes | None = None
        try:
            while True:
                frame = self.pipeline.latest_frame_jpeg()
                if frame is None or frame is last:
                    # Only ever holds the newest frame; a slow client drops frames rather
                    # than accumulating a backlog.
                    time.sleep(STREAM_IDLE_SLEEP_SECONDS)
                    continue
                last = frame
                header = (
                    f"--{BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode()
                self.wfile.write(header)
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return


def make_server(
    pipeline: MonitoringPipeline, *, host: str = "127.0.0.1", port: int = 8878
) -> ThreadingHTTPServer:
    handler = type(
        "BoundMonitoringRequestHandler",
        (MonitoringRequestHandler,),
        {"pipeline": pipeline},
    )

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request: Any, client_address: Any) -> None:
            structlog.get_logger().debug("demo_runtime_connection_error")

    server: ThreadingHTTPServer = Server((host, port), handler)
    assert isinstance(server, BaseServer)
    return server
