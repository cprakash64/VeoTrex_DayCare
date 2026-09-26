"""Loopback demo dashboard (V1-DEMO-01). LOCAL ONLY.

Binds ``127.0.0.1`` by default and refuses a non-loopback bind unless the operator passes an
explicit override flag. It carries no authentication of its own, so it must not be reachable
from the network: the documented way to view it from a demo laptop is an SSH tunnel, which
authenticates with the operator's existing key and exposes nothing new (see
``docs/runbooks/sunday-live-demo.md``).

The page shows the live camera frame with the tracker's boxes drawn on it, a head count,
throughput and source health. Throughput is broken down by cause (V1-03A): capture, inference
and preview rates, and frames not processed split into scheduler skips (intentional sampling),
backpressure drops (the detector behind its schedule) and transport drops (lost before capture).

V1-DEMO-01 served geometry only, on a black canvas. That was a deliberate privacy choice and
the wrong one for a monitoring demonstration, so R1 puts the picture back under bounds: the
frame is rendered server-side onto the exact frame the tracker processed, held as a single
JPEG in memory, never written to disk, and served only over loopback with ``no-store``. The
browser fetches a picture; it is given no way to ask for a past one, because none is kept.

Overlays are drawn server-side, so the page does not redraw geometry in JavaScript and image
and boxes cannot drift apart.

The browser loads the frame by its own same-origin URL into an off-screen ``<img>`` and shows
it only once the browser has decoded it. R1 fetched the bytes and handed the element a ``blob:``
URL instead; this page's own Content-Security-Policy does not allow ``blob:``, so every load was
refused while the ``fetch`` behind it kept succeeding - the page believed it had a picture and
displayed a broken image indefinitely. Loading the URL directly removes the blob, removes the
object-URL bookkeeping, and makes a decode failure something the page can actually observe.

No names are shown, no identity is shown, and an unidentified person is labelled as a track
number and nothing else.
"""

from __future__ import annotations

