"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  type ClassroomStaffPresence,
  countedLabel,
  DEFAULT_LEASE_SECONDS,
  LEASE_CHOICES,
  MANUAL_MODE,
  presenceModeLabel,
  ROSTER_MODE,
  rosterSummaryLines,
  staffEntryView,
  staffErrorMessage,
} from "../../../../lib/staff-presence";

const EVENT_LABELS: Readonly<Record<string, string>> = {
  CHECKED_IN: "Checked in",
  REFRESHED: "Check-in refreshed",
  CHECKED_OUT: "Checked out",
};

/**
 * Adult staff checked into this classroom by an operator (V1-04C). Every change is an explicit
 * operator action on a named staff profile; nothing here is filled in from a camera or a face
 * match. Leases are re-checked on this page's clock every second.
 */
export function StaffPresenceCard({
  classroomId,
  presence,
}: {
  classroomId: string;
  presence: ClassroomStaffPresence | null;
}) {
  const router = useRouter();
  const [now, setNow] = useState(() => Date.now());
  const [lease, setLease] = useState(String(DEFAULT_LEASE_SECONDS));
  const [message, setMessage] = useState<string | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(tick);
  }, []);

  if (presence === null) {
    return (
      <section aria-labelledby="staff-presence-heading">
        <h2 id="staff-presence-heading">Staff presence</h2>
        <p>Staff presence is unavailable right now.</p>
      </section>
    );
  }
  const canEdit = presence.can_administer;
  const rosterMode = presence.presence_source_mode === ROSTER_MODE;

  async function send(key: string, url: string, body: unknown) {
    setWorking(key);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = (await response.json()) as { category?: string | null };
      if (!response.ok) setMessage(staffErrorMessage(result.category));
      else router.refresh();
    } catch {
      setMessage(staffErrorMessage(null));
    } finally {
      setWorking(null);
    }
  }

  function act(action: "check-in" | "check-out" | "refresh", staffId: string) {
    const body =
      action === "check-out"
        ? { staff_profile_id: staffId }
        : { staff_profile_id: staffId, lease_seconds: Number(lease) };
    return send(`${action}:${staffId}`, `/api/classrooms/${classroomId}/staff-presence/${action}`, body);
  }

  const summary = rosterSummaryLines(presence.summary);
  return (
    <section aria-labelledby="staff-presence-heading">
      <h2 id="staff-presence-heading">Staff presence</h2>
      <p className="staff-meta">
        Adult staff checked into this classroom by an operator. A check-in expires automatically unless it
        is refreshed, and nobody is ever checked in from a camera or a face match.
      </p>
      <p>
        Staff count source: <strong>{rosterMode ? "Staff roster" : "Manual report"}</strong> ·{" "}
        {presenceModeLabel(presence.presence_source_mode)}
      </p>
      {rosterMode ? (
        <ul aria-label="Staff roster count">
          {summary.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : (
        <p className="staff-meta">
          Check-ins are recorded but not used for the ratio while this classroom takes its staff count from
          the manual report.
        </p>
      )}
      {canEdit ? (
        <div className="stack">
          <button
            className="secondary"
            type="button"
            disabled={working !== null}
            onClick={() =>
              send("mode", `/api/classrooms/${classroomId}/presence-source-mode`, {
                mode: rosterMode ? MANUAL_MODE : ROSTER_MODE,
              })
            }
          >
            {rosterMode ? "Use the manual staff count instead" : "Count staff from check-ins"}
          </button>
          <label htmlFor="staff-lease">Check-in lasts</label>
          <select
            id="staff-lease"
            value={lease}
            onChange={(event) => setLease(event.target.value)}
            disabled={working !== null}
          >
            {LEASE_CHOICES.map((choice) => (
              <option key={choice.seconds} value={String(choice.seconds)}>
                {choice.label}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {presence.staff.length === 0 ? (
        <p>No staff are on this facility&apos;s roster yet. Add them from each person&apos;s Staff page.</p>
      ) : (
        <ul className="staff-list" aria-label="Facility staff roster">
          {presence.staff.map((entry) => {
            const view = staffEntryView(entry, now);
            const busy = working !== null;
            return (
              <li className="staff-row" key={entry.staff_profile_id}>
                <div>
                  <h3>{entry.display_name}</h3>
                  <p className="staff-meta">
                    <span className={`badge ${entry.counts_toward_ratio ? "ready" : "inactive"}`}>
                      {countedLabel(entry)}
                    </span>
                  </p>
                  <p className="staff-meta">
                    {view.label}
                    {view.state === "here" && entry.checked_in_at
                      ? ` · since ${new Date(entry.checked_in_at).toLocaleTimeString()}`
                      : ""}
                  </p>
                  {canEdit && presence.classroom_active ? (
                    <p>
                      {view.canCheckIn ? (
                        <button
                          className="primary"
                          type="button"
                          disabled={busy}
                          onClick={() => act("check-in", entry.staff_profile_id)}
                        >
                          {view.state === "elsewhere" ? "Move here" : "Check in"}
                        </button>
                      ) : null}{" "}
                      {view.canRefresh ? (
                        <button
                          className="secondary"
                          type="button"
                          disabled={busy}
                          onClick={() => act("refresh", entry.staff_profile_id)}
                        >
                          Refresh
                        </button>
                      ) : null}{" "}
                      {view.canCheckOut ? (
                        <button
                          className="secondary danger"
                          type="button"
                          disabled={busy}
                          onClick={() => act("check-out", entry.staff_profile_id)}
                        >
                          Check out
                        </button>
                      ) : null}
                    </p>
                  ) : null}
                </div>
                <span className={`badge ${view.tone}`}>{view.state === "here" ? "Here" : view.state === "expired" ? "Expired" : "—"}</span>
              </li>
            );
          })}
        </ul>
      )}
      {message ? <p role="alert">{message}</p> : null}
      {presence.recent_events.length > 0 ? (
        <details>
          <summary>Recent check-ins and check-outs</summary>
          <ul aria-label="Recent staff presence events">
            {presence.recent_events.map((event) => (
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
