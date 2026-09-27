import Link from "next/link";
import { redirect } from "next/navigation";

import { auth0 } from "../../../lib/auth0";
import { cameraSummary } from "../../../lib/classrooms";
import { protectedRouteRedirect } from "../../../lib/session-policy";
import { getClassrooms, getFacilities } from "../../../lib/veotrex-api";
import { CreateClassroomForm } from "./create-classroom-form";

export default async function ClassroomsPage() {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const [facilities, classrooms] = await Promise.all([getFacilities(), getClassrooms()]);
  const manageable = facilities.filter((facility) => facility.can_administer && facility.status === "ACTIVE");

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Classrooms</p>
          <h1>Classrooms &amp; configured policies</h1>
        </div>
        <a href="/app">Back</a>
      </header>
      <p>
        Each classroom can carry a configured staff-to-child policy entered by your organization. It is
        a configured classroom policy, not a legal determination. Ratios are calculated only from
        approved presence sources; camera occupancy is never used to count children or staff.
      </p>
      {facilities.length === 0 ? (
        <section>
          <h2>No facility configured</h2>
          <p>A facility must be set up for this organization before classrooms can be added.</p>
        </section>
      ) : null}
      {manageable.length > 0 ? <CreateClassroomForm facilities={manageable} /> : null}
      {classrooms.length === 0 ? (
        <section>
          <h2>No classrooms yet</h2>
          <p>{manageable.length > 0 ? "Add a classroom above." : "A facility administrator can add classrooms."}</p>
        </section>
      ) : (
        <ul className="staff-list" aria-label="Classrooms">
          {classrooms.map((room) => {
            const current = room.policies.find((policy) => policy.policy_id === room.current_policy_id);
            return (
              <li className="staff-row" key={room.classroom_id}>
                <div>
                  <h2>
                    <Link href={`/app/classrooms/${room.classroom_id}`}>{room.name}</Link>
                  </h2>
                  <p className="staff-meta">
                    {room.facility_name}
                    {room.age_band_label ? ` · ${room.age_band_label}` : ""} · {cameraSummary(room)}
                  </p>
                  <p className="staff-meta">
                    {current
                      ? `Configured classroom policy: up to ${current.max_children_per_staff} children per qualified staff member`
                      : "No configured classroom policy in effect"}
                  </p>
                </div>
                <span className={`badge ${room.status === "ACTIVE" ? "" : "inactive"}`}>
                  {room.status === "ACTIVE" ? "Active" : "Inactive"}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}
