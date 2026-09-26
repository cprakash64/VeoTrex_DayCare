"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  ATTENDANCE_LEASE_CHOICES,
  attendanceEntryView,
  attendanceSummaryLines,
  childErrorMessage,
  type ClassroomAttendance,
  DEFAULT_ATTENDANCE_LEASE_SECONDS,
} from "../../../../lib/children";
import { ATTENDANCE_MODE } from "../../../../lib/staff-presence";

const EVENT_LABELS: Readonly<Record<string, string>> = {
  CHECKED_IN: "Checked in",
  REFRESHED: "Attendance refreshed",
  CHECKED_OUT: "Checked out",
};

/**
 * Children checked into this classroom by an operator (V1-04D). Every change is an explicit
 * operator action on a named roster entry; nothing here comes from a camera. Names are shown
 * only on this authenticated page, as text. Leases are re-checked on this page's clock.
 */
export function ChildAttendanceCard({
  classroomId,
  attendance,
}: {
  classroomId: string;
  attendance: ClassroomAttendance | null;
}) {
  const router = useRouter();
  const [now, setNow] = useState(() => Date.now());
  const [lease, setLease] = useState(String(DEFAULT_ATTENDANCE_LEASE_SECONDS));
  const [message, setMessage] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(tick);
  }, []);

  if (attendance === null) {
    return (
      <section aria-labelledby="attendance-heading">
        <h2 id="attendance-heading">Child attendance</h2>
        <p>Child attendance is unavailable right now.</p>
      </section>
    );
  }
  const canEdit = attendance.can_administer && attendance.classroom_active;
  const counted = attendance.presence_source_mode === ATTENDANCE_MODE;

  async function act(action: "check-in" | "check-out" | "refresh", childId: string) {
    setWorking(`${action}:${childId}`);
    setMessage(null);
    const body =
      action === "check-out"
        ? { child_profile_id: childId }
        : { child_profile_id: childId, lease_seconds: Number(lease) };
    try {
      const response = await fetch(`/api/classrooms/${classroomId}/attendance/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = (await response.json()) as { category?: string | null };
      if (!response.ok) setMessage(childErrorMessage(result.category));
      else router.refresh();
    } catch {
      setMessage(childErrorMessage(null));
    } finally {
      setWorking(null);
    }
  }

  return (
    <section aria-labelledby="attendance-heading">
      <h2 id="attendance-heading">Child attendance</h2>
      <p className="staff-meta">
        Children checked into this classroom by staff. Attendance expires automatically unless refreshed, and
        cameras never check a child in or out.
      </p>
      {counted ? (
        <ul aria-label="Attendance count">
          {attendanceSummaryLines(attendance.summary).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : (
        <p className="staff-meta">
          Attendance is recorded but not used for the ratio until this classroom takes its child count from
          attendance.
        </p>
      )}
      {canEdit ? (
        <p>
          <label htmlFor="attendance-lease">Attendance lasts</label>{" "}
          <select
            id="attendance-lease"
            value={lease}
            onChange={(event) => setLease(event.target.value)}
            disabled={working !== null}
          >
            {ATTENDANCE_LEASE_CHOICES.map((choice) => (
              <option key={choice.seconds} value={String(choice.seconds)}>
                {choice.label}
              </option>
            ))}
          </select>
        </p>
      ) : null}
      {attendance.children.length === 0 ? (
        <p>No children are on this facility&apos;s roster yet. Add them on the Child rosters page.</p>
      ) : (
        <ul className="staff-list" aria-label="Child attendance">
          {attendance.children.map((entry) => {
            const view = attendanceEntryView(entry, now);
            const busy = working !== null;
            return (
              <li className="staff-row" key={entry.child_profile_id}>
                <div>
                  <h3>{entry.display_name}</h3>
                  <p className="staff-meta">
                    {view.label}
                    {view.state === "here" && entry.checked_in_at
                      ? ` · since ${new Date(entry.checked_in_at).toLocaleTimeString()}`
                      : ""}
                    {view.state === "here" && entry.valid_until
                      ? ` · until ${new Date(entry.valid_until).toLocaleTimeString()}`
                      : ""}
                  </p>
                  {canEdit ? (
                    <p>
                      {view.canCheckIn ? (
                        <button
                          className="primary"
                          type="button"
                          disabled={busy}
                          onClick={() => act("check-in", entry.child_profile_id)}
                        >
                          {view.state === "elsewhere" ? "Move here" : "Check in"}
                        </button>
                      ) : null}{" "}
                      {view.canRefresh ? (
                        <button
                          className="secondary"
                          type="button"
                          disabled={busy}
                          onClick={() => act("refresh", entry.child_profile_id)}
                        >
                          Refresh
                        </button>
                      ) : null}{" "}
                      {view.canCheckOut ? (
                        <button
                          className="secondary danger"
                          type="button"
                          disabled={busy}
                          onClick={() => act("check-out", entry.child_profile_id)}
                        >
                          Check out
                        </button>
                      ) : null}
                    </p>
                  ) : null}
                </div>
                <span className={`badge ${view.tone}`}>
                  {view.state === "here" ? "Here" : view.state === "expired" ? "Expired" : "—"}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {message ? <p role="alert">{message}</p> : null}
      {attendance.recent_events.length > 0 ? (
        <details>
          <summary>Recent check-ins and check-outs</summary>
          <ul aria-label="Recent attendance events">
            {attendance.recent_events.map((event) => (
              <li key={event.event_id}>
                {new Date(event.occurred_at).toLocaleTimeString()} · {event.display_name} ·{" "}
                {EVENT_LABELS[event.event_type] ?? event.event_type}
                {event.recorded_by_caller ? " · by you" : ""}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
