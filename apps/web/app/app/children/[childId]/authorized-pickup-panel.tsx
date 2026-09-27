"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  type ChildGuardians,
  type ChildLink,
  formatInFacility,
  guardianErrorMessage,
  type GuardianSummary,
  type LinkForm,
  pickupStatusLabel,
  utcToFacilityLocal,
  facilityLocalToUtc,
  cleanRelationshipLabel,
  cleanLinkNote,
  validateLinkForm,
} from "../../../../lib/guardians";

const EMPTY_FORM: LinkForm = {
  guardian_contact_id: "",
  relationship_label: "",
  pickup: "",
  effective_from: "",
  effective_until: "",
  note: "",
};

function period(link: ChildLink, zone: string): string {
  const from = formatInFacility(link.effective_from, zone);
  return link.effective_until ? `${from} – ${formatInFacility(link.effective_until, zone)}` : `From ${from}, no end date`;
}

function EditLink({
  link,
  zone,
  busy,
  onSave,
}: {
  link: ChildLink;
  zone: string;
  busy: boolean;
  onSave: (changes: Record<string, unknown>) => Promise<void>;
}) {
  const [label, setLabel] = useState(link.relationship_label);
  const [from, setFrom] = useState(utcToFacilityLocal(link.effective_from, zone));
  const [until, setUntil] = useState(link.effective_until ? utcToFacilityLocal(link.effective_until, zone) : "");
  const [note, setNote] = useState(link.note ?? "");
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleanLabel = cleanRelationshipLabel(label);
    const begins = facilityLocalToUtc(from, zone);
    const ends = until ? facilityLocalToUtc(until, zone) : null;
    const cleanNote = cleanLinkNote(note);
    if (cleanLabel === null) return setError("Describe the relationship in up to 64 characters.");
    if (begins === null || (until && ends === null)) return setError("Enter valid times.");
    if (ends && Date.parse(ends) <= Date.parse(begins)) return setError("The end must be after the start.");
    if (cleanNote === undefined) return setError("Keep the note under 200 characters, without < or >.");
    setError(null);
    await onSave({ relationship_label: cleanLabel, effective_from: begins, effective_until: ends, note: cleanNote });
  }

  return (
    <form className="stack" onSubmit={submit} noValidate>
      <label>
        Relationship (your words)
        <input value={label} maxLength={64} onChange={(event) => setLabel(event.target.value)} disabled={busy} />
      </label>
      <label>
        Authorized from ({zone})
        <input type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} disabled={busy} />
      </label>
      <label>
        Authorized until (optional)
        <input type="datetime-local" value={until} onChange={(event) => setUntil(event.target.value)} disabled={busy} />
      </label>
      <label>
        Note (optional)
        <input value={note} maxLength={200} onChange={(event) => setNote(event.target.value)} disabled={busy} />
      </label>
      {error ? <p className="field-error">{error}</p> : null}
      <button className="primary" type="submit" disabled={busy}>
        Save
      </button>
    </form>
  );
}

/**
 * Authorized pickup people for one child (V1-04E). Each row is an association with an adult
 * contact: the operator's relationship label, and - separately - whether that adult is authorized
 * for pickup and when. Being associated never authorizes pickup by itself.
 */
