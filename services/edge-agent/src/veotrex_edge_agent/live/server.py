"""Loopback demo dashboard (V1-DEMO-01). LOCAL ONLY.

Binds ``127.0.0.1`` by default and refuses a non-loopback bind unless the operator passes an
explicit override flag. It carries no authentication of its own, so it must not be reachable
from the network: the documented way to view it from a demo laptop is an SSH tunnel, which
authenticates with the operator's existing key and exposes nothing new (see
``docs/runbooks/sunday-live-demo.md``).

The page shows boxes, track ids, a head count, throughput and source health. It deliberately
shows **no video frames**: the state endpoint serves geometry and counts, never pixels. That
keeps the surface free of imagery entirely - nothing to cache, nothing to leak through a
screenshot of a browser tab, and nothing to persist. An operator who wants to see the room
looks at the room.

No names are shown, no identity is shown, and an unidentified person is labelled as a track
number and nothing else.
"""

from __future__ import annotations

import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from veotrex_edge_agent.live.runtime import LiveDemoRuntime

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8891
# The dashboard polls; this bounds how much work a single client can ask for.
MAX_TIMELINE_EVENTS = 50


class InsecureBindRefused(RuntimeError):
    """A non-loopback bind was requested without the explicit override."""


def is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>VeoTrex live demo</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; background:#0e1116; color:#e6edf3;
        font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
 header { padding:16px 20px; border-bottom:1px solid #222a35; display:flex;
          align-items:baseline; gap:16px; flex-wrap:wrap; }
 h1 { font-size:16px; margin:0; font-weight:600; letter-spacing:.02em; }
 .pill { font-size:12px; padding:2px 8px; border-radius:999px; border:1px solid #2b3440; }
 .ok { color:#3fb950; border-color:#204c2a; } .warn { color:#d29922; border-color:#4d3c12; }
 .bad { color:#f85149; border-color:#5c2626; }
 main { display:grid; grid-template-columns:minmax(0,2fr) minmax(260px,1fr); gap:20px;
        padding:20px; align-items:start; }
 @media (max-width:820px){ main{ grid-template-columns:1fr; } }
 .card { background:#141a22; border:1px solid #222a35; border-radius:10px; padding:16px; }
 .count { font-size:56px; font-weight:650; line-height:1; margin:4px 0 2px; }
 .muted { color:#8b949e; font-size:12px; }
 table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
 td,th { text-align:left; padding:4px 0; font-size:13px; }
 th { color:#8b949e; font-weight:500; }
 #stage { position:relative; width:100%; aspect-ratio:16/9; background:#0b0e13;
          border:1px solid #222a35; border-radius:10px; overflow:hidden; }
 .box { position:absolute; border:2px solid #58a6ff; border-radius:3px; }
 .tag { position:absolute; transform:translateY(-100%); background:#58a6ff; color:#0b0e13;
        font-size:11px; font-weight:600; padding:1px 5px; border-radius:3px 3px 0 0; }
 ul { list-style:none; margin:0; padding:0; max-height:320px; overflow:auto; }
 li { padding:5px 0; border-bottom:1px solid #1b222c; font-size:12px; }
 li b { font-weight:600; color:#c9d1d9; }
</style>
<header>
  <h1>VeoTrex &mdash; live tracking</h1>
  <span id="kind" class="pill">source</span>
  <span id="health" class="pill">health</span>
  <span class="pill">local evaluation &mdash; no identification</span>
</header>
<main>
  <div>
    <div id="stage"></div>
    <p class="muted" id="geometry">&nbsp;</p>
  </div>
  <div style="display:grid; gap:16px;">
    <div class="card">
      <div class="muted">People currently visible</div>
      <div class="count" id="occupancy">0</div>
      <div class="muted" id="peak">&nbsp;</div>
    </div>
    <div class="card">
      <table id="metrics"></table>
    </div>
    <div class="card">
      <div class="muted" style="margin-bottom:6px">Activity</div>
      <ul id="timeline"></ul>
    </div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
function pill(el, text, cls){ el.textContent = text; el.className = "pill " + (cls||""); }
function draw(state){
  const stage = $("stage");
  stage.querySelectorAll(".box,.tag").forEach(n => n.remove());
  const w = state.width || 1, h = state.height || 1;
  for (const t of state.tracks){
    const [x1,y1,x2,y2] = t.bbox_xyxy;
    const box = document.createElement("div");
    box.className = "box";
    box.style.left = (x1/w*100)+"%"; box.style.top = (y1/h*100)+"%";
    box.style.width = ((x2-x1)/w*100)+"%"; box.style.height = ((y2-y1)/h*100)+"%";
    const tag = document.createElement("div");
    tag.className = "tag"; tag.textContent = "track " + t.track_id;
    tag.style.left = (x1/w*100)+"%"; tag.style.top = (y1/h*100)+"%";
    stage.append(box, tag);
  }
}
function rows(m){
  const p = (o) => (o && o.p50 != null) ? o.p50.toFixed(1)+" / "+o.p95.toFixed(1)+" ms" : "\\u2013";
  return [
    ["Processing FPS", (m.processing_fps ?? 0).toFixed(1)],
    ["Capture FPS", (m.camera_capture_fps ?? 0).toFixed(1)],
    ["Detector p50/p95", p(m.detector_latency_ms)],
    ["Tracker p50/p95", p(m.tracker_latency_ms)],
    ["Pipeline p50/p95", p(m.pipeline_latency_ms)],
    ["Frames captured", m.frames_captured_total ?? 0],
    ["Frames processed", m.video_frames_processed_total ?? 0],
    ["Frames dropped", m.frames_dropped_total ?? 0],
    ["Tracks created", m.tracks_created_total ?? 0],
    ["Reconnects", m.camera_reconnect_count ?? 0],
  ];
}
async function tick(){
  try {
    const r = await fetch("/api/state", {cache:"no-store"});
    const d = await r.json();
    const s = d.state;
    $("occupancy").textContent = s.occupancy;
    $("peak").textContent = "peak this session: " + (d.metrics.peak_occupancy ?? 0);
    pill($("kind"), s.source.is_live ? s.source.kind : s.source.kind + " (not live)",
         s.source.is_live ? "ok" : "warn");
    const h = s.source.health;
    pill($("health"), h, h === "RUNNING" ? "ok" : (h === "FAILED" ? "bad" : "warn"));
    $("geometry").textContent = s.width
      ? (s.width+"\\u00d7"+s.height+" \\u00b7 frame "+s.frame_index) : "";
    draw(s);
    $("metrics").innerHTML = rows(d.metrics)
      .map(([k,v]) => "<tr><th>"+k+"</th><td>"+v+"</td></tr>").join("");
    $("timeline").innerHTML = d.timeline
      .map(e => "<li><b>"+e.kind+"</b>"+(e.track_id!=null?" track "+e.track_id:"")
                +(e.occupancy!=null?" \\u2192 "+e.occupancy:"")+"</li>").join("")
      || "<li class='muted'>Nothing has happened yet.</li>";
  } catch (err) { pill($("health"), "SERVER UNREACHABLE", "bad"); }
}
tick(); setInterval(tick, 500);
</script>
"""


def build_handler(runtime: LiveDemoRuntime) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "VeoTrexLiveDemo"
        sys_version = ""

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Nothing is embedded and nothing is loaded from anywhere else.
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # BaseHTTPRequestHandler's naming contract
            path = self.path.split("?", 1)[0]
            if path in {"/", "/index.html"}:
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/state":
                payload: dict[str, Any] = {
                    "state": runtime.state.as_dict(),
                    "metrics": runtime.metrics(),
                    "timeline": runtime.timeline.recent(MAX_TIMELINE_EVENTS),
                    "failure": runtime.failure,
                    "running": runtime.running,
                }
                self._send(
                    200,
                    json.dumps(payload, sort_keys=True).encode("utf-8"),
                    "application/json",
                )
                return
            if path == "/healthz":
                self._send(200, b'{"status":"ok"}', "application/json")
                return
            self._send(404, b'{"error":"not_found"}', "application/json")

        def log_message(self, *_: Any) -> None:
            # The default handler writes request lines to stderr; the demo has its own logging
            # and a request log adds nothing but noise on a loopback socket.
            return

    return Handler


class DemoServer:
    """A threaded loopback HTTP server over one runtime."""

    def __init__(
        self,
        runtime: LiveDemoRuntime,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        allow_non_loopback: bool = False,
    ) -> None:
        if not is_loopback(host) and not allow_non_loopback:
            raise InsecureBindRefused(
                "the demo dashboard is unauthenticated; bind loopback and use an SSH tunnel, "
                "or pass the explicit override if you understand the exposure"
            )
        self._runtime = runtime
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._logger = structlog.get_logger()

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            return (self._host, self._port)
        host, port = self._server.server_address[:2]
        return (str(host), int(port))

    def start(self) -> tuple[str, int]:
        if self._server is not None:
            return self.address
        server = ThreadingHTTPServer((self._host, self._port), build_handler(self._runtime))
        server.daemon_threads = True
        self._server = server
        thread = threading.Thread(
            target=server.serve_forever, name="veotrex-demo-http", daemon=True
        )
        self._thread = thread
        thread.start()
        host, port = self.address
        self._logger.info("live_demo_dashboard_started", host=host, port=port)
        return (host, port)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def __enter__(self) -> DemoServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
