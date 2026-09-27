import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { protectedRouteRedirect } from "../../../lib/session-policy";
import { getFacilities, getFacilityChildren } from "../../../lib/veotrex-api";
import { ChildRosterPanel } from "./child-roster-panel";

// Facility child rosters (V1-04D). Names and optional identifiers only: no photographs, no
// biometrics and no dates of birth. Authorized pickup people live on each child's page (V1-04E).
// Shown to authenticated operators only.
export default async function ChildrenPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const facilities = await getFacilities();
  const rosters = await Promise.all(facilities.map((facility) => getFacilityChildren(facility.facility_id)));

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Children</p>
          <h1>Child rosters</h1>
        </div>
        <a href="/app">Back</a>
      </header>
      <p>
        A roster entry lets staff check a child into a classroom so attendance can supply the child count.
        It holds a name your staff recognise and, optionally, an identifier from your own attendance
        system. Open a child to manage who is authorized to collect them. VeoTrex stores no photographs,
        faces or dates of birth, and cameras never identify children or the adults who collect them.
      </p>
      {facilities.length === 0 ? <p>No facility is available to you.</p> : null}
      {facilities.map((facility, index) => {
        const roster = rosters[index];
        return roster === null ? null : (
          <ChildRosterPanel
            key={facility.facility_id}
            roster={roster}
            canAdd={roster.can_administer && facility.status === "ACTIVE"}
          />
        );
      })}
    </main>
  );
}
