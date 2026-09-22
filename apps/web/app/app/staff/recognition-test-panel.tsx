"use client";

import { useState } from "react";

import {
  clientUploadCheck,
  recognitionPresentation,
  rejectionMessage,
  type RecognitionTestResult,
} from "../../../lib/staff";

/**
 * Evaluation-only recognition panel (V1-02B0).
 *
 * Rendered by the staff page only in an evaluation build, and only for an operator who can
 * manage staff. The photo is sent, answered and forgotten: it is never stored by the browser,
 * never added to anyone's enrollment, and the API keeps nothing either. The result deliberately
 * shows the raw similarity and the threshold that produced it, so the operator running the
 * qualification can see how close a decision was rather than trusting a verdict.
 */
export function RecognitionTestPanel() {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RecognitionTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // Cleared immediately so the same photo can be re-submitted, and so the chosen file is not
    // left sitting in the control after the answer comes back.
    event.target.value = "";
    if (!file || busy) return;
    const precheck = clientUploadCheck(file);
    if (precheck) {
      setResult(null);
      setError(precheck);
      return;
    }
    setBusy(true);
    setResult(null);
    setError(null);
    try {
      const form = new FormData();
      form.append("photo", file);
      const response = await fetch("/api/staff/recognition-test", { method: "POST", body: form });
      const body = (await response.json().catch(() => ({}))) as {
        status?: string;
        category?: string;
        result?: RecognitionTestResult;
      };
      if (response.ok && body.result) {
        setResult(body.result);
      } else if (body.status === "rejected") {
        setError(rejectionMessage(body.category));
      } else {
        setError("The photo could not be checked right now. Try again.");
      }
    } catch {
      setError("The photo could not be checked right now. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const view = result ? recognitionPresentation(result) : null;

  return (
    <section aria-labelledby="recognition-test">
      <h2 id="recognition-test">Test recognition</h2>
      <p className="staff-meta">
        <strong>Evaluation mode — not production calibrated.</strong> Upload a photo of one adult
        who has consented, and VeoTrex will say which enrolled teacher it is, or Unknown. The
        photo is not saved, is not added to anyone&apos;s enrollment, and never leaves this
        organization. Never use a photo of a child.
      </p>
      <form className="stack" onSubmit={(event) => event.preventDefault()}>
        <label htmlFor="recognition-photo">Test photo (JPEG or PNG, one person)</label>
        <input
          id="recognition-photo"
          type="file"
          accept="image/jpeg,image/png"
          onChange={submit}
          disabled={busy}
          aria-busy={busy}
        />
      </form>
      {busy ? <p role="status">Checking the photo…</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {result && view ? (
        <div className="recognition-result">
          <p role="status">
            <span className={`badge ${view.matched ? "ready" : "inactive"}`}>{view.headline}</span>
          </p>
          <p className="staff-meta">{view.detail}</p>
          <p className="staff-meta">
            Model {result.model_id} {result.model_version} · evaluation thresholds{" "}
            {result.threshold.toFixed(2)} similarity and {result.margin.toFixed(2)} margin. These
            are conservative evaluation values, not calibrated production settings: treat every
            answer as a measurement to review, not a decision to act on.
          </p>
        </div>
      ) : null}
    </section>
  );
}
