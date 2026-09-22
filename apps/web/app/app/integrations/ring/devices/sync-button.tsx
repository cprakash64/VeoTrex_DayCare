"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

type SyncState = "ready" | "working" | "synchronized" | "failed";

export function SyncButton({ connectionId }: { connectionId: string }) {
  const router = useRouter();
  const [state, setState] = useState<SyncState>("ready");

  async function synchronize() {
    if (state === "working") return;
    setState("working");
    try {
      const response = await fetch("/api/integrations/ring/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ connection_id: connectionId }),
      });
      if (!response.ok) throw new Error("sync failed");
      setState("synchronized");
      router.refresh();
    } catch {
      setState("failed");
    }
  }

  return (
    <div>
      <button
        className="primary"
        type="button"
        disabled={state === "working"}
        aria-busy={state === "working"}
        onClick={synchronize}
      >
        {state === "working" ? "Synchronizing…" : "Sync Ring devices"}
      </button>
      {state === "synchronized" ? (
        <p role="status">Ring inventory synchronized.</p>
      ) : null}
      {state === "failed" ? (
        <p role="alert">Ring synchronization is temporarily unavailable. Try again later.</p>
      ) : null}
    </div>
  );
}
