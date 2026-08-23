import { redirect } from "next/navigation";

import { auth0 } from "../../../../lib/auth0";
import { validRingLinkParameters } from "../../../../lib/ring-link";
import { getRingLinkContext } from "../../../../lib/veotrex-api";
import { RingConnectForm } from "./ring-connect-form";

type Props = { searchParams: Promise<{ nonce?: string; time?: string }> };

export default async function RingAccountLink({ searchParams }: Props) {
  const values = await searchParams;
  const nonce = values.nonce;
  const time = values.time;
  if (!validRingLinkParameters(nonce, time)) {
    return <LinkUnavailable />;
  }
  const session = await auth0.getSession();
  if (!session?.user) {
    const returnTo = `/integrations/ring/link?${new URLSearchParams({ nonce: nonce!, time: time! })}`;
    redirect(`/auth/login?returnTo=${encodeURIComponent(returnTo)}`);
  }
  let context;
  try {
    context = await getRingLinkContext(nonce!, time!);
  } catch {
    return <LinkUnavailable />;
  }
  if (!context.eligible) return <LinkUnavailable />;

  return (
    <main>
      <p className="eyebrow">VeoTrex · Ring</p>
      <h1>Connect Ring securely</h1>
      <p>Connect this Ring account to VeoTrex organization {context.tenant_name}.</p>
      <p>No Ring account identifier or credential is shown in your browser.</p>
      <RingConnectForm nonce={nonce!} time={Number(time)} />
    </main>
  );
}

function LinkUnavailable() {
  return (
    <main>
      <p className="eyebrow">VeoTrex · Ring</p>
      <h1>This link is unavailable</h1>
      <p>The secure Ring link is invalid, expired, already used, or you do not have permission.</p>
    </main>
  );
}
