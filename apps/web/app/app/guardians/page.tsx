import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { protectedRouteRedirect } from "../../../lib/session-policy";
import { getFacilities, getFacilityGuardians } from "../../../lib/veotrex-api";
import { GuardianRosterPanel } from "./guardian-roster-panel";

// Facility guardians & contacts (V1-04E). Names and optional identifiers only: no photographs,
// no identity documents, no contact details and nothing from a camera. Operators only.
export default async function GuardiansPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const facilities = await getFacilities();
  const rosters = await Promise.all(facilities.map((facility) => getFacilityGuardians(facility.facility_id)));

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Guardians &amp; contacts</p>
          <h1>Guardians &amp; contacts</h1>
        </div>
        <a href="/app">Back</a>
      </header>
      <p>
        A contact is an adult your staff may hand a child to - a parent, guardian, relative, babysitter or
        family friend. Listing someone here does not let them collect a child: on each child&apos;s page you
        choose who is authorized for pickup and for how long. VeoTrex stores a name and, optionally, an
        identifier from your own records - no photographs, identity documents or camera images - and it
        never identifies anyone from a camera.
      </p>
      {facilities.length === 0 ? <p>No facility is available to you.</p> : null}
      {facilities.map((facility, index) => {
        const roster = rosters[index];
        return roster === null ? null : (
          <GuardianRosterPanel
            key={facility.facility_id}
            roster={roster}
            canAdd={roster.can_administer && facility.status === "ACTIVE"}
          />
        );
      })}
    </main>
  );
}
