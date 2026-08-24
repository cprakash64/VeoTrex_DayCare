"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export function SyncButton({ connectionId }: { connectionId: string }) {
  const router = useRouter();
  const [state, setState] = useState<"ready" | "working" | "failed">("ready");

  async function synchronize() {
    setState("working");
    try {
      const response = await fetch("/api/integrations/ring/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ connection_id: connectionId }),
      });
      if (!response.ok) throw new Error("sync failed");
      setState("ready");
      router.refresh();
    } catch {
      setState("failed");
    }
  }

  return (
    <div>
      <button className="primary" type="button" disabled={state === "working"} onClick={synchronize}>
        {state === "working" ? "Synchronizing…" : "Sync Ring devices"}
      </button>
      {state === "failed" ? <p role="alert">Ring synchronization is temporarily unavailable.</p> : null}
    </div>
  );
}
