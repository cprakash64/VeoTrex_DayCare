# Daycare owner demo runbook

How to run and record a VeoTrex demonstration. Read the scope section first: it says exactly
what the product does today, so the demo can be delivered without claiming anything untrue.

## Scope: what is real right now

**Real and demonstrable**

- Person detection on the edge device — YOLOX-s FP16 on TensorRT, in an isolated GPU worker.
- Multi-object tracking — bounded ByteTrack-style tracker with a Kalman filter, per stream.
- Occupancy — a count of *distinct people currently confirmed by the tracker*.
- Monitoring coverage — ACTIVE / IMPAIRED / UNKNOWN, derived from real frame arrival.
- Session safety events — emitted when the pipeline observes a real state change.
- Measured telemetry — frames per second and inference latency, both measured, never invented.
- Ring account linking and device inventory — real, through the control plane.

**Deliberately not claimed**

- **Live Ring frames are not analysed.** The Ring transport decodes to a GStreamer `fakesink`;
  no frames reach the detector. Ring mode demonstrates linking, inventory and device health.
- **No staff/child distinction.** The product has no defensible way to tell a staff member
  from anyone else, and inferring it from appearance is prohibited. The dashboard says
  "People detected", never "children".
- **No regulatory ratio.** The demo threshold is an operator-typed number. It is not the
  Arizona staffing ratio and must never be described as one.
- **No supervision engine.** There is no supervision detection, so no supervision card exists.
- **No evidence clips or snapshots.** Nothing is captured or stored.
- **Events are not an audit trail.** They live in the monitoring process and end with it.

---

## A. Ring mode

Demonstrates account linking, device inventory and provider health. It does **not** show
detection on Ring video — say so plainly if asked.

**Prerequisites**: a linked Ring account, the API running, `VEOTREX_API_BASE_URL` set for the
web app.

1. Start the API and web app (see the repository README).
2. Sign in through Auth0 and open `/app/cameras`.
3. Press **Manage Ring inventory** and sync the connection to refresh device state.
4. Show the device list: display name, inventory state, provider online status, assignment.

The Cameras page states on screen that Ring devices are not being analysed. Leave that text
visible; it is what keeps the demo honest.

---

## B. Recorded video mode

This is the mode that shows the full pipeline: video, detection, tracking, occupancy, coverage
and events.

### 1. Place the footage

```bash
cp /path/to/your/footage.mp4 data/demo/daycare_demo.mp4
```

`data/demo/` is git-ignored apart from its placeholder files. **Never commit footage.**

> Use only video you recorded, own, or are licensed to use. Footage containing children must
> have the permission of the parents or guardians and of the facility, and must stay on the
> machine that plays it. Do not download third-party video for this purpose.

Requirements: `.mp4`, `.m4v` or `.mov`, H.264 or H.265. Other containers are refused by name
rather than guessed at.

### 2. Configure and start the monitoring runtime

```bash
export VEOTREX_DEMO_VIDEO_PATH="$PWD/data/demo/daycare_demo.mp4"
export VEOTREX_DEMO_VIDEO_LOOP=true
export VEOTREX_DEMO_CADENCE_FPS=5
export VEOTREX_DEMO_AREA_LABEL="Demo Classroom"
export VEOTREX_DEMO_CAMERA_LABEL="Demo Camera"
export VEOTREX_EDGE_NODE_ID=00000000-0000-0000-0000-000000000001
uv run --all-packages veotrex-demo-runtime
```

To show the threshold card, also declare how many staff are on duty. This is typed in by the
operator and is never inferred from the video:

```bash
export VEOTREX_DEMO_STAFF_ON_DUTY=1
export VEOTREX_DEMO_PEOPLE_PER_STAFF=4
```

Leaving `VEOTREX_DEMO_STAFF_ON_DUTY` unset hides the threshold card entirely, which is the
right choice if you would rather not explain it on camera.

Confirm the runtime is healthy:

```bash
curl -s http://127.0.0.1:8878/state | head -c 400
```

Expect `"kind": "RECORDED_DEMO"`, `"health": "RUNNING"`, `"state": "ACTIVE"`.

### 3. Point the web app at the runtime and start it

```bash
export VEOTREX_DEMO_RUNTIME_URL=http://127.0.0.1:8878
pnpm web:dev
```

The browser never talks to the runtime directly. Frames and state are proxied through
`/api/demo/*`, which requires an Auth0 session.

### 4. Open the dashboard

Sign in, then open **`/app`**. This is the recording view.

### Playback control

- **Restart** — `/app/demo`, or `curl -X POST http://127.0.0.1:8878/control/restart`
- **Loop** — `VEOTREX_DEMO_VIDEO_LOOP=true` (default)
- **Pause** — intentionally not implemented. With no frames arriving the pipeline correctly
  degrades to impaired coverage and unknown occupancy; that is right for safety and wrong to
  trigger deliberately mid-demo.

### Decoder note

The software decoder is the default. On this Jetson, GStreamer's `decodebin` selects
`nvv4l2decoder`, which rejects ordinary H.264 files with "Unsupported Codec". Set
`VEOTREX_DEMO_DECODER=nvidia` only if you have confirmed hardware decoding works with your
specific clip.

---

## C. Recording checklist

Before you hit record:

- [ ] Runtime reports `RUNNING` and coverage `ACTIVE`
- [ ] People are visibly detected in the clip — watch a full loop first
- [ ] Correct classroom and camera labels configured
- [ ] Browser at roughly 1440×900, dashboard at `/app`
- [ ] Browser notifications silenced; bookmarks bar and extra tabs hidden
- [ ] No terminal windows visible
- [ ] No secrets, tokens or `.env` files on screen
- [ ] Restart the runtime for a clean event timeline
- [ ] Real names, faces of identifiable children, and any personal information reviewed and
      acceptable to show

---

## D. Three-minute demo flow

| Time | Beat | What to show | What to say |
|------|------|--------------|-------------|
| 0:00 | Overview | `/app` — the whole dashboard | "This is your safety operations view. One place for every room." |
| 0:20 | Camera | The 16:9 panel, the source badge | "Live Ring cameras, or a recording. The badge always tells you which — a recording can never look live." |
| 0:45 | Detection | Boxes and track numbers | "Each person gets a box and an identifier that follows them across frames. No faces, no names, no identity." |
| 1:15 | Occupancy | The People detected number | "That number is counted from the tracker, not estimated. It's how many people are in the room right now." |
| 1:45 | Threshold | The demo threshold card | "You set the capacity. We compare the real count against it. We don't guess who is staff — that's your configuration, not our inference." |
| 2:15 | Coverage | Camera health, then stop the runtime | "Watch what happens when the camera drops. It doesn't say zero people. It says *unknown*. Losing video is never reported as a safe room." |
| 2:40 | On device | The Processing card | "Detection and tracking run on the device in your building. Video isn't shipped to a cloud service." |
| 3:00 | Close | Back to the overview | "Your cameras become an operational safety system." |

The coverage beat at 2:15 is the strongest moment in the demo. Stop the runtime with Ctrl-C
and let the dashboard degrade on camera — the card turns to UNKNOWN and an event appears in
the timeline. Restart it afterwards to show recovery.
