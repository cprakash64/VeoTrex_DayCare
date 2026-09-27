"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiErrorMessage, type FacilitySummary, validateClassroomForm } from "../../../lib/classrooms";

export function CreateClassroomForm({ facilities }: { facilities: ReadonlyArray<FacilitySummary> }) {
  const router = useRouter();
  const [facilityId, setFacilityId] = useState(facilities[0]?.facility_id ?? "");
  const [name, setName] = useState("");
  const [ageBand, setAgeBand] = useState("");
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const checked = validateClassroomForm(name, ageBand);
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setErrors({});
    setMessage(null);
    setWorking(true);
    try {
      const response = await fetch("/api/classrooms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ facility_id: facilityId, ...checked.value }),
      });
      const body = (await response.json()) as { classroom_id?: string | null; category?: string | null };
      if (!response.ok) {
        setMessage(apiErrorMessage(body.category));
        return;
      }
      setName("");
      setAgeBand("");
      if (body.classroom_id) router.push(`/app/classrooms/${body.classroom_id}`);
      else router.refresh();
    } catch {
      setMessage(apiErrorMessage(null));
    } finally {
      setWorking(false);
    }
  }

  return (
    <section aria-labelledby="add-classroom">
      <h2 id="add-classroom">Add a classroom</h2>
      <form className="stack" onSubmit={submit}>
        {facilities.length > 1 ? (
          <>
            <label htmlFor="classroom-facility">Facility</label>
            <select
              id="classroom-facility"
              value={facilityId}
              onChange={(event) => setFacilityId(event.target.value)}
              disabled={working}
            >
              {facilities.map((facility) => (
                <option key={facility.facility_id} value={facility.facility_id}>
                  {facility.name}
                </option>
              ))}
            </select>
          </>
        ) : null}
        <label htmlFor="classroom-name">Classroom name</label>
        <input id="classroom-name" maxLength={200} value={name} onChange={(event) => setName(event.target.value)} disabled={working} />
        {errors.name ? <p className="field-error">{errors.name}</p> : null}
        <label htmlFor="classroom-age-band">Age band label (optional, your own wording)</label>
        <input
          id="classroom-age-band"
          maxLength={64}
          placeholder="e.g. Toddler, Pre-K, Mixed"
          value={ageBand}
          onChange={(event) => setAgeBand(event.target.value)}
          disabled={working}
        />
        {errors.age_band_label ? <p className="field-error">{errors.age_band_label}</p> : null}
        <p className="consent">Never enter a child&apos;s name in a classroom name or label.</p>
        <button className="primary" type="submit" disabled={working}>
          {working ? "Adding…" : "Add classroom"}
        </button>
        {message ? <p role="alert">{message}</p> : null}
      </form>
    </section>
  );
}
