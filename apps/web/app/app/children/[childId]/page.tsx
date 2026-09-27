import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { auth0 } from "../../../../lib/auth0";
import { childStatusLabel } from "../../../../lib/children";
import { formatInFacility, verificationLabel } from "../../../../lib/guardians";
import { UUID_PATTERN } from "../../../../lib/ring-inventory";
import { protectedRouteRedirect } from "../../../../lib/session-policy";
import { getChild, getChildGuardians, getFacilityGuardians, getReleaseHistory } from "../../../../lib/veotrex-api";
import { AuthorizedPickupPanel } from "./authorized-pickup-panel";

type Props = { params: Promise<{ childId: string }> };

// One child's authorized pickup people and release history (V1-04E). Names, the operator's
// relationship labels and dates only - no photographs, identity documents or camera images.
export default async function ChildPage({ params }: Props) {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const { childId } = await params;
  if (!UUID_PATTERN.test(childId)) notFound();
  const [child, links, history] = await Promise.all([
    getChild(childId),
    getChildGuardians(childId),
    getReleaseHistory(childId),
  ]);
  if (child === null || links === null) notFound();
  const contacts = await getFacilityGuardians(child.facility_id);
  const zone = links.facility_timezone;

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Children</p>
          <h1>{child.display_name}</h1>
        </div>
        <Link href="/app/children">Back</Link>
      </header>
      <p>
        {childStatusLabel(child.status)} · times are in {zone}
      </p>

      <AuthorizedPickupPanel links={links} contacts={contacts?.guardians ?? []} />

      <section aria-labelledby="release-history-heading">
        <h2 id="release-history-heading">Release history</h2>
        {history === null || history.releases.length === 0 ? (
          <p>No releases recorded yet.</p>
        ) : (
          <ul aria-label="Release history">
            {history.releases.map((item) => (
              <li key={item.release_id}>
                {formatInFacility(item.released_at, zone)} · released to {item.guardian_display_name} from{" "}
                {item.classroom_name} · {verificationLabel(item.verification_method)}
                {item.recorded_by_caller ? " · by you" : ""}
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
