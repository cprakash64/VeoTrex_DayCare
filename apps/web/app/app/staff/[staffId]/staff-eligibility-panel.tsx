"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { type EligibilityAssignment, eligibilityLabel, staffErrorMessage } from "../../../../lib/staff-presence";

export type FacilityEligibility = Readonly<{
  facility_id: string;
  facility_name: string;
  can_administer: boolean;
  current: EligibilityAssignment | null;
  history: number;
}>;

/**
 * Which facilities' rosters this adult staff member is on, and whether an operator designated
 * them as counting toward the configured classroom policy (V1-04C). This is the operator's own
 * designation; VeoTrex does not check a licence or a qualification.
 */
export function StaffEligibilityPanel({
  staffId,
  staffActive,
  facilities,
}: {
  staffId: string;
  staffActive: boolean;
  facilities: ReadonlyArray<FacilityEligibility>;
}) {
  const router = useRouter();
  const [working, setWorking] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [choice, setChoice] = useState<Readonly<Record<string, string>>>({});

  async function send(key: string, url: string, method: "POST" | "PATCH", body?: unknown) {
    setWorking(key);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
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

  return (
    <section aria-labelledby="eligibility-heading">
      <h2 id="eligibility-heading">Facility roster and configured ratio</h2>
      <p className="staff-meta">
        Choose whether this person counts toward each facility&apos;s configured classroom policy. This is your
        organization&apos;s designation; VeoTrex does not verify licences or qualifications.
      </p>
      {facilities.length === 0 ? <p>No facility is available to you.</p> : null}
      <ul className="staff-list" aria-label="Facility roster designations">
        {facilities.map((facility) => {
          const base = `/api/facilities/${facility.facility_id}/staff-ratio-eligibility`;
          const current = facility.current;
          const busy = working !== null;
          const selected = choice[facility.facility_id] ?? "yes";
          return (
            <li className="staff-row" key={facility.facility_id}>
              <div>
                <h3>{facility.facility_name}</h3>
                {current ? (
                  <p className="staff-meta">
                    On this facility&apos;s roster · Counts toward configured classroom policy:{" "}
                    <strong>{current.counts_toward_ratio ? "Yes" : "No"}</strong>
                    {current.in_effect ? "" : " · not in effect today"} · revision {current.revision}
                  </p>
                ) : (
                  <p className="staff-meta">Not on this facility&apos;s roster.</p>
                )}
                {facility.history > 0 ? (
                  <p className="staff-meta">{facility.history} earlier designation(s) kept in history.</p>
                ) : null}
                {facility.can_administer && current ? (
                  <p>
                    <button
                      className="secondary"
                      type="button"
                      disabled={busy || !staffActive}
                      onClick={() =>
                        send(`toggle:${facility.facility_id}`, `${base}/${current.eligibility_id}`, "PATCH", {
                          counts_toward_ratio: !current.counts_toward_ratio,
                        })
                      }
                    >
                      {current.counts_toward_ratio ? "Mark as not counting" : "Mark as counting"}
                    </button>{" "}
                    <button
                      className="secondary danger"
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        send(`remove:${facility.facility_id}`, `${base}/${current.eligibility_id}/deactivate`, "POST")
                      }
                    >
                      Remove from roster
                    </button>
                  </p>
                ) : null}
                {facility.can_administer && !current && staffActive ? (
                  <p>
                    <label htmlFor={`eligibility-${facility.facility_id}`}>
                      Counts toward configured classroom policy
                    </label>
                    <select
                      id={`eligibility-${facility.facility_id}`}
                      value={selected}
                      onChange={(event) => setChoice({ ...choice, [facility.facility_id]: event.target.value })}
                      disabled={busy}
                    >
                      <option value="yes">Yes</option>
                      <option value="no">No</option>
                    </select>{" "}
                    <button
                      className="primary"
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        send(`add:${facility.facility_id}`, base, "POST", {
                          staff_profile_id: staffId,
                          counts_toward_ratio: selected === "yes",
                        })
                      }
                    >
                      Add to roster
                    </button>
                  </p>
                ) : null}
              </div>
              <span className={`badge ${current?.counts_toward_ratio ? "ready" : "inactive"}`}>
                {current ? eligibilityLabel(current.counts_toward_ratio) : "Not on roster"}
              </span>
            </li>
          );
        })}
      </ul>
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
