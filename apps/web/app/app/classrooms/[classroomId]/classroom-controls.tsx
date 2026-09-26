"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiErrorMessage, type Classroom, validateClassroomForm } from "../../../../lib/classrooms";

export function ClassroomControls({ classroom }: { classroom: Classroom }) {
  const router = useRouter();
  const [name, setName] = useState(classroom.name);
  const [ageBand, setAgeBand] = useState(classroom.age_band_label ?? "");
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const base = `/api/classrooms/${classroom.classroom_id}`;

  async function send(url: string, body: unknown) {
    setWorking(true);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method: url === base ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = (await response.json()) as { category?: string | null };
      if (!response.ok) setMessage(apiErrorMessage(result.category));
      else router.refresh();
    } catch {
      setMessage(apiErrorMessage(null));
    } finally {
      setWorking(false);
    }
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const checked = validateClassroomForm(name, ageBand);
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setErrors({});
    await send(base, checked.value);
  }

  const active = classroom.status === "ACTIVE";
  return (
    <section aria-labelledby="classroom-settings">
      <h2 id="classroom-settings">Classroom settings</h2>
      <form className="stack" onSubmit={save}>
        <label htmlFor="edit-classroom-name">Classroom name</label>
        <input id="edit-classroom-name" maxLength={200} value={name} onChange={(event) => setName(event.target.value)} disabled={working} />
        {errors.name ? <p className="field-error">{errors.name}</p> : null}
        <label htmlFor="edit-classroom-age-band">Age band label (optional, your own wording)</label>
        <input id="edit-classroom-age-band" maxLength={64} value={ageBand} onChange={(event) => setAgeBand(event.target.value)} disabled={working} />
        {errors.age_band_label ? <p className="field-error">{errors.age_band_label}</p> : null}
        <button className="primary" type="submit" disabled={working}>Save classroom</button>
      </form>
      <button
        className={active ? "secondary danger" : "secondary"}
        type="button"
        disabled={working}
        onClick={() => send(`${base}/state`, { active: !active })}
      >
        {active ? "Deactivate classroom" : "Reactivate classroom"}
      </button>
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
