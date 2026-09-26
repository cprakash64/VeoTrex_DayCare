import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { auth0 } from "../../../../../../lib/auth0";
import { UUID_PATTERN } from "../../../../../../lib/ring-inventory";
import { protectedRouteRedirect } from "../../../../../../lib/session-policy";
import { getCameraPortals } from "../../../../../../lib/veotrex-api";
import { PortalPanel } from "./portal-panel";

type Props = { params: Promise<{ classroomId: string; cameraId: string }> };

// Doorway lines for one classroom camera (V1-05A). Geometry only - no picture is stored or shown
// here, and nothing identifies anyone.
export default async function CameraPortalsPage({ params }: Props) {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const { classroomId, cameraId } = await params;
  if (!UUID_PATTERN.test(classroomId) || !UUID_PATTERN.test(cameraId)) notFound();
  const portals = await getCameraPortals(classroomId, cameraId);
  if (portals === null) notFound();

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Classrooms · {portals.classroom_name}</p>
          <h1>{portals.camera_name} · doorway lines</h1>
        </div>
        <Link href={`/app/classrooms/${classroomId}`}>Back</Link>
      </header>
      <p>
        A doorway line tells the camera where the room&apos;s door is. A person is counted as entering
        or leaving the room only when their track crosses the line - appearing in or disappearing
        from the picture is never counted as an entry or an exit. Everyone stays an anonymous track;
        nobody is identified.
      </p>
      <PortalPanel portals={portals} />
    </main>
  );
}
