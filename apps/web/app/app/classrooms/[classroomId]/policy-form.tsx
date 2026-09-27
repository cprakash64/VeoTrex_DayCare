"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  apiErrorMessage,
  type PolicyFormInput,
  type RatioPolicy,
  validatePolicyForm,
} from "../../../../lib/classrooms";

const FIELDS: ReadonlyArray<{ key: keyof PolicyFormInput; label: string; type: string; hint?: string }> = [
  { key: "label", label: "Policy label", type: "text" },
  { key: "age_band_label", label: "Age band label (optional)", type: "text" },
  { key: "max_children_per_staff", label: "Maximum children per qualified staff member", type: "number" },
  { key: "minimum_staff", label: "Minimum qualified staff when children are present", type: "number" },
  { key: "maximum_group_size", label: "Maximum group size (optional)", type: "number" },
  { key: "effective_from_date", label: "First day in effect", type: "date" },
  { key: "effective_through_date", label: "Last day in effect (optional)", type: "date" },
  {
    key: "source_reference",
    label: "Source or reference (optional)",
    type: "text",
    hint: "Where these numbers came from, in your own words.",
  },
];

function initial(policy: RatioPolicy | null, ageBand: string | null | undefined): PolicyFormInput {
  return {
    label: policy?.label ?? "Configured classroom policy",
    age_band_label: policy?.age_band_label ?? ageBand ?? "",
    max_children_per_staff: policy ? String(policy.max_children_per_staff) : "",
    minimum_staff: policy ? String(policy.minimum_staff) : "1",
    maximum_group_size: policy?.maximum_group_size != null ? String(policy.maximum_group_size) : "",
    effective_from_date: policy?.effective_from_date ?? "",
    effective_through_date: policy?.effective_through_date ?? "",
    source_reference: policy?.source_reference ?? "",
  };
}

export function PolicyForm({
  classroomId,
  policy,
  defaultAgeBand,
}: {
  classroomId: string;
  policy: RatioPolicy | null;
  defaultAgeBand?: string | null;
}) {
  const router = useRouter();
  const [values, setValues] = useState<PolicyFormInput>(() => initial(policy, defaultAgeBand));
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const base = `/api/classrooms/${classroomId}/ratio-policies`;

  async function send(url: string, method: string, body?: unknown) {
    setWorking(true);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method,
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
    const checked = validatePolicyForm(values);
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setErrors({});
    if (policy) await send(`${base}/${policy.policy_id}`, "PATCH", checked.value);
    else await send(base, "POST", checked.value);
  }

  const prefix = policy ? `policy-${policy.policy_id}` : "policy-new";
  return (
    <form className="stack" onSubmit={submit}>
      {FIELDS.map((field) => (
        <div key={field.key}>
          <label htmlFor={`${prefix}-${field.key}`}>{field.label}</label>
          <input
            id={`${prefix}-${field.key}`}
            type={field.type}
            min={field.type === "number" ? 0 : undefined}
            step={field.type === "number" ? 1 : undefined}
            value={values[field.key]}
            onChange={(event) => setValues({ ...values, [field.key]: event.target.value })}
            disabled={working}
          />
          {field.hint ? <p className="staff-meta">{field.hint}</p> : null}
          {errors[field.key] ? <p className="field-error">{errors[field.key]}</p> : null}
        </div>
      ))}
      <p className="consent">
        This is a configured classroom policy entered by your organization. It is not a statement that
        these numbers meet any legal requirement.
      </p>
      <button className="primary" type="submit" disabled={working}>
        {policy ? "Save changes" : "Add policy"}
      </button>
      {policy ? (
        <button
          className="secondary danger"
          type="button"
          disabled={working}
          onClick={() => send(`${base}/${policy.policy_id}/deactivate`, "POST")}
        >
          Deactivate policy
        </button>
      ) : null}
      {message ? <p role="alert">{message}</p> : null}
    </form>
  );
}
