"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  type ChildSummary,
  childErrorMessage,
  childStatusLabel,
  type FacilityChildren,
  validateChildForm,
} from "../../../lib/children";

function ChildForm({
  submitLabel,
  initial,
  busy,
  onSubmit,
}: {
  submitLabel: string;
  initial: { display_name: string; external_reference: string };
  busy: boolean;
  onSubmit: (value: { display_name: string; external_reference: string | null }) => Promise<void>;
}) {
  const [name, setName] = useState(initial.display_name);
  const [reference, setReference] = useState(initial.external_reference);
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const checked = validateChildForm(name, reference);
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setErrors({});
    await onSubmit(checked.value);
  }

  return (
    <form className="stack" onSubmit={submit} noValidate>
      <label>
        Name shown to staff
        <input
          value={name}
          maxLength={120}
          onChange={(event) => setName(event.target.value)}
          disabled={busy}
          aria-invalid={errors.display_name ? true : undefined}
        />
      </label>
      {errors.display_name ? <p className="field-error">{errors.display_name}</p> : null}
      <label>
        Reference in your attendance system (optional)
        <input
          value={reference}
          maxLength={64}
          onChange={(event) => setReference(event.target.value)}
          disabled={busy}
          aria-invalid={errors.external_reference ? true : undefined}
        />
      </label>
      {errors.external_reference ? <p className="field-error">{errors.external_reference}</p> : null}
      <button className="primary" type="submit" disabled={busy}>
        {submitLabel}
      </button>
    </form>
  );
}

/**
 * One facility's child roster (V1-04D). Operators add, rename, deactivate, reactivate and
 * archive entries. There is deliberately no photo, date-of-birth, guardian or camera field.
 */
export function ChildRosterPanel({ roster, canAdd }: { roster: FacilityChildren; canAdd: boolean }) {
  const router = useRouter();
  const [working, setWorking] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function send(key: string, url: string, method: "POST" | "PATCH", body?: unknown): Promise<void> {
    setWorking(key);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
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

  const busy = working !== null;
  const lifecycle = (child: ChildSummary) => {
    const verbs: Array<{ verb: string; label: string; danger?: boolean }> = [];
    if (child.status === "ACTIVE") verbs.push({ verb: "deactivate", label: "Deactivate" });
    if (child.status === "INACTIVE") verbs.push({ verb: "activate", label: "Reactivate" });
    if (child.status !== "ARCHIVED") verbs.push({ verb: "archive", label: "Archive", danger: true });
    return verbs;
  };

  return (
    <section aria-labelledby={`children-${roster.facility_id}`}>
      <h2 id={`children-${roster.facility_id}`}>{roster.facility_name}</h2>
      {roster.children.length === 0 ? <p>No children on this roster yet.</p> : null}
      <ul className="staff-list" aria-label={`Child roster for ${roster.facility_name}`}>
        {roster.children.map((child) => (
          <li className="staff-row" key={child.child_id}>
            <div>
              <h3>{child.display_name}</h3>
              <p className="staff-meta">
                {child.external_reference ? `Reference ${child.external_reference}` : "No external reference"}
              </p>
              {child.can_administer && child.status !== "ARCHIVED" ? (
                <details>
                  <summary>Edit or change status</summary>
                  <ChildForm
                    submitLabel="Save"
                    busy={busy}
                    initial={{ display_name: child.display_name, external_reference: child.external_reference ?? "" }}
                    onSubmit={(value) => send(`edit:${child.child_id}`, `/api/children/${child.child_id}`, "PATCH", value)}
                  />
                  <p>
                    {lifecycle(child).map(({ verb, label, danger }) => (
                      <button
                        key={verb}
                        className={danger ? "secondary danger" : "secondary"}
                        type="button"
                        disabled={busy}
                        onClick={() => send(`${verb}:${child.child_id}`, `/api/children/${child.child_id}/${verb}`, "POST")}
                      >
                        {label}
                      </button>
                    ))}
                  </p>
                </details>
              ) : null}
            </div>
            <span className={`badge ${child.status === "ACTIVE" ? "ready" : "inactive"}`}>{childStatusLabel(child.status)}</span>
          </li>
        ))}
      </ul>
      {canAdd ? (
        <details>
          <summary>Add a child to this roster</summary>
          <ChildForm
            submitLabel="Add child"
            busy={busy}
            initial={{ display_name: "", external_reference: "" }}
            onSubmit={(value) => send("add", `/api/facilities/${roster.facility_id}/children`, "POST", value)}
          />
        </details>
      ) : null}
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