import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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
 main { display:grid; grid-template-columns:minmax(0,2fr) minmax(280px,1fr); gap:20px;
        padding:20px; align-items:start; }
 @media (max-width:900px){ main{ grid-template-columns:1fr; } }
 .card { background:#141a22; border:1px solid #222a35; border-radius:10px; padding:16px; }
 .count { font-size:56px; font-weight:650; line-height:1; margin:4px 0 2px; }
 .muted { color:#8b949e; font-size:12px; }
 table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
 td,th { text-align:left; padding:4px 0; font-size:13px; }
 th { color:#8b949e; font-weight:500; }
 #stage { position:relative; width:100%; background:#0b0e13; border:1px solid #222a35;
          border-radius:10px; overflow:hidden; display:flex; align-items:center;
          justify-content:center; min-height:280px; }
 /* contain, never cover: the camera image must not be stretched or cropped. */
 .feed { width:100%; height:auto; display:block; object-fit:contain; }
 /* Load-bearing, not tidiness. An author rule beats the user-agent's [hidden]{display:none},
    so `display:block` above silently defeated the hidden attribute on both buffers: the stage
    is a flex row, so two 100%-wide images were laid out side by side at half size each and the
    room appeared duplicated. Only one preview may ever occupy the stage. */
 .feed[hidden] { display:none; }
 #placeholder { position:absolute; inset:0; display:flex; flex-direction:column; gap:8px;
                align-items:center; justify-content:center; text-align:center; padding:24px; }
 #placeholder.hidden { display:none; }
 #placeholder .big { font-size:15px; color:#c9d1d9; }
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
    <div id="stage">
      <img id="feedA" class="feed" alt="Live camera view with tracking overlay" hidden>
      <img id="feedB" class="feed" alt="Live camera view with tracking overlay" hidden>
      <div id="placeholder"><div class="big" id="phtitle">Waiting for live camera\u2026</div>
        <div class="muted" id="phdetail">No frame has arrived yet.</div></div>
    </div>
    <p class="muted" id="geometry">&nbsp;</p>
  </div>
  <div style="display:grid; gap:16px;">
    <div class="card">
      <div class="muted">People currently visible</div>
      <div class="count" id="occupancy">0</div>
      <div class="muted" id="peak">&nbsp;</div>
    </div>
    <div class="card"><table id="metrics"></table></div>
    <div class="card">
      <div class="muted" style="margin-bottom:6px">Activity</div>
      <ul id="timeline"></ul>
    </div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
function pill(el, text, cls){ el.textContent = text; el.className = "pill " + (cls||""); }

// The overlay is drawn server-side onto the frame the tracker processed, so this page never
// redraws geometry: it just shows the picture it is given. Image and boxes cannot disagree.
//
// Two <img> elements, one visible and one loading. A frame becomes visible only once the
// browser has actually decoded it, so a failed or refused load can never put a broken-image
// icon on screen - the previous good frame stays, and a placeholder takes over if the failures
// persist. R1 fetched the frame and handed the <img> an object URL, which this page's own
// Content-Security-Policy does not permit; the fetch succeeded, the decode never happened, and
// the page had no way to tell the difference. The frame is now loaded by its own same-origin
// URL, so there is no object URL to permit, to track or to revoke.
const FRAME_URL = "/api/live/frame.jpg";
// The server encodes at most ~8 fps, and only on inference frames; asking faster only
// re-fetches bytes that have not changed.
const PREVIEW_INTERVAL_MS = 120;
const PREVIEW_RETRY_MS = 400;
// Tolerate one lost frame before replacing the picture: a single 503 between encodes is normal,
// two in a row means there is nothing fresh to show and a still image would be a lie.
const FAILURES_BEFORE_PLACEHOLDER = 2;
const STATE_INTERVAL_MS = 250;

let visibleFeed = null;
let previewFailures = 0;
let lastHealth = null;
let reachable = true;
let previewTimer = null;
let stateTimer = null;
let stopped = false;
// Distinguishes one preview request from the next. Not a cache-buster - the endpoint already
// sends no-store and the browser honours it - but assigning an unchanged src string starts no
// load and fires no event, which would stall this loop the second time an element is reused.
let previewRequest = 0;

function showPlaceholder(title, detail){
  visibleFeed = null;
  $("feedA").hidden = true;
  $("feedB").hidden = true;
  $("placeholder").classList.remove("hidden");
  $("phtitle").textContent = title;
  $("phdetail").textContent = detail;
}
function waitingPlaceholder(){
  // Both loops can want the placeholder at once. An unreachable server is the more specific
  // diagnosis, so it wins; otherwise the two would alternate once a second and read as a fault
  // of their own.
  if (!reachable)
    showPlaceholder("Dashboard cannot reach the demo", "Is the SSH tunnel still up?");
  else if (lastHealth === "FAILED")
    showPlaceholder("Camera reconnecting\u2026", "The feed stopped; still trying.");
  else if (lastHealth === "RECONNECTING")
    showPlaceholder("Camera reconnecting\u2026", "Waiting for the camera.");
  else if (lastHealth === "STOPPED") showPlaceholder("Session stopped", "No live feed.");
  else showPlaceholder("Waiting for live camera\u2026", "No frame has arrived yet.");
}
function schedulePreview(delay){
  // One pending timer and one in-flight request at a time, always. Nothing queues up behind a
  // slow or dead endpoint, and a page that has gone away schedules nothing at all.
  if (stopped || previewTimer !== null) return;
  previewTimer = setTimeout(() => { previewTimer = null; pullPreview(); }, delay);
}
function pullPreview(){
  if (stopped) return;
  const loader = (visibleFeed === $("feedA")) ? $("feedB") : $("feedA");
  const settle = (decoded) => {
    loader.onload = null;
    loader.onerror = null;
    if (decoded) {
      previewFailures = 0;
      if (visibleFeed && visibleFeed !== loader) visibleFeed.hidden = true;
      loader.hidden = false;
      visibleFeed = loader;
      $("placeholder").classList.add("hidden");
      schedulePreview(PREVIEW_INTERVAL_MS);
      return;
    }
    previewFailures += 1;
    loader.hidden = true;
    if (previewFailures >= FAILURES_BEFORE_PLACEHOLDER) waitingPlaceholder();
    schedulePreview(PREVIEW_RETRY_MS);
  };
  loader.onload = () => settle(true);
  loader.onerror = () => settle(false);
  previewRequest += 1;
  loader.src = FRAME_URL + "?sequence=" + previewRequest;
}
// Inference deliberately runs on a sample of camera frames, so "not processed" is split by
// cause: a scheduler skip is a sampling decision, a backpressure drop is the detector falling
// behind its own schedule, and a transport drop was lost before capture. A dash means the
// source cannot measure that number, which is different from zero.
function rows(m){
  const p = (o) => (o && o.p50 != null) ? o.p50.toFixed(1)+" / "+o.p95.toFixed(1)+" ms" : "\u2013";
  const fps = (v) => (v == null) ? "\u2013" : v.toFixed(1);
  const count = (v) => (v == null) ? "\u2013" : v;
  const scheduled = (m.inference_scheduled_fps != null)
    ? " (target " + m.inference_scheduled_fps.toFixed(1) + ")" : "";
  return [
    ["Capture FPS", fps(m.camera_capture_fps ?? 0)],
    ["Inference FPS", fps(m.effective_inference_fps ?? m.processing_fps ?? 0) + scheduled],
    ["Preview FPS", fps(m.preview_fps)],
    ["Detector p50/p95", p(m.detector_latency_ms)],
    ["Tracker p50/p95", p(m.tracker_latency_ms)],
    ["Pipeline p50/p95", p(m.pipeline_latency_ms)],
    ["Preview encode p50/p95", p(m.preview_encode_ms)],
    ["Frames captured", m.camera_frames_captured_total ?? m.frames_captured_total ?? 0],
    ["Inference frames processed",
     m.inference_frames_processed_total ?? m.video_frames_processed_total ?? 0],
    ["Inference scheduler skips", m.inference_frames_skipped_scheduler_total ?? 0],
    ["Backpressure drops", m.inference_frames_dropped_backpressure_total ?? 0],
    ["Transport/media drops", count(m.source_frames_dropped_total)],
    ["Ignored detections", m.detections_ignored_total ?? 0],
    ["Tracks created", m.tracks_created_total ?? 0],
    ["Reconnects", m.camera_reconnect_count ?? 0],
  ];
}
// Counts, metrics and the timeline are on their own timer. The preview is the slowest and the
// most likely thing to fail, and it must not be able to hold up the numbers.
async function pullState(){
  if (stopped) return;
  try {
    const r = await fetch("/api/state", {cache:"no-store"});
    const d = await r.json();
    const s = d.state;
    $("occupancy").textContent = s.occupancy;
    $("peak").textContent = "peak this session: " + (d.metrics.peak_occupancy ?? 0);
    pill($("kind"), s.source.is_live ? s.source.kind : s.source.kind + " (not live)",
         s.source.is_live ? "ok" : "warn");
    const h = s.source.health;
    lastHealth = h;
    reachable = true;
    // The wording is the server's - one definition, shared with the overlay drawn onto the
    // frame - so the two can never disagree about what the camera is doing.
    pill($("health"), s.source.health_label || h,
         h === "RUNNING" ? "ok" : (h === "FAILED" ? "bad" : "warn"));
    $("geometry").textContent = s.width
      ? (s.width+"\u00d7"+s.height+" \u00b7 frame "+s.frame_index) : "";
    $("metrics").innerHTML = rows(d.metrics)
      .map(([k,v]) => "<tr><th>"+k+"</th><td>"+v+"</td></tr>").join("");
    $("timeline").innerHTML = d.timeline
      .map(e => "<li><b>"+e.kind+"</b>"+(e.track_id!=null?" track "+e.track_id:"")
                +(e.occupancy!=null?" \u2192 "+e.occupancy:"")+"</li>").join("")
      || "<li class='muted'>Nothing has happened yet.</li>";
    if (visibleFeed === null) waitingPlaceholder();
  } catch (err) {
    lastHealth = null;
    reachable = false;
    pill($("health"), "SERVER UNREACHABLE", "bad");
    waitingPlaceholder();
  }
  if (!stopped) stateTimer = setTimeout(pullState, STATE_INTERVAL_MS);
}
function teardown(){
  stopped = true;
  if (previewTimer !== null) { clearTimeout(previewTimer); previewTimer = null; }
  if (stateTimer !== null) { clearTimeout(stateTimer); stateTimer = null; }
}
addEventListener("pagehide", teardown);
pullState();
pullPreview();
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
            # img-src is explicit because it is load-bearing: the preview is fetched by
            # its own same-origin URL. blob: is deliberately not permitted, and the page
            # creates no object URLs, so there is nothing for it to allow.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' 'unsafe-inline'; img-src 'self'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # BaseHTTPRequestHandler's naming contract
            # Route on the parsed path. The client appends a query string to every
            # preview request, so a handler that compared the raw request target would
            # 404 the only URL the dashboard actually asks for.
            path = urlsplit(self.path).path
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
            if path == "/api/live/frame.jpg":
                frame = runtime.preview.buffer.latest() if runtime.preview else None
                if frame is None:
                    # No picture yet, or the last one has gone stale. The page shows an
                    # explicit placeholder rather than a convincing frozen image.
                    self._send(
                        503,
                        b'{"error":"no_frame"}',
                        "application/json",
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame.jpeg)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Preview-Sequence", str(frame.sequence))
                self.end_headers()
                try:
                    self.wfile.write(frame.jpeg)
                except (BrokenPipeError, ConnectionResetError):
                    # The browser navigated away or refreshed mid-write. Nothing is retained
                    # on its behalf, so there is nothing to clean up.
                    return
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
