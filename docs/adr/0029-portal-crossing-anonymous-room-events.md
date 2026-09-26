# ADR 0029: Portal crossing and anonymous room entry / exit events

- Status: Accepted (V1-05A). Semantics qualification; events not yet persisted. No alerting.
- Amended by ADR 0030 (V1-05B): managed edge distribution and event persistence are implemented
  but not deployed; `edge_distribution` stays `NOT_CONNECTED` until a deployed node consumes them.
- Date: 2026-09-26

## Context

The live pipeline reports camera-view facts: `PERSON_APPEARED_IN_VIEW` when a validated track
starts and `PERSON_NO_LONGER_VISIBLE` when it ends (ADR 0023). Neither is a room fact. A person
appears in view when they step out from behind furniture, when the detector recovers after a
miss, when they were already there as the stream started, or after a WebRTC reconnect; they
disappear when occluded, when the detector loses them, or when the camera does. Renaming these
into "entered" / "exited" would be false, and the existing test that forbids entry/exit wording
in the camera-view vocabulary stays.

Physical entry and exit need spatial evidence: a person crossing the room's doorway.

**V1-05A does NOT identify teachers**, children, guardians or anyone else, and does not classify
people as adults or children. A teacher-enter/exit feature requires a separately approved
identity / roster evidence path.

## Decision

### Portal geometry

A **portal** is an operator-configured line segment across a doorway in one camera's picture:
`A = (x1, y1)` to `B = (x2, y2)`, normalised to the frame (origin top-left, `y` down, the same
convention as ignore regions), plus **which side is the room**, a label, an `enabled` flag and a
dead-band.

The room's side is given **as seen on the picture**: `LEFT` / `RIGHT` for a line running up and
down the picture, `ABOVE` / `BELOW` for one running across it. Internally it becomes the unit
normal of the line pointing into the room - of `±(dy, -dx)/|d|`, the one with a positive
component along the chosen image direction (LEFT `(-1,0)`, RIGHT `(1,0)`, ABOVE `(0,-1)`, BELOW
`(0,1)`). A side within 60° of the line itself (|normal · direction| < 0.5) does not say which side
is meant and is refused as ambiguous. Also refused: NaN/infinity, coordinates outside `[0,1]`,
lines shorter than 0.01 of the frame, a dead-band outside `[0, 0.1]`, labels outside a restricted
character set, duplicate ids, and more than **4 portals per camera**.

The signed **inside offset** of a point is `(P − A) · n_inside` in the normalised plane (fractions
of width horizontally, of height vertically - anisotropic on non-square frames, deterministic and
resolution-independent). Positive is in the room.

### Track reference point

A track's position is the **bottom-centre of its box**, normalised and clamped to the frame
(`track_reference_point`). For an upright person that approximates where they stand, which is
what a doorway on the floor is about. It is deliberately not called "feet" and claims no physical
floor position: a box is a detector's estimate and its bottom edge can be a knee behind a table.
Non-finite or degenerate boxes carry no position and are skipped.

### Crossing state machine

Per track, per portal, O(1) state (`PortalTrackState`): established side `UNKNOWN_SIDE` /
`INSIDE` / `OUTSIDE`, the last point on that side, and a candidate run on the other side.

- An observation within `deadband` of the line is `DEADBAND` and **neutral** - it neither
  confirms nor breaks anything, so hovering in a doorway produces nothing.
- **Initialisation:** the first observation clearly on one side sets that side and emits
  nothing. A person already inside at startup, or first detected inside, did not enter.
- **Transition:** `confirm_observations` (default 3) consecutive observations clearly on the
  opposite side (dead-band observations do not break the run; one back on the established side
  resets it). Combined with the dead-band this requires sustained evidence and a perpendicular
  movement of at least twice the dead-band; one noisy box never fires.
- **Through the doorway:** the straight path from the last old-side point to the first new-side
  point must meet the line within the drawn segment (± 10 % of its length). Otherwise the side is
  rebased silently and counted `outside_segment` - someone who walked past the end of the line did
  not use the door.
- **Cooldown delays, never hides:** after an event the next one for the same track and portal
  waits until `cooldown_seconds` (default 1 s) of source time has passed. The confirmed run is
  kept: if the person is still on the new side when the cooldown ends, the event fires then; if
  they went back, nothing fires, which is right for someone who stepped in and straight back out.
- Out-of-order timestamps are ignored and counted.

Time is the source/media timeline (`TrackObservation.timestamp_ms`), not wall clock.

### Which tracks may emit

