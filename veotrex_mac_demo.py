#!/usr/bin/env python3
"""VeoTrex live person-detection demo for Apple Silicon macOS.

An isolated demo utility. It does not import, modify or depend on the VeoTrex control plane
or the edge agent: it opens a camera, runs Ultralytics YOLO person detection with tracking,
and draws a clean overlay suitable for showing a customer.

    python3 veotrex_mac_demo.py
    python3 veotrex_mac_demo.py --camera 1      # index from list_mac_cameras.py

Press Q or ESC to quit.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import urllib.request
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

PERSON_CLASS_ID = 0  # COCO class 0 is "person"; nothing else is detected or drawn.
DEFAULT_MODEL = "yolov8n.pt"
DEFAULT_CONFIDENCE = 0.35
DEFAULT_IMAGE_SIZE = 640
WINDOW_TITLE = "VeoTrex - Live Safety Intelligence"

# BGR, because OpenCV. Muted VeoTrex green on a dark panel.
ACCENT = (178, 224, 109)
PANEL = (21, 31, 7)
TEXT = (239, 244, 232)
MUTED = (166, 179, 147)
MAX_CONSECUTIVE_READ_FAILURES = 30
TELEMETRY_URL = "http://127.0.0.1:8765/telemetry"


class TelemetryPublisher:
    """Best-effort delivery off the inference thread; a missing dashboard is harmless."""

    def __init__(self) -> None:
        self.pending: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self.last_sent = 0.0
        threading.Thread(target=self._deliver, daemon=True).start()

    def publish(self, people_count: int, tracked_count: int, fps: float | None) -> None:
        now = time.monotonic()
        if now - self.last_sent < 0.2:
            return
        self.last_sent = now
        payload = {
            "camera_id": "classroom-demo",
            "camera_name": "Classroom Demo",
            "camera_status": "live",
            "ai_status": "active",
            "people_count": people_count,
            "tracked_count": tracked_count,
            "fps": round(fps, 1) if fps is not None else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.pending.put_nowait(payload)
        except queue.Full:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            self.pending.put_nowait(payload)

    def _deliver(self) -> None:
        while True:
            payload = self.pending.get()
            request = urllib.request.Request(
                TELEMETRY_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=0.3).close()
            except (OSError, ValueError):
                pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VeoTrex live person detection and tracking demo for macOS.",
        epilog="Find your camera index with: python3 list_mac_cameras.py",
    )
    parser.add_argument(
        "--camera", type=int, default=0, help="Camera index (default: 0, the built-in camera)."
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ultralytics model weight (default: {DEFAULT_MODEL}, downloaded on first run).",
    )
    parser.add_argument(
        "--conf",
        type=float,
        default=DEFAULT_CONFIDENCE,
        help=f"Minimum detection confidence (default: {DEFAULT_CONFIDENCE}).",
    )
    parser.add_argument(
        "--imgsz",
        type=int,
        default=DEFAULT_IMAGE_SIZE,
        help=f"Inference size; lower is faster (default: {DEFAULT_IMAGE_SIZE}).",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "mps", "cpu"),
        default="auto",
        help="Compute device (default: auto, which prefers Apple GPU and falls back to CPU).",
    )
    parser.add_argument(
        "--width", type=int, default=1280, help="Requested capture width (default: 1280)."
    )
    parser.add_argument(
        "--height", type=int, default=720, help="Requested capture height (default: 720)."
    )
    args = parser.parse_args(argv)
    if args.camera < 0:
        parser.error("--camera must be 0 or greater")
    if not 0.0 < args.conf < 1.0:
        parser.error("--conf must be between 0 and 1")
    if not 160 <= args.imgsz <= 1280:
        parser.error("--imgsz must be between 160 and 1280")
    return args


def resolve_device(requested: str) -> str:
    """Prefer the Apple GPU when it is genuinely usable, otherwise CPU. Never CUDA."""
    if requested != "auto":
        return requested
    try:
        import torch

        if torch.backends.mps.is_available() and torch.backends.mps.is_built():
            return "mps"
    except (ImportError, AttributeError):
        pass
    return "cpu"


def camera_backend(cv2: Any) -> int:
    if sys.platform == "darwin":
        return int(getattr(cv2, "CAP_AVFOUNDATION", 0))
    return int(getattr(cv2, "CAP_ANY", 0))


def camera_help(index: int) -> str:
    return (
        f"\nCould not open camera {index}.\n\n"
        "  1. System Settings > Privacy & Security > Camera - allow Terminal (or iTerm).\n"
        "     After granting access, quit and reopen the terminal completely.\n"
        "  2. List what is actually available:  python3 list_mac_cameras.py\n"
        f"  3. Try another index, e.g.  python3 veotrex_mac_demo.py --camera {index + 1}\n"
        "  4. For an iPhone: unlock it, keep it near the Mac, and make sure Continuity\n"
        "     Camera is enabled on the phone (Settings > General > AirPlay & Continuity).\n"
    )


def open_camera(cv2: Any, index: int, width: int, height: int) -> Any:
    capture = cv2.VideoCapture(index, camera_backend(cv2))
    if not capture.isOpened():
        capture.release()
        return None
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return capture


def extract_people(result: Any) -> list[tuple[tuple[int, int, int, int], float, int | None]]:
    """Pull person boxes, confidences and track ids out of one Ultralytics result."""
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []
    people: list[tuple[tuple[int, int, int, int], float, int | None]] = []
    xyxy = boxes.xyxy.cpu().numpy()
    confidences = boxes.conf.cpu().numpy()
    classes = boxes.cls.cpu().numpy()
    identifiers = boxes.id.cpu().numpy() if getattr(boxes, "id", None) is not None else None
    for position in range(len(xyxy)):
        if int(classes[position]) != PERSON_CLASS_ID:
            continue
        x1, y1, x2, y2 = (int(value) for value in xyxy[position][:4])
        track_id = int(identifiers[position]) if identifiers is not None else None
        people.append(((x1, y1, x2, y2), float(confidences[position]), track_id))
    return people


def draw_overlay(
    cv2: Any,
    frame: Any,
    people: list[tuple[tuple[int, int, int, int], float, int | None]],
    fps: float | None,
) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    for (x1, y1, x2, y2), confidence, track_id in people:
        cv2.rectangle(frame, (x1, y1), (x2, y2), ACCENT, 2)
        label = f"Person #{track_id}" if track_id is not None else "Person"
        label = f"{label}  {confidence:.2f}"
        (text_width, text_height), _ = cv2.getTextSize(label, font, 0.55, 1)
        top = max(0, y1 - text_height - 9)
        cv2.rectangle(frame, (x1, top), (x1 + text_width + 12, top + text_height + 9), ACCENT, -1)
        cv2.putText(
            frame, label, (x1 + 6, top + text_height + 2), font, 0.55, PANEL, 1, cv2.LINE_AA
        )

    height, width = frame.shape[:2]
    panel_height = 108
    panel = frame[0:panel_height, 0:width].copy()
    cv2.rectangle(panel, (0, 0), (width, panel_height), PANEL, -1)
    cv2.addWeighted(
        panel, 0.78, frame[0:panel_height, 0:width], 0.22, 0, frame[0:panel_height, 0:width]
    )
    cv2.line(frame, (0, panel_height), (width, panel_height), ACCENT, 2)

    cv2.putText(frame, "VeoTrex", (22, 38), font, 0.95, TEXT, 2, cv2.LINE_AA)
    cv2.putText(frame, "LIVE SAFETY INTELLIGENCE", (22, 62), font, 0.48, ACCENT, 1, cv2.LINE_AA)
    cv2.putText(frame, "Camera: LIVE", (22, 92), font, 0.52, MUTED, 1, cv2.LINE_AA)
    cv2.putText(
        frame, f"People detected: {len(people)}", (210, 92), font, 0.52, TEXT, 1, cv2.LINE_AA
    )
    # Never a placeholder number: until FPS has actually been measured it shows as a dash.
    processing = "AI processing: --" if fps is None else f"AI processing: {fps:.1f} FPS"
    cv2.putText(frame, processing, (470, 92), font, 0.52, TEXT, 1, cv2.LINE_AA)
    cv2.putText(frame, "Q or ESC to quit", (width - 190, 92), font, 0.45, MUTED, 1, cv2.LINE_AA)


def run(args: argparse.Namespace) -> int:
    try:
        import cv2
    except ImportError:
        print("OpenCV is not installed. Run: pip install -r requirements-mac-demo.txt")
        return 1
    try:
        from ultralytics import YOLO
    except ImportError:
        print("Ultralytics is not installed. Run: pip install -r requirements-mac-demo.txt")
        return 1

    device = resolve_device(args.device)
    print(f"VeoTrex demo starting - model={args.model} device={device} camera={args.camera}")
    if device == "cpu":
        print("Apple GPU (MPS) unavailable; running on CPU. This is slower but works.")

    try:
        model = YOLO(args.model)
    except Exception as error:
        print(f"\nCould not load model '{args.model}': {error}")
        print("The default weight downloads automatically on first run and needs internet once.")
        return 1

    capture = open_camera(cv2, args.camera, args.width, args.height)
    if capture is None:
        print(camera_help(args.camera))
        return 1

    supports_tracking = True
    publisher = TelemetryPublisher()
    smoothed_fps: float | None = None
    failures = 0
    window_created = False
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                # A dropped frame is normal, especially just after a Continuity Camera
                # connects. Only give up once they stop arriving altogether.
                failures += 1
                if failures >= MAX_CONSECUTIVE_READ_FAILURES:
                    print("\nThe camera stopped delivering frames. Exiting.")
                    return 1
                time.sleep(0.03)
                continue
            failures = 0

            started = time.perf_counter()
            if supports_tracking:
                try:
                    results = model.track(
                        frame,
                        persist=True,
                        classes=[PERSON_CLASS_ID],
                        conf=args.conf,
                        imgsz=args.imgsz,
                        device=device,
                        verbose=False,
                    )
                except Exception:
                    supports_tracking = False
                    print("Tracking unavailable; continuing with detection only.")
                    continue
            else:
                results = model.predict(
                    frame,
                    classes=[PERSON_CLASS_ID],
                    conf=args.conf,
                    imgsz=args.imgsz,
                    device=device,
                    verbose=False,
                )
            elapsed = time.perf_counter() - started

            people = extract_people(results[0]) if results else []
            if elapsed > 0:
                instant = 1.0 / elapsed
                # Exponential smoothing keeps the displayed number from jumping around.
                smoothed_fps = (
                    instant if smoothed_fps is None else (smoothed_fps * 0.85 + instant * 0.15)
                )
            draw_overlay(cv2, frame, people, smoothed_fps)
            publisher.publish(
                len(people),
                sum(track_id is not None for _, _, track_id in people),
                smoothed_fps,
            )

            cv2.imshow(WINDOW_TITLE, frame)
            window_created = True
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            try:
                if cv2.getWindowProperty(WINDOW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
    except KeyboardInterrupt:
        pass
    finally:
        capture.release()
        if window_created:
            cv2.destroyAllWindows()
            # macOS needs a few event-loop turns to actually close the window.
            for _ in range(4):
                cv2.waitKey(1)
    print("VeoTrex demo stopped.")
    return 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
