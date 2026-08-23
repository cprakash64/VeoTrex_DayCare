"use client";

import { useState } from "react";

export function RingConnectForm({ nonce, time }: { nonce: string; time: number }) {
  const [state, setState] = useState<"ready" | "working" | "active" | "configuring" | "failed">("ready");
  const [supportId, setSupportId] = useState<string | null>(null);

  async function connect() {
    setState("working");
    try {
      const response = await fetch("/api/integrations/ring/claim", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nonce, time }),
      });
      const body = (await response.json()) as { status?: string; support_id?: string };
      setSupportId(typeof body.support_id === "string" ? body.support_id : null);
      setState(response.ok && body.status === "active" ? "active" : response.ok ? "configuring" : "failed");
    } catch {
      setState("failed");
    }
  }

  if (state === "active") return <p role="status">Ring is securely connected.</p>;
  if (state === "configuring") {
    return <p role="status">The account is linked, but completion needs to be resumed safely.</p>;
  }
  return (
    <div>
      <button className="primary" type="button" disabled={state === "working"} onClick={connect}>
        {state === "working" ? "Connecting…" : "Connect Ring account"}
      </button>
      {state === "failed" ? (
        <p role="alert">
          We could not complete the secure connection. Try again or contact support
          {supportId ? ` with support ID ${supportId}` : ""}.
        </p>
      ) : null}
    </div>
  );
}
