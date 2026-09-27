"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiErrorMessage } from "../../../../lib/classrooms";
import { PRESENCE_MODE_CHOICES, presenceModeLabel } from "../../../../lib/staff-presence";

/**
 * Where this classroom's ratio inputs come from (V1-04C/V1-04D). Changing it is an explicit,
 * audited operator action; nothing switches on its own, and no count is copied between sources.
 */
export function PresenceSourceControl({
  classroomId,
  mode,
  canEdit,
}: {
  classroomId: string;
  mode: string;
  canEdit: boolean;
}) {
  const router = useRouter();
  const [choice, setChoice] = useState(mode);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function apply() {
    setWorking(true);
    setMessage(null);
    try {
      const response = await fetch(`/api/classrooms/${classroomId}/presence-source-mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: choice }),
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

  return (
    <section aria-labelledby="source-heading">
      <h2 id="source-heading">Presence sources</h2>
      <p>{presenceModeLabel(mode)}</p>
      {canEdit ? (
        <p>
          <label htmlFor="presence-source-mode">Take counts from</label>{" "}
          <select
            id="presence-source-mode"
            value={choice}
            onChange={(event) => setChoice(event.target.value)}
            disabled={working}
          >
            {PRESENCE_MODE_CHOICES.map((item) => (
              <option key={item.mode} value={item.mode}>
                {item.label}
              </option>
            ))}
          </select>{" "}
          <button className="secondary" type="button" disabled={working || choice === mode} onClick={apply}>
            Use these sources
          </button>
        </p>
      ) : null}
      <p className="staff-meta">
        Switching never copies a number from one source to another. Cameras are never a source.
      </p>
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
