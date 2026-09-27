"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  type FacilityGuardians,
  type GuardianSummary,
  guardianErrorMessage,
  guardianStatusLabel,
  validateGuardianForm,
} from "../../../lib/guardians";

function GuardianForm({
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
    const checked = validateGuardianForm(name, reference);
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
        Reference in your own records (optional)
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
 * One facility's adult contacts (V1-04E). Operators add, rename, deactivate, reactivate and
 * archive entries. There is deliberately no photo, identity-document, phone, email or camera field.
 */
export function GuardianRosterPanel({ roster, canAdd }: { roster: FacilityGuardians; canAdd: boolean }) {
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
      if (!response.ok) setMessage(guardianErrorMessage(result.category));
      else router.refresh();
    } catch {
      setMessage(guardianErrorMessage(null));
    } finally {
      setWorking(null);
    }
  }

  const busy = working !== null;
  const lifecycle = (contact: GuardianSummary) => {
    const verbs: Array<{ verb: string; label: string; danger?: boolean }> = [];
    if (contact.status === "ACTIVE") verbs.push({ verb: "deactivate", label: "Deactivate" });
    if (contact.status === "INACTIVE") verbs.push({ verb: "activate", label: "Reactivate" });
    if (contact.status !== "ARCHIVED") verbs.push({ verb: "archive", label: "Archive", danger: true });
    return verbs;
  };

  return (
    <section aria-labelledby={`contacts-${roster.facility_id}`}>
      <h2 id={`contacts-${roster.facility_id}`}>{roster.facility_name}</h2>
      {roster.guardians.length === 0 ? <p>No contacts at this facility yet.</p> : null}
      <ul className="staff-list" aria-label={`Contacts at ${roster.facility_name}`}>
        {roster.guardians.map((contact) => (
          <li className="staff-row" key={contact.guardian_contact_id}>
            <div>
              <h3>{contact.display_name}</h3>
              <p className="staff-meta">
                {contact.active_link_count === 1
                  ? "Associated with 1 child"
                  : `Associated with ${contact.active_link_count} children`}
                {contact.external_reference ? ` · Reference ${contact.external_reference}` : ""}
              </p>
              {contact.can_administer && contact.status !== "ARCHIVED" ? (
                <details>
                  <summary>Edit or change status</summary>
                  <GuardianForm
                    submitLabel="Save"
                    busy={busy}
                    initial={{
                      display_name: contact.display_name,
                      external_reference: contact.external_reference ?? "",
                    }}
                    onSubmit={(value) =>
                      send(`edit:${contact.guardian_contact_id}`, `/api/guardians/${contact.guardian_contact_id}`, "PATCH", value)
                    }
                  />
                  <p>
                    {lifecycle(contact).map(({ verb, label, danger }) => (
                      <button
                        key={verb}
                        className={danger ? "secondary danger" : "secondary"}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          send(`${verb}:${contact.guardian_contact_id}`, `/api/guardians/${contact.guardian_contact_id}/${verb}`, "POST")
                        }
                      >
                        {label}
                      </button>
                    ))}
                  </p>
                </details>
              ) : null}
            </div>
            <span className={`badge ${contact.status === "ACTIVE" ? "ready" : "inactive"}`}>
              {guardianStatusLabel(contact.status)}
            </span>
          </li>
        ))}
      </ul>
      {canAdd ? (
        <details>
          <summary>Add a contact</summary>
          <GuardianForm
            submitLabel="Add contact"
            busy={busy}
            initial={{ display_name: "", external_reference: "" }}
            onSubmit={(value) => send("add", `/api/facilities/${roster.facility_id}/guardians`, "POST", value)}
          />
        </details>
      ) : null}
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
