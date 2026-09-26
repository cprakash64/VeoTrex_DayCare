# Runbook: classroom presence sources (manual, staff roster, child attendance, pickup)

Operator guide for V1-04B / V1-04C / V1-04D / V1-04E (ADRs 0025, 0026, 0027, 0028). Configured
classroom policy only - nothing here is a legal compliance determination or identity verification.

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
   the reference from your own attendance system. Do not enter dates of birth, medical notes or
   anything else; the form does not accept them. Adults who may collect the child are managed
   separately (see *Pickup and release* below).
3. **Mode** - on the classroom page, Presence sources -> "Child attendance + staff check-ins".
4. **Daily** - check children and staff in on the classroom page as they arrive; use *Move here*
   to move a child or teacher from another room (one step, never in two rooms); at pickup use
   *Release child* (below). Staff check themselves out when they leave.

## Pickup and release (V1-04E, ADR 0028)

1. **Contacts** - on *Guardians & contacts*, add each adult who may be involved: a name staff
   recognise and, optionally, a reference from your own records. No photos, IDs, phone numbers or
   emails are entered.
2. **Authorized pickup people** - open a child from Child rosters. *Add association*: choose the
   contact, describe the relationship in your own words, and choose **explicitly** whether they are
   authorized for pickup. For a one-off pickup set *Authorized from / until* (facility time). The
   relationship never authorizes anything by itself - a "Father" with pickup set to No cannot
   collect the child.
3. **At the door** - on the classroom page choose *Release child* for the child, pick one of the
   adults listed as authorized right now, choose how you confirmed them (*Known to staff*,
   *Confirmed by me*, *Photo ID checked* - nothing is scanned or stored), tick the confirmation,
   and submit. The child is checked out and the release is recorded with the exact authorization
   used. Adults who are associated but not currently authorized are shown with the reason and
   cannot be chosen.
4. **Corrections** - *Correction -> Administrative check-out* ends a stay without a pickup record
   (a mistaken check-in, a lapsed stay). It is shown and audited as administrative, never as a
   release.
5. **If a release is refused** - "not authorized for pickup", "authorization has ended" or "has
   not started": an administrator updates the child's authorized pickup people; staff never work
   around it by editing a relationship label.

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
- Cameras, Ring and face recognition never authorize or record a release, and never identify a
  parent, guardian or any other adult. Contact names and relationship labels never appear in audit
  records or logs.

## Database notes

- Migrations 0011 (staff roster), 0012 (child attendance) and 0013 (guardian contacts, links and
  release events) are additive. They have **not** been applied to production; apply them only
  through the normal reviewed deployment.
- The 0012 downgrade refuses while any child, attendance event, visitor-only report or
  attendance-mode classroom exists (the 0011 downgrade similarly refuses while roster-mode reports
  exist), and the 0013 downgrade refuses while any contact, link or release exists. This is
  deliberate: a downgrade must not destroy roster or pickup history or invent counts.
- After applying a migration, re-run `veotrex-db-runtime-role apply` so the runtime role receives
  exactly the new tables' grants (child profiles, guardian contacts and child guardian links:
  SELECT/INSERT/UPDATE; attendance and release events: SELECT/INSERT; never DELETE).
