"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  apiErrorMessage,
  type ClassroomPresence,
  DEFAULT_VALIDITY_SECONDS,
  MANUAL_LIMITS,
  type ManualPresenceForm,
  presenceStateLabel,
  VALIDITY_CHOICES,
  validateManualPresence,
} from "../../../../lib/classrooms";

const COUNT_FIELDS: ReadonlyArray<{ key: keyof ManualPresenceForm; label: string; max: number }> = [
  { key: "child_count", label: "Children present", max: MANUAL_LIMITS.children },
  { key: "qualified_staff_count", label: "Qualified staff present", max: MANUAL_LIMITS.qualified_staff },
  { key: "visitor_count", label: "Visitors present", max: MANUAL_LIMITS.visitors },
];

export function ManualPresenceCard({
  classroomId,
  presence,
  canReport,
  rosterMode = false,
}: {
  classroomId: string;
  presence: ClassroomPresence | null;
  canReport: boolean;
  // V1-04C: staff are counted from check-ins, so this form reports children and visitors only.
  rosterMode?: boolean;
}) {
  const router = useRouter();
  const current = presence?.current ?? null;
  const [values, setValues] = useState<ManualPresenceForm>({
    child_count: current && !current.revoked_at ? String(current.child_count) : "",
    qualified_staff_count:
      current && !current.revoked_at && current.qualified_staff_count !== null
        ? String(current.qualified_staff_count)
        : "",
    visitor_count: current && !current.revoked_at ? String(current.visitor_count) : "0",
    valid_for_seconds: String(DEFAULT_VALIDITY_SECONDS),
  });
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(tick);
  }, []);
  const base = `/api/classrooms/${classroomId}/presence`;

  async function send(url: string, body?: unknown) {
    setWorking(true);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
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

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const checked = validateManualPresence(values, rosterMode);
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setErrors({});
    await send(base, checked.value);
  }

  const state = current ? presenceStateLabel(current, now) : null;
  const remaining = current && state === "Fresh" ? Math.max(0, Math.round((Date.parse(current.valid_until) - now) / 1000)) : null;
  return (
    <section aria-labelledby="presence-heading">
      <h2 id="presence-heading">Manual presence</h2>
      <p className="staff-meta">
        Operator-reported head counts for this classroom. Numbers only &mdash; never enter names. A report
        expires automatically and is never inferred from cameras.
      </p>
      {current ? (
        <ul aria-label="Current manual presence report">
          <li>Source: Manual (operator-reported){current.submitted_by_caller ? " · reported by you" : ""}</li>
          <li>
            Children {current.child_count} ·{" "}
            {rosterMode || current.qualified_staff_count === null
              ? "qualified staff from staff check-ins"
              : `qualified staff ${current.qualified_staff_count}`}{" "}
            · visitors {current.visitor_count}
          </li>
          <li>Reported at {new Date(current.observed_at).toLocaleTimeString()}</li>
          <li>Valid until {new Date(current.valid_until).toLocaleTimeString()}</li>
          <li>
            <span className={`badge ${state === "Fresh" ? "ready" : "inactive"}`}>{state}</span>
            {remaining !== null ? ` · expires in ${remaining} s` : ""}
          </li>
        </ul>
      ) : (
        <p>No presence has been reported for this classroom.</p>
      )}
      {canReport ? (
        <form className="stack" onSubmit={submit} noValidate>
          {rosterMode ? (
            <p className="staff-meta">Qualified staff are counted from staff check-ins in this classroom.</p>
          ) : null}
          {COUNT_FIELDS.filter((field) => !rosterMode || field.key !== "qualified_staff_count").map((field) => (
            <div key={field.key}>
              <label htmlFor={`presence-${field.key}`}>{field.label}</label>
              <input
                id={`presence-${field.key}`}
                type="number"
                inputMode="numeric"
                min={0}
                max={field.max}
                step={1}
                value={values[field.key]}
                onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
                disabled={working}
                aria-invalid={errors[field.key] ? true : undefined}
              />
              {errors[field.key] ? <p className="field-error">{errors[field.key]}</p> : null}
            </div>
          ))}
          <label htmlFor="presence-validity">Valid for</label>
          <select
            id="presence-validity"
            value={values.valid_for_seconds}
            onChange={(event) => setValues({ ...values, valid_for_seconds: event.target.value })}
            disabled={working}
          >
            {VALIDITY_CHOICES.map((choice) => (
              <option key={choice.seconds} value={String(choice.seconds)}>
                {choice.label}
              </option>
            ))}
          </select>
          {errors.valid_for_seconds ? <p className="field-error">{errors.valid_for_seconds}</p> : null}
          <button className="primary" type="submit" disabled={working}>
            {working ? "Reporting…" : "Update counts"}
          </button>
          {current && !current.revoked_at ? (
            <button
              className="secondary danger"
              type="button"
              disabled={working}
              onClick={() => send(`${base}/${current.snapshot_id}/revoke`)}
            >
              Revoke current report
            </button>
          ) : null}
          {message ? <p role="alert">{message}</p> : null}
        </form>
      ) : null}
    </section>
  );
}
