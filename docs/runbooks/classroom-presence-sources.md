# Runbook: classroom presence sources (manual, staff roster, child attendance)

Operator guide for V1-04B / V1-04C / V1-04D (ADRs 0025, 0026, 0027). Configured classroom policy
only - nothing here is a legal compliance determination.

## The three modes

| Classroom mode | Children | Qualified staff | Visitors |
| --- | --- | --- | --- |
| Manual report (`MANUAL_AGGREGATE`, default) | manual report | manual report | manual report |
| Staff check-ins + manual child count (`ROSTER_STAFF_PLUS_MANUAL_CHILDREN`) | manual report | staff check-ins | manual report |
| Child attendance + staff check-ins (`ATTENDANCE_CHILDREN_PLUS_ROSTER_STAFF`) | child attendance | staff check-ins | manual report (optional) |

A classroom only changes mode when an administrator chooses so on the classroom page (Presence
sources). Switching never copies a number between sources; after switching back to a manual mode,
report a new count.

## Setting up attendance mode for a classroom

1. **Staff roster** - on each teacher's Staff page, add them to the facility roster and choose
   whether they count toward the configured classroom policy.
2. **Child roster** - on Child rosters, add each child: a name staff recognise and, optionally,
   the reference from your own attendance system. Do not enter dates of birth, guardians, medical
   notes or anything else; the form does not accept them.
3. **Mode** - on the classroom page, Presence sources -> "Child attendance + staff check-ins".
4. **Daily** - check children and staff in on the classroom page as they arrive; use *Move here*
   to move a child or teacher from another room (one step, never in two rooms); check them out
   when they leave.

## Expiry

- A child's attendance lasts 12 hours by default (30 minutes - 12 hours). A forgotten check-out
  lapses on its own; the child then shows *Attendance expired · not counted*. Check them in again
  if they are still present - an expired stay cannot be refreshed.
- A staff check-in lasts 15 minutes by default (1 minute - 4 hours); refresh it while it is
  current.
- Visitors in attendance mode are optional; an unreported visitor count is shown as *not
  reported*, never as zero, and never blocks the ratio.

## What never happens

- Cameras never check anyone in or out, never count children, and never identify a child.
- A face-recognition result never checks a teacher in.
- Child names never appear on the edge dashboard, in audit records or in logs.

## Database notes

- Migrations 0011 (staff roster) and 0012 (child attendance) are additive. They have **not** been
  applied to production; apply them only through the normal reviewed deployment.
- The 0012 downgrade refuses while any child, attendance event, visitor-only report or
  attendance-mode classroom exists (the 0011 downgrade similarly refuses while roster-mode reports
  exist). This is deliberate: a downgrade must not destroy roster history or invent counts.
- After applying a migration, re-run `veotrex-db-runtime-role apply` so the runtime role receives
  exactly the new tables' grants (child profiles: SELECT/INSERT/UPDATE; attendance events:
  SELECT/INSERT; never DELETE).
