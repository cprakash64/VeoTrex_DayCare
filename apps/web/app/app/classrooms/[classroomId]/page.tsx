import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { auth0 } from "../../../../lib/auth0";
import { policyNumbers, policyPeriod, policyStatusLabel } from "../../../../lib/classrooms";
import { UUID_PATTERN } from "../../../../lib/ring-inventory";
import { protectedRouteRedirect } from "../../../../lib/session-policy";
import { ATTENDANCE_MODE, ROSTER_MODE } from "../../../../lib/staff-presence";
import {
  getClassroom,
  getClassroomAttendance,
  getClassroomPresence,
  getClassroomRatioStatus,
  getClassroomStaffPresence,
  getReleaseOptions,
} from "../../../../lib/veotrex-api";
import { ChildAttendanceCard } from "./child-attendance-card";
import { ClassroomControls } from "./classroom-controls";
import { ManualPresenceCard } from "./manual-presence-card";
import { PresenceSourceControl } from "./presence-source-control";
import { RatioStatusCard } from "./ratio-status-card";
import { StaffPresenceCard } from "./staff-presence-card";
import { PolicyForm } from "./policy-form";

type Props = { params: Promise<{ classroomId: string }> };

export default async function ClassroomPage({ params }: Props) {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const { classroomId } = await params;
  if (!UUID_PATTERN.test(classroomId)) notFound();
  const [room, status, presence, staffPresence, attendance, releaseOptions] = await Promise.all([
    getClassroom(classroomId),
    getClassroomRatioStatus(classroomId),
    getClassroomPresence(classroomId),
    getClassroomStaffPresence(classroomId),
    getClassroomAttendance(classroomId),
    getReleaseOptions(classroomId),
  ]);
  if (room === null) notFound();
  const canEdit = room.can_administer;
  const active = room.status === "ACTIVE";

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Classrooms · {room.facility_name}</p>
          <h1>{room.name}</h1>
        </div>
        <Link href="/app/classrooms">Back</Link>
      </header>
      <p>
        {room.age_band_label ? `Age band: ${room.age_band_label} · ` : ""}
        {active ? "Active" : "Inactive"} · dates are in {room.facility_timezone}
      </p>

      <RatioStatusCard status={status} />

      <ManualPresenceCard
        key={presence?.current?.snapshot_id ?? "none"}
        classroomId={room.classroom_id}
        presence={presence}
        canReport={canEdit && active}
        rosterMode={room.presence_source_mode === ROSTER_MODE}
        attendanceMode={room.presence_source_mode === ATTENDANCE_MODE}
      />

      <PresenceSourceControl
        key={room.presence_source_mode}
        classroomId={room.classroom_id}
        mode={room.presence_source_mode}
        canEdit={canEdit}
      />

      <ChildAttendanceCard classroomId={room.classroom_id} attendance={attendance} releaseOptions={releaseOptions} />

      <StaffPresenceCard classroomId={room.classroom_id} presence={staffPresence} />

      <section aria-labelledby="camera-heading">
        <h2 id="camera-heading">Cameras</h2>
        <p>
          <Link href={`/app/classrooms/${room.classroom_id}/room-transitions`}>Room transitions</Link> · anonymous
          entries and exits across doorway lines
        </p>
        {room.cameras.length === 0 ? (
          <p>No camera is associated with this classroom yet.</p>
        ) : (
          <ul>
            {room.cameras.map((camera) => (
              <li key={camera.camera_id}>
                {camera.name} · {camera.zone_name} · {camera.status.toLowerCase()} ·{" "}
                <Link href={`/app/classrooms/${room.classroom_id}/cameras/${camera.camera_id}`}>Doorway lines</Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      {canEdit ? <ClassroomControls classroom={room} /> : null}

      <section aria-labelledby="policy-heading">
        <h2 id="policy-heading">Configured classroom policies</h2>
        <p className="staff-meta">
          Entered by your organization. VeoTrex does not verify these numbers against any regulation.
        </p>
        {room.policies.length === 0 ? <p>No policy has been configured for this classroom.</p> : null}
        <ul className="staff-list" aria-label="Configured classroom policies">
          {room.policies.map((policy) => (
            <li className="staff-row" key={policy.policy_id}>
              <div>
                <h3>{policy.label}</h3>
                <p className="staff-meta">{policyNumbers(policy)}</p>
                <p className="staff-meta">
                  {policyPeriod(policy)}
                  {policy.age_band_label ? ` · ${policy.age_band_label}` : ""} · revision {policy.revision}
                </p>
                {policy.source_reference ? <p className="staff-meta">Source: {policy.source_reference}</p> : null}
                {canEdit && active && policy.status === "ACTIVE" ? (
                  <details>
                    <summary>Edit or deactivate</summary>
                    <PolicyForm classroomId={room.classroom_id} policy={policy} />
                  </details>
                ) : null}
              </div>
              <span className={`badge ${policy.in_effect ? "ready" : "inactive"}`}>{policyStatusLabel(policy)}</span>
            </li>
          ))}
        </ul>
        {canEdit && active ? (
          <details>
            <summary>Add a configured classroom policy</summary>
            <PolicyForm classroomId={room.classroom_id} policy={null} defaultAgeBand={room.age_band_label} />
          </details>
        ) : null}
      </section>
    </main>
  );
}
