# Runbook: live camera tracking demo

Local demo on the Jetson: a camera, person detection, multi-object tracking, and a loopback
dashboard showing **the live camera view** with tracking boxes, track ids and a head count.

**What this demo is.** Anonymous person tracking. It detects and follows people and counts how
many are currently visible.

**What it is not, and must not be described as.** It does not identify anyone. It does not know
who is a teacher and who is a child. It does not infer age, gender, emotion or anything else
about a person. It is not connected to Ring, and it is not running in production. If asked
whether it recognises staff: the enrollment and recognition work exists but its real-person
qualification is still pending, so recognition is switched off here.

**Consent.** Only consenting adults, synthetic imagery, or an empty room. Do not point the
camera at children, and do not use the recorded clip in `data/demo/` — it contains children and
is prohibited for this stage.

---

## Prerequisites

- On the Jetson, in the repository root.
- A UVC/USB camera plugged in **before** you start (see *No camera?* below if not).
- The workspace synced once: `uv sync --all-packages --all-groups --locked`
- The YOLOX engine present at its usual candidate path. It is a local, git-ignored artifact;
  nothing downloads it.

## 1. Find the camera

```bash
uv run --all-packages veotrex-edge live-cameras
```

Prints every `/dev/video*` node with its resolution, frame rate and whether it can actually be
opened, then suggests the command for the first usable one. It reads metadata only — **no frame
is captured, shown or stored**, so it is safe to run in front of the client.

Do not assume `/dev/video0`. Many cameras expose a metadata node alongside the capture node, and
the capture node is often not the first.

## 2. Start the demo

```bash
uv run --all-packages veotrex-edge live-demo --device /dev/video0
```

Replace the device with the one step 1 reported. Useful options:

| Option | Why |
|---|---|
| `--width 1280 --height 720` | request a mode; the camera's accepted mode is read back |
| `--detector none` | run everything except detection — proves the camera and dashboard work |
| `--source synthetic` | no camera at all; a moving synthetic scene |
| `--no-preview` | metrics only, no camera image (lower cost) |
| `--preview-fps 5` | slower preview if the machine is busy; inference is never throttled to match |
| `--inference-fps 6` | the default detector cadence; it adapts down under load, never above this |
| `--inference-min-fps 3` / `--inference-max-fps 8` | the band the adaptive scheduler stays in |
| `--duration 300` | stop automatically after five minutes |
| `--port 8891` | change the dashboard port |
| `--pixel-format MJPG` | the default. YUYV at 720p is capped at 9 fps by USB 2.0 bandwidth |
| `--view left` \| `right` | **only** for dual-lens modules that send both lenses in one frame |
| `--ignore-region ...` | drop detections from a known fixed artifact. See below |

### Masking a known false positive

A fixed camera sees fixed things, and the detector will sometimes call one of them a person.
A poster with a figure on it is the usual culprit: it never moves and never leaves, so the head
count is permanently one too high and the operator learns to distrust it.

Mask it by naming the rectangle it occupies, in coordinates normalized to the frame, with the
origin at the top left:

```
uv run --all-packages veotrex-edge live-demo --device /dev/video0 \
  --ignore-region 0.62,0.18,0.78,0.52,poster-by-the-door
```

Repeatable, up to eight regions. The demo prints the regions it accepted at startup, and the
dashboard shows an **Ignored detections** counter — watch it climb to confirm the region is
doing something, and watch it stay put when a person walks past.

**A detection is dropped when at least 80% of its area falls inside a region.** Containment, not
the centre point: an adult standing in front of a poster is taller and wider than the poster, so
most of their box is outside the region and they are still counted. Lower the bar with
`--ignore-containment` only if you have checked what it costs — the lower it goes, the more
likely a real person in that part of the room disappears.

Regions are for **known fixed visual artifacts**: posters, mirrors, displays, signage. They are
not a way to quieten a detector that is wrong in general, and they are not detector
qualification. Anything else in the masked area is hidden too, so never draw one over a doorway
or a play area. A single region may not cover more than half the frame, and the set may not
cover more than three quarters of it; the demo refuses to start otherwise.

Containment is measured against **one** region at a time, so two regions side by side do not
add up: a detection straddling both, half in each, is kept. Draw one region around adjacent
artifacts rather than two touching ones. (Seen on this camera: a box 51% covered across two
neighbouring regions survived, correctly by the rule and probably not what the operator meant.)

Finding the numbers: divide the pixel position by the frame width and height. A poster whose
top-left corner is at (790, 130) in a 1280x720 frame starts at `0.62,0.18`.

To find them without measuring the room, run the demo with no regions, watch `/api/state`, and
look for a track whose box does not move: a person drifts tens of pixels in a few seconds, a
poster drifts one or two.

It prints the dashboard URL and the tunnel command, then runs until `Ctrl-C`.

## 3. Open the dashboard

The dashboard binds **127.0.0.1 only**. It has no login of its own, so it is deliberately not
reachable from the network. From the demo laptop, forward it over SSH — your existing key
authenticates, and nothing new is exposed:

```bash
ssh -N -L 8891:127.0.0.1:8891 <user>@<jetson-host>
```

Then open <http://127.0.0.1:8891/> on the laptop. Leave that SSH session running for the demo.

