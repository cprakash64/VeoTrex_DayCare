import { redirect } from "next/navigation";

import { auth0 } from "../../lib/auth0";
import { fetchMonitoringStatus } from "../../lib/demo-runtime";
import { protectedRouteRedirect } from "../../lib/session-policy";
import { SafetyOperations } from "./monitoring-view";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  // Rendered server-side first so the page is never blank, then kept current by polling.
  const initial = await fetchMonitoringStatus();
  return <SafetyOperations initial={initial} />;
}
