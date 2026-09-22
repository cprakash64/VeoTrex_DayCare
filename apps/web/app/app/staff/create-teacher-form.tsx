"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export function CreateTeacherForm() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [consent, setConsent] = useState(false);
  const [state, setState] = useState<"ready" | "working" | "failed">("ready");

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state === "working" || !consent || !name.trim()) return;
    setState("working");
    try {
      const response = await fetch("/api/staff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: name.trim() }),
      });
      const body = (await response.json()) as { staff_id?: string | null };
      if (!response.ok) throw new Error("create failed");
      setName("");
      setConsent(false);
      setState("ready");
      if (body.staff_id) router.push(`/app/staff/${body.staff_id}`);
      else router.refresh();
    } catch {
      setState("failed");
    }
  }

  return (
    <section aria-labelledby="add-teacher">
      <h2 id="add-teacher">Add a teacher</h2>
      <form className="stack" onSubmit={submit}>
        <label htmlFor="teacher-name">Display name</label>
        <input
          id="teacher-name"
          type="text"
          maxLength={200}
          required
          value={name}
          onChange={(event) => setName(event.target.value)}
          disabled={state === "working"}
        />
        <p className="consent">
          Only enroll an adult staff member who has agreed to face recognition for workplace safety
          monitoring at this organization. Their photos and derived face templates are stored privately
          for this organization only, are never used for children, and are removed when the teacher is
          deleted here.
        </p>
        <label>
          <input
            type="checkbox"
            checked={consent}
            onChange={(event) => setConsent(event.target.checked)}
            disabled={state === "working"}
          />{" "}
          This person is an adult staff member who has given consent.
        </label>
        <button className="primary" type="submit" disabled={state === "working" || !consent || !name.trim()}>
          {state === "working" ? "Adding…" : "Add teacher"}
        </button>
        {state === "failed" ? <p role="alert">The teacher could not be added. Try again.</p> : null}
      </form>
    </section>
  );
}
