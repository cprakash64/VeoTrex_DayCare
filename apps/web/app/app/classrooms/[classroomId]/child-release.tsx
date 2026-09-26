"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  type ChildReleaseOptions,
  EMPTY_RELEASE_SELECTION,
  guardianErrorMessage,
  pickupStatusLabel,
  releaseReadiness,
  type ReleaseSelection,
  VERIFICATION_METHODS,
} from "../../../../lib/guardians";

/**
 * Release one child at pickup (V1-04E). The operator explicitly chooses one of the adults who
 * are authorized for pickup right now, says how they confirmed that adult, and confirms. Nothing
 * is pre-selected and nothing submits on its own. Adults who are associated but not currently
 * authorized are shown for context only and cannot be chosen. VeoTrex does not identify anyone:
 * the confirmation is the operator's.
 */
export function ChildRelease({
  classroomId,
  childName,
  options,
  disabled,
}: {
  classroomId: string;
  childName: string;
  options: ChildReleaseOptions;
  disabled: boolean;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState<ReleaseSelection>(EMPTY_RELEASE_SELECTION);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const readiness = releaseReadiness(options.child_profile_id, options.candidates, selection);
  const chosen = options.candidates.find((item) => item.guardian_contact_id === selection.guardianContactId);
  const name = `release-${options.child_profile_id}`;

  function close() {
    setOpen(false);
    setSelection(EMPTY_RELEASE_SELECTION);
    setMessage(null);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!readiness.ready) return;
    setWorking(true);
    setMessage(null);
    try {
      const response = await fetch(`/api/classrooms/${classroomId}/attendance/release`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(readiness.payload),
      });
      const result = (await response.json()) as { category?: string | null };
      if (!response.ok) setMessage(guardianErrorMessage(result.category));
      else {
        close();
        router.refresh();
      }
    } catch {
      setMessage(guardianErrorMessage(null));
    } finally {
      setWorking(false);
    }
  }

  if (!open) {
    return (
      <button className="primary" type="button" disabled={disabled} onClick={() => setOpen(true)}>
        Release child
      </button>
    );
  }

  return (
    <form className="stack" onSubmit={submit} noValidate aria-label={`Release ${childName}`}>
      <fieldset>
        <legend>1. Who is collecting {childName}?</legend>
        {options.candidates.length === 0 ? (
          <p>
            No one is currently authorized for pickup. Update authorized pickup people on the{" "}
            <a href={`/app/children/${options.child_profile_id}`}>child&apos;s page</a>.
          </p>
        ) : (
          options.candidates.map((item) => (
            <label key={item.guardian_contact_id}>
              <input
                type="radio"
                name={`${name}-person`}
                value={item.guardian_contact_id}
                checked={selection.guardianContactId === item.guardian_contact_id}
                onChange={() => setSelection({ ...selection, guardianContactId: item.guardian_contact_id, confirmed: false })}
                disabled={working}
              />{" "}
              {item.display_name} · {item.relationship_label} · Authorized for pickup
            </label>
          ))
        )}
        {options.unavailable.length > 0 ? (
          <ul aria-label="Not currently authorized">
            {options.unavailable.map((item) => (
              <li key={item.guardian_contact_id} className="staff-meta">
                {item.display_name} · {item.relationship_label} · {pickupStatusLabel(item.reason)}
              </li>
            ))}
          </ul>
        ) : null}
      </fieldset>
      <fieldset>
        <legend>2. How did you confirm this adult?</legend>
        {VERIFICATION_METHODS.map((item) => (
          <label key={item.method}>
            <input
              type="radio"
              name={`${name}-method`}
              value={item.method}
              checked={selection.method === item.method}
              onChange={() => setSelection({ ...selection, method: item.method, confirmed: false })}
              disabled={working}
            />{" "}
            {item.label} — {item.help}
          </label>
        ))}
      </fieldset>
      <label>
        <input
          type="checkbox"
          checked={selection.confirmed}
          onChange={(event) => setSelection({ ...selection, confirmed: event.target.checked })}
          disabled={working || !chosen || !selection.method}
        />{" "}
        3. I am releasing {childName} to {chosen ? `${chosen.display_name} (${chosen.relationship_label})` : "the adult chosen above"}.
      </label>
      {message ? <p role="alert">{message}</p> : null}
      <p>
        <button className="primary" type="submit" disabled={working || !readiness.ready}>
          Release {childName}
        </button>{" "}
        <button className="secondary" type="button" onClick={close} disabled={working}>
          Cancel
        </button>
      </p>
    </form>
  );
}