Only **CONFIRMED** tracks produce observations at all (the pipeline never reports TENTATIVE
tracks), so a tentative track has no portal state and cannot emit. Among confirmed tracks, an event
is emitted only if the occupancy ledger has **validated** the track (ADR 0023). A candidate that
completes a crossing moves its state to the new side (so it cannot fire later) and is counted
`suppressed_not_validated`: candidates are precisely the tracks the ledger says may be a poster or
other fixed nuisance. A walking person validates within about two confirmed observations, before
any crossing (at least four observations) can complete.

### Events

`PERSON_ENTERED_ROOM` (OUTSIDE → INSIDE) and `PERSON_EXITED_ROOM` (INSIDE → OUTSIDE), a separate
vocabulary from the camera-view timeline. Fields: sequence, kind, direction, stream id, track id,
portal id and label, timestamp, the normalised crossing point, and the evidence count. No child,
staff or guardian id, no face, embedding, crop, image, frame, clothing descriptor or cross-camera
identity. Track ids are session-local.

### Birth, death, discontinuity

- Track birth (inside or outside) is not an entry; track death (inside or outside) is not an
  exit: when a track ends, its portal state is simply dropped.
- On any tracker discontinuity - a reconnect, an over-long gap, a resolution change - all portal
  state is dropped. Tracks seen afterwards initialise wherever they are; nothing is synthesised
  and no id before the break is linked to one after it (after a resolution change the tracker even
  reuses ids from 1, which is why the reset is unconditional).

### Bounds and cost

At most 4 portals per camera, at most 256 tracked states per portal (the tracker's own
128 active + 128 lost; least recently observed is evicted and counted), O(1) state per track,
100 recent transitions retained, fixed-size latency reservoir. Synthetic microbenchmark
(4 portals × 200 tracks × 250 observations on the Jetson): p50 16 µs, p95 24 µs per observation -
negligible against a ~100 ms detector.

### Configuration

- **Local edge (evaluation path):** `veotrex-edge live-demo --portal
  [id:]x1,y1,x2,y2,INSIDE[,label][,deadband=D]` (repeatable), `--portal-deadband`,
  `--portal-confirm-observations`. Validated before any source or detector starts. No portal
  means no behaviour change.
- **Control plane (persistence):** `camera_portals` (migration 0014), scoped to tenant, facility,
  classroom and camera (the camera must belong to the classroom via its zone), forced RLS, PUBLIC
  revoked, runtime SELECT/INSERT/UPDATE, archived never deleted, revisioned, audited with geometry
  but not the label. API under `/v1/classrooms/{id}/cameras/{id}/portals` (read:operational;
  configure:facility-cameras to change). Web: *Doorway lines* per classroom camera, numeric
  entry. The same geometry rules are applied by the API, the web form and the edge; parity tests
  keep their constants identical.
- **Edge distribution is deferred.** No channel delivers portals to the edge in this stage, and
  the edge machine credential was not widened to fetch them. Every API response says
  `edge_distribution: NOT_CONNECTED`, the UI says "Not yet sent to cameras", and each portal is
  shown as the exact `--portal` flag for local evaluation.

### Dashboard

A *Room transitions* card (entries, exits, "Entered via <door>" / "Exited via <door>", rendered via
`textContent`) and a preview overlay: the line, an arrow into the room and "door N in". Labels are
never drawn into the picture. No identity, role or demographic wording.

### Persistence

`PERSISTENCE_CONNECTED=NO`. There is no control-plane event ingestion path, and none is invented
here: events live in the edge session (dashboard, `/api/state`, the demo's exit report) and in a
structured log line with kind, portal and track id. `RoomTransition` is the domain object the next
persistence stage will carry.

## Future boundaries (not implemented)

- **Staff recognition attachment point:** an approved identity path may later annotate a track
  (`TrackIdentityObservation`, ADR 0019/0020) and join that annotation to a room transition by
  track id. That requires its own ADR and roster evidence; this stage never produces one, and an
  UNKNOWN track is never a teacher or a child.
- **Event persistence:** a bounded, authenticated ingestion of `RoomTransition` into the control
  plane, with its own retention.
- **Multi-camera:** each camera's portals and tracks are independent. Nothing correlates a person
  across cameras, and a doorway seen by two cameras would be counted by each.
- **Edge configuration delivery** for portals (and ignore regions) with an explicit, least-privilege
  credential scope.

## Consequences

- Entry and exit counts exist only where an operator has drawn a doorway, and are honest about
  what they are: anonymous line crossings.
- A person who enters out of the camera's view of the door, or is occluded while crossing, is not
  counted; a person already inside at startup is not counted as entering. Counts are therefore not
  a head count and never feed ratios.
- Migration 0014's downgrade refuses while any portal exists.
