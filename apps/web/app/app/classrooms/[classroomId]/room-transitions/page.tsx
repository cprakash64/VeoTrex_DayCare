import Link from "next/link";
import { notFound, redirect } from "next/navigation";

import { auth0 } from "../../../../../lib/auth0";
import { UUID_PATTERN } from "../../../../../lib/ring-inventory";
import {
  cleanCursor,
  cleanTransitionType,
  transitionSentence,
  type TransitionType,
} from "../../../../../lib/room-transitions";
import { protectedRouteRedirect } from "../../../../../lib/session-policy";
import { getClassroom, getRoomTransitions } from "../../../../../lib/veotrex-api";

type Props = {
  params: Promise<{ classroomId: string }>;
  searchParams: Promise<{ cursor?: string; type?: string }>;
};

function pageHref(classroomId: string, cursor: string | null, type: TransitionType | null): string {
  const query = new URLSearchParams();
  if (cursor !== null) query.set("cursor", cursor);
  if (type !== null) query.set("type", type);
  const suffix = query.toString();
  return `/app/classrooms/${classroomId}/room-transitions${suffix ? `?${suffix}` : ""}`;
}

// Anonymous room entries and exits reported by the edge (V1-05B). One bounded page at a time,
// newest first. Every row is a person crossing a doorway line - nobody is identified.
export default async function RoomTransitionsPage({ params, searchParams }: Props) {
  const destination = protectedRouteRedirect(await auth0.getSession());
  if (destination) redirect(destination);
  const { classroomId } = await params;
  if (!UUID_PATTERN.test(classroomId)) notFound();
  const query = await searchParams;
  const cursor = cleanCursor(query.cursor);
  const type = cleanTransitionType(query.type);
  const [room, result] = await Promise.all([
    getClassroom(classroomId),
    getRoomTransitions(classroomId, cursor, type),
  ]);
  if (room === null || result.status === "not_found") notFound();
  const when = (value: string) =>
    new Date(value).toLocaleString("en-US", { timeZone: room.facility_timezone });

  return (
    <main>
      <header className="toolbar">
        <div>
          <p className="eyebrow">VeoTrex · Classrooms · {room.name}</p>
          <h1>Room transitions</h1>
        </div>
        <Link href={`/app/classrooms/${classroomId}`}>Back</Link>
      </header>
      <p>
        Each row is an anonymous person crossing a doorway line on one of this room&apos;s cameras.
        Nobody is identified, and an entry is never matched to an exit. Times are in {room.facility_timezone}.
      </p>
      <nav aria-label="Filter room transitions">
        <Link href={pageHref(classroomId, null, null)} aria-current={type === null ? "page" : undefined}>
          All
        </Link>{" "}
        ·{" "}
        <Link href={pageHref(classroomId, null, "ENTERED")} aria-current={type === "ENTERED" ? "page" : undefined}>
          Entries
        </Link>{" "}
        ·{" "}
        <Link href={pageHref(classroomId, null, "EXITED")} aria-current={type === "EXITED" ? "page" : undefined}>
          Exits
        </Link>
      </nav>
      {result.status === "error" ? (
        <p role="alert">Room transitions could not be loaded. Try again in a moment.</p>
      ) : result.page.events.length === 0 ? (
        <p>{cursor === null ? "No room transitions have been reported for this classroom yet." : "No older room transitions."}</p>
      ) : (
        <ul className="staff-list" aria-label="Room transitions">
          {result.page.events.map((event) => (
            <li className="staff-row" key={event.event_id}>
              <div>
                <h3>{transitionSentence(event)}</h3>
                <p className="staff-meta">
                  {when(event.occurred_at)} · {event.camera_name}
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}
      {result.status === "ok" && result.page.next_cursor !== null ? (
        <p>
          <Link href={pageHref(classroomId, result.page.next_cursor, type)}>Older transitions</Link>
        </p>
      ) : null}
      {cursor !== null ? (
        <p>
          <Link href={pageHref(classroomId, null, type)}>Newest transitions</Link>
        </p>
      ) : null}
    </main>
  );
}