export function AuthorizedPickupPanel({
  links,
  contacts,
}: {
  links: ChildGuardians;
  contacts: ReadonlyArray<GuardianSummary>;
}) {
  const router = useRouter();
  const zone = links.facility_timezone;
  const [form, setForm] = useState<LinkForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const [working, setWorking] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const canEdit = links.can_administer && links.child_status !== "ARCHIVED";
  const busy = working !== null;
  const linked = new Set(links.links.filter((item) => item.status === "ACTIVE").map((item) => item.guardian_contact_id));
  const available = contacts.filter((item) => item.status !== "ARCHIVED" && !linked.has(item.guardian_contact_id));
  const base = `/api/children/${links.child_profile_id}/guardians`;

  async function send(key: string, url: string, method: "POST" | "PATCH", body?: unknown): Promise<boolean> {
    setWorking(key);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const result = (await response.json()) as { category?: string | null };
      if (!response.ok) {
        setMessage(guardianErrorMessage(result.category));
        return false;
      }
      router.refresh();
      return true;
    } catch {
      setMessage(guardianErrorMessage(null));
      return false;
    } finally {
      setWorking(null);
    }
  }

  async function add(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const checked = validateLinkForm(form, zone);
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setErrors({});
    if (await send("add", base, "POST", checked.value)) setForm(EMPTY_FORM);
  }

  const update = (key: keyof LinkForm) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [key]: event.target.value });

  return (
    <section aria-labelledby="pickup-heading">
      <h2 id="pickup-heading">Authorized pickup people</h2>
      <p className="staff-meta">
        The relationship is your own description and grants nothing by itself. Only people marked
        &quot;Authorized for pickup&quot;, within their dates, can be chosen when this child is released. Staff
        confirm who is collecting at the door; VeoTrex does not identify anyone.
      </p>
      {links.links.length === 0 ? <p>No contacts are associated with this child yet.</p> : null}
      <ul className="staff-list" aria-label="Associated contacts">
        {links.links.map((item) => {
          const active = item.status === "ACTIVE";
          const authorized = item.pickup_status === "AUTHORIZED";
          return (
            <li className="staff-row" key={item.link_id}>
              <div>
                <h3>{item.guardian_display_name}</h3>
                <p className="staff-meta">
                  {item.relationship_label} · {pickupStatusLabel(item.pickup_status)}
                  {active ? ` · ${period(item, zone)}` : ""}
                  {item.guardian_status !== "ACTIVE" ? " · contact inactive" : ""}
                </p>
                {item.note ? <p className="staff-meta">Note: {item.note}</p> : null}
                {canEdit && active ? (
                  <>
                    <p>
                      <button
                        className="secondary"
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          send(`pickup:${item.link_id}`, `${base}/${item.link_id}`, "PATCH", {
                            pickup_authorized: !item.pickup_authorized,
                          })
                        }
                      >
                        {item.pickup_authorized ? "Disable pickup permission" : "Enable pickup permission"}
                      </button>{" "}
                      <button
                        className="secondary danger"
                        type="button"
                        disabled={busy}
                        onClick={() => send(`end:${item.link_id}`, `${base}/${item.link_id}/deactivate`, "POST")}
                      >
                        Deactivate association
                      </button>
                    </p>
                    <details>
                      <summary>Edit</summary>
                      <EditLink
                        link={item}
                        zone={zone}
                        busy={busy}
                        onSave={async (changes) => {
                          await send(`edit:${item.link_id}`, `${base}/${item.link_id}`, "PATCH", changes);
                        }}
                      />
                    </details>
                  </>
                ) : null}
              </div>
              <span className={`badge ${authorized ? "ready" : "inactive"}`}>
                {!active ? "Ended" : authorized ? "Authorized for pickup" : "Not currently authorized"}
              </span>
            </li>
          );
        })}
      </ul>
      {canEdit ? (
        <details>
          <summary>Add association</summary>
          {available.length === 0 ? (
            <p>
              Every contact at this facility is already associated. Add new people on the{" "}
              <a href="/app/guardians">Guardians &amp; contacts</a> page.
            </p>
          ) : (
            <form className="stack" onSubmit={add} noValidate>
              <label>
                Contact
                <select value={form.guardian_contact_id} onChange={update("guardian_contact_id")} disabled={busy}>
                  <option value="">Choose a contact</option>
                  {available.map((contact) => (
                    <option key={contact.guardian_contact_id} value={contact.guardian_contact_id}>
                      {contact.display_name}
                    </option>
                  ))}
                </select>
              </label>
              {errors.guardian_contact_id ? <p className="field-error">{errors.guardian_contact_id}</p> : null}
              <label>
                Relationship (your words, e.g. Mother, Grandparent, Family friend)
                <input value={form.relationship_label} maxLength={64} onChange={update("relationship_label")} disabled={busy} />
              </label>
              {errors.relationship_label ? <p className="field-error">{errors.relationship_label}</p> : null}
              <fieldset>
                <legend>Authorized for pickup?</legend>
                <label>
                  <input type="radio" name="pickup" value="yes" checked={form.pickup === "yes"} onChange={update("pickup")} disabled={busy} />{" "}
                  Yes, may collect this child
                </label>
                <label>
                  <input type="radio" name="pickup" value="no" checked={form.pickup === "no"} onChange={update("pickup")} disabled={busy} />{" "}
                  No (contact only)
                </label>
              </fieldset>
              {errors.pickup ? <p className="field-error">{errors.pickup}</p> : null}
              <label>
                Authorized from (optional, {zone}; defaults to now)
                <input type="datetime-local" value={form.effective_from} onChange={update("effective_from")} disabled={busy} />
              </label>
              {errors.effective_from ? <p className="field-error">{errors.effective_from}</p> : null}
              <label>
                Authorized until (optional, for a temporary authorization)
                <input type="datetime-local" value={form.effective_until} onChange={update("effective_until")} disabled={busy} />
              </label>
              {errors.effective_until ? <p className="field-error">{errors.effective_until}</p> : null}
              <label>
                Note (optional)
                <input value={form.note} maxLength={200} onChange={update("note")} disabled={busy} />
              </label>
              {errors.note ? <p className="field-error">{errors.note}</p> : null}
              <button className="primary" type="submit" disabled={busy}>
                Add association
              </button>
            </form>
          )}
        </details>
      ) : null}
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