The page shows the live camera image with tracking boxes drawn on it, the current head count,
capture/inference/preview rates, detector/tracker/pipeline latency, and source health. Frames
the detector did not run on are split by cause (ADR 0022):

| Row | Meaning |
|---|---|
| Inference scheduler skips | intentional sampling — inference was not due yet. Normal, and large |
| Backpressure drops | inference was due but the detector was still busy. Should stay near 0 |
| Transport/media drops | lost before capture; `–` when the source cannot measure it |

`Inference FPS` shows the achieved rate and, in brackets, the rate the scheduler is currently
aiming for. A bracketed value below `--inference-fps` means the scheduler slowed down because the
detector got slower; it recovers gradually on its own.

The picture is rendered on the Jetson onto the exact frame the tracker processed, so boxes
cannot drift away from the person. Exactly one frame is held in memory at a time and **nothing
is ever written to disk** — no recording, no stills, no history. If the feed stops, the page
shows an explicit placeholder rather than leaving a frozen image looking live.

> Binding to anything other than loopback requires an explicit flag and is refused by default.
> Do not use it. The tunnel takes five seconds and keeps an unauthenticated dashboard off the
> network.

## 4. Stop cleanly

`Ctrl-C` in the terminal running the demo. It releases the camera, stops the GPU worker, shuts
down the dashboard and prints the session's metrics. Then `Ctrl-C` the SSH tunnel.

---

## If something goes wrong

**No camera found.** `live-cameras` prints nothing usable. Check the cable, then re-run it —
USB cameras take a second or two to enumerate. `ls /dev/video*` confirms whether Linux sees the
device at all. If the client is waiting, fall back to `--source synthetic`, and say plainly
that it is a synthetic scene: the dashboard labels it `SYNTHETIC_TEST (not live)` and you
should not describe it as a camera feed.

**Camera unplugged mid-demo.** The source reconnects within a bounded budget and the dashboard
shows `RECONNECTING`. Plug it back in and it resumes; the timeline records the disconnect.
If the budget is exhausted the run stops cleanly with `FAILED` rather than hanging.

**GPU worker fails to start.** The run exits immediately with `detector unavailable`. Confirm
the engine artifact is present and re-run. To keep the demo moving, `--detector none` shows the
live pipeline and dashboard with detection switched off — say so rather than implying the
detector is running.

**Dashboard unreachable.** Almost always the tunnel, not the demo. Check the SSH session is
still up, and that the port in the tunnel matches `--port`. `curl -s localhost:8891/healthz` on
the Jetson itself distinguishes a dead demo from a dead tunnel.

**Boxes lag behind the person.** Expected and by design. The detector runs at ~6 fps on the
newest camera frame, not on every frame, so a box is up to one inference period (~170 ms) plus
one detection (~100 ms) behind a moving person. The "Inference scheduler skips" counter is that
sampling working, not a fault. A current view with gaps beats a complete view that is seconds
behind.

**Backpressure drops are climbing.** The detector is slower than the schedule. The scheduler
slows itself down within a second (watch the bracketed target in `Inference FPS`); if drops keep
climbing at the minimum rate, the detector itself is slower than `--inference-min-fps` allows,
and `inference_scheduler_state` in the final metrics reads `DETECTOR_BELOW_MINIMUM`.

**The picture looks choppier than the camera.** The preview is drawn only on frames the detector
ran on, so its rate is at most the inference rate (~6 fps) and at most `--preview-fps`. That is
deliberate: the boxes are drawn on the frame they were detected in, so they cannot drift off the
person. `--no-preview` removes the picture entirely.

**The video area shows a broken-image icon and its alt text.** Fixed in V1-DEMO-01R1; if it
ever comes back, the page is claiming a frame the browser refused to render. Check the browser
console first: the earlier cause was the dashboard's own Content-Security-Policy refusing the
URL the page had built for the image, which the page could not detect because the fetch behind
it succeeded. `curl -sI http://127.0.0.1:8891/api/live/frame.jpg` returning `200 image/jpeg`
proves only the endpoint, not the page. The page now reveals a frame only after the browser
reports it decoded, so the failure mode is a placeholder, never a broken icon.

**The video area shows a placeholder.** It says which state it is in — waiting, reconnecting,
disconnected, or stopped. A placeholder is correct behaviour: the dashboard refuses to display a
stale frame, because a frozen image of an empty room is indistinguishable from a live one.

**Someone asks about Ring.** Ring is blocked upstream by an Amazon-side IP-level rejection. It
is unrelated to this demo, no Ring call is made here, and the live source is designed so Ring
becomes an additional source later without changing any detection, tracking or dashboard code.

---

## Notes

- Nothing is recorded. The live preview exists only as a single JPEG in memory, replaced
  several times a second and dropped when the session ends. No video file, no stills, no face
  crops, no biometric data. A finished session leaves nothing on disk.
- The activity list says `PERSON_APPEARED_IN_VIEW` / `PERSON_NO_LONGER_VISIBLE`, not "entered"
  or "exited" — appearing in a camera view is not proof of walking through a door, and the
  wording stays honest about that.
- Track ids are continuity within one run. They are not identities and do not persist across
  restarts.
- No credentials are needed and none are stored by this demo.
