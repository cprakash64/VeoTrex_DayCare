import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { auth0 } from "../../../../lib/auth0";
import { UUID_PATTERN } from "../../../../lib/ring-inventory";
import { protectedRouteRedirect } from "../../../../lib/session-policy";
import { staffPresentation } from "../../../../lib/staff";
import {
  getApplicationIdentity,
  getFacilities,
  getFacilityRoster,
  getStaffImages,
  getStaffMember,
} from "../../../../lib/veotrex-api";
import { type FacilityEligibility, StaffEligibilityPanel } from "./staff-eligibility-panel";
import { TeacherControls } from "./teacher-controls";

type Props = { params: Promise<{ staffId: string }> };

export default async function TeacherPage({ params }: Props) {
  const { staffId } = await params;
  if (!UUID_PATTERN.test(staffId)) notFound();
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const [identity, member] = await Promise.all([getApplicationIdentity(), getStaffMember(staffId)]);
  if (!member) notFound();
  const [images, facilities] = await Promise.all([getStaffImages(staffId), getFacilities()]);
  const canManage = identity.permissions.includes("manage:staff");
  const view = staffPresentation(member);
  // V1-04C: this person's designation at each facility the viewer can read.
  const rosters = await Promise.all(facilities.map((facility) => getFacilityRoster(facility.facility_id, staffId)));
  const eligibility: FacilityEligibility[] = facilities.map((facility, index) => {
    const assignments = rosters[index]?.assignments ?? [];
    return {
      facility_id: facility.facility_id,
      facility_name: facility.name,
      can_administer: facility.can_administer && facility.status === "ACTIVE",
      current: assignments.find((item) => item.status === "ACTIVE") ?? null,
      history: assignments.filter((item) => item.status !== "ACTIVE").length,
    };
  });

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Staff</p>
          <h1>{member.display_name}</h1>
        </div>
        <Link href="/app/staff">All teachers</Link>
      </header>
      <p>
        <span className={`badge ${member.recognition_ready ? "ready" : member.status === "INACTIVE" ? "inactive" : ""}`}>
          {view.statusLabel}
        </span>{" "}
        {view.progressLabel} · {view.readinessLabel}
      </p>
      <p className="staff-meta">
        Recognition requires at least {member.required_images} accepted photos, each processed
        successfully, and an active teacher. Up to {member.maximum_images} photos are kept.
      </p>
      <TeacherControls member={member} images={images} canManage={canManage} view={view} />
      <StaffEligibilityPanel staffId={member.staff_id} staffActive={member.status === "ACTIVE"} facilities={eligibility} />
    </main>
  );
}
