"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  clientUploadCheck,
  rejectionMessage,
  type EnrollmentImageSummary,
  type StaffPresentation,
  type StaffSummary,
} from "../../../../lib/staff";

type Props = {
  member: StaffSummary;
  images: ReadonlyArray<EnrollmentImageSummary>;
  canManage: boolean;
  view: StaffPresentation;
};

type Busy = "idle" | "uploading" | "removing" | "lifecycle" | "deleting";

export function TeacherControls({ member, images, canManage, view }: Props) {
  const router = useRouter();
  const [busy, setBusy] = useState<Busy>("idle");
  const [notice, setNotice] = useState<{ kind: "status" | "alert"; text: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  async function upload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || busy !== "idle") return;
    const precheck = clientUploadCheck(file);
    if (precheck) {
      setNotice({ kind: "alert", text: precheck });
      return;
    }
    setBusy("uploading");
    setNotice({ kind: "status", text: "Uploading and checking the photo…" });
    try {
      const form = new FormData();
      form.append("photo", file);
      const response = await fetch(`/api/staff/${member.staff_id}/images`, { method: "POST", body: form });
      const body = (await response.json().catch(() => ({}))) as { status?: string; category?: string };
      if (response.ok) {
        setNotice({ kind: "status", text: "Photo accepted." });
        router.refresh();
      } else if (body.status === "rejected") {
        setNotice({ kind: "alert", text: rejectionMessage(body.category) });
      } else {
        setNotice({ kind: "alert", text: "The photo could not be uploaded right now. Try again." });
      }
    } catch {
      setNotice({ kind: "alert", text: "The photo could not be uploaded right now. Try again." });
    } finally {
      setBusy("idle");
    }
  }

  async function post(path: string, method: "POST" | "DELETE", kind: Busy, success: string) {
    if (busy !== "idle") return false;
    setBusy(kind);
    try {
      const response = await fetch(path, { method });
      if (!response.ok) throw new Error("failed");
      setNotice({ kind: "status", text: success });
      router.refresh();
      return true;
    } catch {
      setNotice({ kind: "alert", text: "The change could not be saved. Try again." });
      return false;
    } finally {
      setBusy("idle");
    }
  }

  async function remove(imageId: string) {
    await post(`/api/staff/${member.staff_id}/images/${imageId}`, "DELETE", "removing", "Photo removed.");
  }

  async function lifecycle(active: boolean) {
    await post(
      `/api/staff/${member.staff_id}/${active ? "activate" : "deactivate"}`,
      "POST",
      "lifecycle",
      active ? "Teacher activated." : "Teacher deactivated; they will not be recognized.",
    );
  }

  async function deleteTeacher() {
    const ok = await post(`/api/staff/${member.staff_id}`, "DELETE", "deleting", "Teacher deleted.");
    if (ok) router.push("/app/staff");
  }

  return (
    <>
      {notice ? <p role={notice.kind}>{notice.text}</p> : null}
      <section aria-labelledby="photos">
        <h2 id="photos">Enrollment photos</h2>
        {canManage && view.canUpload ? (
          <form className="stack" onSubmit={(event) => event.preventDefault()}>
            <label htmlFor="photo">Add a photo (JPEG or PNG, one person, face clearly visible)</label>
            <input
              id="photo"
              type="file"
              accept="image/jpeg,image/png"
              onChange={upload}
              disabled={busy !== "idle"}
              aria-busy={busy === "uploading"}
            />
          </form>
        ) : null}
        {canManage && !view.canUpload && member.status === "ACTIVE" ? (
          <p className="staff-meta">The maximum number of photos is enrolled.</p>
        ) : null}
        {images.length === 0 ? (
          <p>No photos enrolled yet.</p>
        ) : (
          <div className="photo-grid">
            {images.map((image, index) => (
              <article className="photo-card" key={image.image_id}>
                {/* Private biometric material served by the BFF with no-store; it must not pass
                    through an image optimizer or a public loader, so a plain img is deliberate. */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={`/api/staff/${member.staff_id}/images/${image.image_id}`}
                  alt={`Enrollment photo ${index + 1} of ${member.display_name}`}
                />
                <small>
                  {image.width}×{image.height} · {image.template_state === "READY" ? "Processed" : "Not processed"}
                </small>
                {canManage ? (
                  <button
                    className="secondary danger"
                    type="button"
                    disabled={busy !== "idle"}
                    onClick={() => remove(image.image_id)}
                  >
                    Remove
                  </button>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>
      {canManage ? (
        <section aria-labelledby="lifecycle">
          <h2 id="lifecycle">Status</h2>
          {view.canDeactivate ? (
            <button className="secondary" type="button" disabled={busy !== "idle"} onClick={() => lifecycle(false)}>
              Deactivate
            </button>
          ) : null}
          {view.canActivate ? (
            <button className="primary" type="button" disabled={busy !== "idle"} onClick={() => lifecycle(true)}>
              Activate
            </button>
          ) : null}
          <h2>Delete teacher</h2>
          <p className="staff-meta">
            Deleting removes every enrollment photo and face template for {member.display_name}. This
            cannot be undone.
          </p>
          {confirmDelete ? (
            <>
              <button className="secondary danger" type="button" disabled={busy !== "idle"} onClick={deleteTeacher}>
                {busy === "deleting" ? "Deleting…" : "Yes, delete permanently"}
              </button>{" "}
              <button className="secondary" type="button" disabled={busy !== "idle"} onClick={() => setConfirmDelete(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button className="secondary danger" type="button" disabled={busy !== "idle"} onClick={() => setConfirmDelete(true)}>
              Delete teacher…
            </button>
          )}
        </section>
      ) : null}
    </>
  );
}
