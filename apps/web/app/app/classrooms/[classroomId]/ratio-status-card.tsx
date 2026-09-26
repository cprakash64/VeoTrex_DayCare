"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { type RatioStatus, ratioStatusView } from "../../../../lib/classrooms";

// Re-read the server's evaluation this often, and re-check expiry on this clock every second:
// a result that was fresh when the page loaded must not keep showing after its report expires.
const REFRESH_MS = 15_000;

export function RatioStatusCard({ status }: { status: RatioStatus | null }) {
  const router = useRouter();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 1_000);
    const refresh = setInterval(() => router.refresh(), REFRESH_MS);
    return () => {
      clearInterval(tick);
      clearInterval(refresh);
    };
  }, [router]);
  const view = ratioStatusView(status, now);
  return (
    <section className="ratio-card" aria-labelledby="ratio-heading" aria-live="polite">
      <p className="eyebrow">{view.basis}</p>
      <h2 id="ratio-heading">
        <span className={`badge ${view.tone === "attention" ? "attention" : view.tone === "ok" ? "ready" : "inactive"}`}>
          {view.headline}
        </span>
      </h2>
      {view.details.map((line) => (
        <p key={line}>{line}</p>
      ))}
      <p className="staff-meta">
        Counts come only from approved presence sources. People seen by a camera are never counted as
        children or staff.
      </p>
    </section>
  );
}
