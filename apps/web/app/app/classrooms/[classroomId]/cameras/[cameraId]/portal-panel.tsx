"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  type CameraPortal,
  type CameraPortals,
  EMPTY_PORTAL_FORM,
  INSIDE_SIDES,
  MAX_DEADBAND,
  type PortalForm,
  portalErrorMessage,
  sideLabel,
  validatePortalForm,
} from "../../../../../../lib/portals";

function formOf(portal: CameraPortal): PortalForm {
  return {
    label: portal.label,
    x1: String(portal.x1),
    y1: String(portal.y1),
    x2: String(portal.x2),
    y2: String(portal.y2),
    inside_side: portal.inside_side,
    deadband: String(portal.deadband),
    enabled: portal.enabled,
  };
}

function PortalEditor({
  initial,
  submitLabel,
  busy,
  onSubmit,
}: {
  initial: PortalForm;
  submitLabel: string;
  busy: boolean;
  onSubmit: (value: unknown) => Promise<boolean>;
}) {
  const [form, setForm] = useState<PortalForm>(initial);
  const [errors, setErrors] = useState<Readonly<Record<string, string>>>({});
  const set = (key: keyof PortalForm) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [key]: event.target.value });

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const checked = validatePortalForm(form);
    if (!checked.ok) {
      setErrors(checked.errors);
      return;
    }
    setErrors({});
    if ((await onSubmit(checked.value)) && initial === EMPTY_PORTAL_FORM) setForm(EMPTY_PORTAL_FORM);
  }

  return (
    <form className="stack" onSubmit={submit} noValidate>
      <label>
        Doorway name
        <input value={form.label} maxLength={40} onChange={set("label")} disabled={busy} />
      </label>
      {errors.label ? <p className="field-error">{errors.label}</p> : null}
      <p className="staff-meta">
        Endpoints as fractions of the picture: 0 is the left or top edge, 1 the right or bottom edge.
      </p>
      {(["x1", "y1", "x2", "y2"] as const).map((key) => (
        <label key={key}>
          {key}
          <input inputMode="decimal" value={form[key]} onChange={set(key)} disabled={busy} />
          {errors[key] ? <span className="field-error"> {errors[key]}</span> : null}
        </label>
      ))}
      <label>
        Which side is the room?
        <select value={form.inside_side} onChange={set("inside_side")} disabled={busy}>
          <option value="">Choose a side</option>
          {INSIDE_SIDES.map((side) => (
            <option key={side} value={side}>
              {sideLabel(side)}
            </option>
          ))}
        </select>
      </label>
      {errors.inside_side ? <p className="field-error">{errors.inside_side}</p> : null}
      <label>
        Dead-band (0-{MAX_DEADBAND}): positions this close to the line count as neither side
        <input inputMode="decimal" value={form.deadband} onChange={set("deadband")} disabled={busy} />
      </label>
      {errors.deadband ? <p className="field-error">{errors.deadband}</p> : null}
      <label>
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
          disabled={busy}
        />{" "}
        Enabled
      </label>
      {errors.geometry ? <p className="field-error">{errors.geometry}</p> : null}
      <button className="primary" type="submit" disabled={busy}>
        {submitLabel}
      </button>
    </form>
  );
}

/** Doorway lines for one camera: numeric configuration, enable/disable, archive (V1-05A). */
export function PortalPanel({ portals }: { portals: CameraPortals }) {
  const router = useRouter();
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const base = `/api/classrooms/${portals.classroom_id}/cameras/${portals.camera_id}/portals`;
  const active = portals.portals.filter((item) => item.status === "ACTIVE");
  const canEdit = portals.can_configure;

  async function send(url: string, method: "POST" | "PATCH", body?: unknown): Promise<boolean> {
    setWorking(true);
    setMessage(null);
    try {
      const response = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const result = (await response.json()) as { category?: string | null };
      if (!response.ok) {
        setMessage(portalErrorMessage(result.category));
        return false;
      }
      router.refresh();
      return true;
    } catch {
      setMessage(portalErrorMessage(null));
      return false;
    } finally {
      setWorking(false);
    }
  }

  return (
    <section aria-labelledby="portals-heading">
      <h2 id="portals-heading">Doorway lines</h2>
      {portals.edge_distribution !== "CONNECTED" ? (
        <p role="note">
          Not yet sent to cameras: saving a doorway line here does not change what a camera reports.
          For a local evaluation on the edge device, copy the flag shown with each line into{" "}
          <code>veotrex-edge live-demo</code>.
        </p>
      ) : null}
      {portals.portals.length === 0 ? <p>No doorway line is configured for this camera.</p> : null}
      <ul className="staff-list" aria-label="Doorway lines">
        {portals.portals.map((portal) => (
          <li className="staff-row" key={portal.portal_id}>
            <div>
              <h3>{portal.label}</h3>
              <p className="staff-meta">
                ({portal.x1}, {portal.y1}) to ({portal.x2}, {portal.y2}) · {sideLabel(portal.inside_side)} ·
                dead-band {portal.deadband} · revision {portal.revision}
              </p>
              {portal.status === "ACTIVE" ? (
                <p className="staff-meta">
                  Local evaluation flag: <code>--portal {portal.edge_flag}</code>
                </p>
              ) : null}
              {canEdit && portal.status === "ACTIVE" ? (
                <>
                  <p>
                    <button
                      className="secondary"
                      type="button"
                      disabled={working}
                      onClick={() => send(`${base}/${portal.portal_id}`, "PATCH", { enabled: !portal.enabled })}
                    >
                      {portal.enabled ? "Disable" : "Enable"}
                    </button>{" "}
                    <button
                      className="secondary danger"
                      type="button"
                      disabled={working}
                      onClick={() => send(`${base}/${portal.portal_id}/archive`, "POST")}
                    >
                      Archive
                    </button>
                  </p>
                  <details>
                    <summary>Edit</summary>
                    <PortalEditor
                      initial={formOf(portal)}
                      submitLabel="Save"
                      busy={working}
                      onSubmit={(value) => send(`${base}/${portal.portal_id}`, "PATCH", value)}
                    />
                  </details>
                </>
              ) : null}
            </div>
            <span className={`badge ${portal.status === "ACTIVE" && portal.enabled ? "ready" : "inactive"}`}>
              {portal.status === "ARCHIVED" ? "Archived" : portal.enabled ? "Enabled" : "Disabled"}
            </span>
          </li>
        ))}
      </ul>
      {canEdit && active.length < portals.max_portals ? (
        <details>
          <summary>Add a doorway line</summary>
          <PortalEditor
            initial={EMPTY_PORTAL_FORM}
            submitLabel="Add doorway line"
            busy={working}
            onSubmit={(value) => send(base, "POST", value)}
          />
        </details>
      ) : null}
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
