import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { auth0 } from "../../../../lib/auth0";
import { UUID_PATTERN } from "../../../../lib/ring-inventory";
import { protectedRouteRedirect } from "../../../../lib/session-policy";
import { staffPresentation } from "../../../../lib/staff";
import { getApplicationIdentity, getStaffImages, getStaffMember } from "../../../../lib/veotrex-api";
import { TeacherControls } from "./teacher-controls";

type Props = { params: Promise<{ staffId: string }> };

export default async function TeacherPage({ params }: Props) {
  const { staffId } = await params;
  if (!UUID_PATTERN.test(staffId)) notFound();
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const [identity, member] = await Promise.all([getApplicationIdentity(), getStaffMember(staffId)]);
  if (!member) notFound();
  const images = await getStaffImages(staffId);
  const canManage = identity.permissions.includes("manage:staff");
  const view = staffPresentation(member);

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
    </main>
  );
}
