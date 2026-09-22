import Link from "next/link";
import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { protectedRouteRedirect } from "../../../lib/session-policy";
import { staffPresentation } from "../../../lib/staff";
import { getApplicationIdentity, getStaff } from "../../../lib/veotrex-api";
import { CreateTeacherForm } from "./create-teacher-form";

export default async function StaffPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const [identity, staff] = await Promise.all([getApplicationIdentity(), getStaff()]);
  const canManage = identity.permissions.includes("manage:staff");

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Staff</p>
          <h1>Teachers &amp; staff</h1>
        </div>
        <a href="/app">Back</a>
      </header>
      <p>
        Enroll consenting adult teachers so VeoTrex can recognize them in recorded video. Anyone who
        is not enrolled, including every child, is treated as unknown. Enrollment never happens from
        camera footage; it requires an authorized operator to add photos here.
      </p>
      {canManage ? <CreateTeacherForm /> : null}
      {staff.length === 0 ? (
        <section>
          <h2>No teachers enrolled</h2>
          <p>{canManage ? "Add a teacher above to begin enrollment." : "An owner can enroll teachers."}</p>
        </section>
      ) : (
        <ul className="staff-list" aria-label="Enrolled teachers">
          {staff.map((member) => {
            const view = staffPresentation(member);
            return (
              <li className="staff-row" key={member.staff_id}>
                <div>
                  <h2>
                    <Link href={`/app/staff/${member.staff_id}`}>{member.display_name}</Link>
                  </h2>
                  <p className="staff-meta">
                    {view.progressLabel} · {view.readinessLabel} · updated{" "}
                    {new Date(member.updated_at).toLocaleString()}
                  </p>
                </div>
                <span className={`badge ${member.recognition_ready ? "ready" : member.status === "INACTIVE" ? "inactive" : ""}`}>
                  {member.recognition_ready ? "Ready" : view.statusLabel}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}
