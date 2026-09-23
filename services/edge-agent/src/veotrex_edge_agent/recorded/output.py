"""Machine-readable output for a recorded-video tracking run (V1-02B1A).

``tracks.ndjson`` is one JSON object per line: a ``header`` first, then ``observation`` and
``track_summary`` records in the order the pipeline decided them, then a ``footer`` carrying
the run's metrics. Newline-delimited rather than one large document so an operator can watch a
long run with ``tail -f``, and so a truncated run is still readable up to its last line.

Every record carries ``schema_version``. A consumer that does not recognise the version must
refuse the file rather than guess at its shape.

What is deliberately absent is as much a part of the contract as what is present: **no face
embedding, no biometric template, no face crop and no frame image ever appears here.** The
records carry geometry, timing and counts. ``assert_no_biometric_material`` enforces that as a
runtime check rather than a convention, and a test runs it over real output.

The optional annotated video is an evaluation aid, written only when an operator asks for it,
and never over the input.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from veotrex_edge_agent.recorded.model import TRACK_SCHEMA_VERSION
from veotrex_edge_agent.recorded.pipeline import PipelineRecord
from veotrex_edge_agent.recorded.source import VideoMetadata

TRACKS_FILENAME = "tracks.ndjson"
ANNOTATED_FILENAME = "annotated.mp4"

# Keys that must never appear anywhere in an emitted record, at any nesting depth. Checked by
# name because the failure this guards against is a future field added without thinking, not a
# deliberate attempt to smuggle one.
FORBIDDEN_KEYS = frozenset(
    {
        "embedding",
        "embeddings",
        "template",
        "templates",
        "face",
        "face_crop",
        "crop",
        "image",
        "frame_image",
        "pixels",
        "descriptor",
        "biometric",
        "data_base64",
    }
)


class OutputError(Exception):
    """A bounded category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def assert_no_biometric_material(record: Any, *, path: str = "") -> None:
    """Raise if a record contains anything shaped like biometric material.

    Two rules. A forbidden key name anywhere in the structure is refused outright. So is any
    long base64-looking string, which is what an embedding would become if someone serialised
    one into an innocuously-named field.
    """
    if isinstance(record, dict):
        for key, value in record.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_KEYS:
                raise OutputError("biometric_material_in_output")
            assert_no_biometric_material(value, path=f"{path}.{lowered}")
        return
    if isinstance(record, list | tuple):
        for item in record:
            assert_no_biometric_material(item, path=path)
        return
    if isinstance(record, str) and len(record) > 128:
        # No legitimate field in this schema is a long opaque string.
        raise OutputError("oversized_opaque_value_in_output")


def _encode(record: PipelineRecord) -> dict[str, Any]:
    if record.kind == "observation" and record.observation is not None:
        payload = asdict(record.observation)
        payload["lifecycle"] = str(record.observation.lifecycle)
        return {"schema_version": TRACK_SCHEMA_VERSION, "type": "observation", **payload}
    if record.kind == "track_summary" and record.summary is not None:
        payload = asdict(record.summary)
        payload["end_reason"] = str(record.summary.end_reason)
        payload["duration_ms"] = round(record.summary.duration_ms, 3)
        return {"schema_version": TRACK_SCHEMA_VERSION, "type": "track_summary", **payload}
    raise OutputError("unencodable_record")  # pragma: no cover - defensive


def _open_private(path: Path) -> Any:
    """Create the output file exclusively at mode 0600.

    Tracking output describes where people were and when. It is not biometric, but it is not
    public either, so it is created by this process rather than written into whatever file
    already sat at that path with whatever permissions it had.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise OutputError("output_already_exists") from None
    except OSError:
        raise OutputError("output_not_writable") from None
    return os.fdopen(descriptor, "w", encoding="utf-8")


def write_ndjson(
    records: Iterable[PipelineRecord],
    destination: Path,
    *,
    header: dict[str, Any],
    metrics_factory: Callable[[], dict[str, Any]] | None = None,
) -> int:
    """Stream records to ``destination``. Returns the number of records written.

    Written as the pipeline yields, so memory does not grow with the length of the video and a
    long run is observable while it happens.

    ``metrics_factory`` is called once, after the last record, so the footer carries the run's
    final numbers rather than a snapshot taken before any frame was processed.
    """
    written = 0
    handle = _open_private(destination)
    try:
        head = {"schema_version": TRACK_SCHEMA_VERSION, "type": "header", **header}
        assert_no_biometric_material(head)
        handle.write(json.dumps(head, sort_keys=True, separators=(",", ":")) + "\n")
        for record in records:
            payload = _encode(record)
            assert_no_biometric_material(payload)
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
            written += 1
        foot = {
            "schema_version": TRACK_SCHEMA_VERSION,
            "type": "footer",
            "records": written,
            "metrics": metrics_factory() if metrics_factory is not None else {},
        }
        assert_no_biometric_material(foot)
        handle.write(json.dumps(foot, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        handle.close()
        # A partial run's output is misleading rather than useful.
        destination.unlink(missing_ok=True)
        raise
    handle.close()
    return written


def run_header(
    video: Path,
    metadata: VideoMetadata,
    *,
    detector_id: str,
    detector_version: str,
    run_id: str,
) -> dict[str, Any]:
    """Provenance for one run. Names the video by filename only.

    The operator's full directory layout is not interesting to a consumer and is exactly the
    kind of thing that ends up pasted into an issue, so only the basename is recorded.
    """
    return {
        "run_id": run_id,
        "video_filename": video.name,
        "detector_id": detector_id,
        "detector_version": detector_version,
        "source_width": metadata.width,
        "source_height": metadata.height,
        "source_fps": metadata.source_fps,
        "source_duration_ms": metadata.duration_ms,
        "identity_annotation": "none",
    }


class AnnotatedVideoWriter:
    """Optional local evaluation video: boxes, track ids and the media timestamp.

    Evaluation only. It is written beside the ndjson, never over the input, and only when the
    operator asked for it. No name is drawn - this stage has no identities, and a label on a
    box is exactly the thing that would make an anonymous track look like a named person.
    """

    def __init__(self, destination: Path, *, width: int, height: int, fps: float) -> None:
        self._destination = Path(destination)
        self._width = width
        self._height = height
        self._fps = fps if fps and fps > 0 else 15.0
        self._writer: Any | None = None

    def _ensure(self) -> Any:
        if self._writer is not None:
            return self._writer
        try:
            import cv2
        except ImportError:
            raise OutputError("video_backend_unavailable") from None
        if self._destination.exists():
            raise OutputError("output_already_exists")
        # ``VideoWriter_fourcc`` exists at runtime but is absent from the published stubs.
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]
        writer = cv2.VideoWriter(
            str(self._destination),
            fourcc,
            self._fps,
            (self._width, self._height),
        )
        if not writer.isOpened():
            raise OutputError("annotated_output_unavailable")
        self._writer = writer
        return writer

    def write(
        self,
        image: Any,
        boxes: Sequence[tuple[int, tuple[float, ...]]],
        timestamp_ms: float,
    ) -> None:
        import cv2

        writer = self._ensure()
        # Drawn on a copy: the caller's frame is the one the detector saw, and mutating it
        # would mean the annotation could change what a later consumer of that frame sees.
        canvas = image.copy()
        for track_id, box in boxes:
            x1, y1, x2, y2 = box
            colour = _track_colour(track_id)
            cv2.rectangle(canvas, (int(x1), int(y1)), (int(x2), int(y2)), colour, 2)
            cv2.putText(
                canvas,
                f"track {track_id}",
                (int(x1), max(12, int(y1) - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                colour,
                1,
                cv2.LINE_AA,
            )
        cv2.putText(
            canvas,
            f"{timestamp_ms / 1000.0:8.2f}s",
            (8, 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        writer.write(canvas)

    def close(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            # Release must never mask the error that caused the unwind.
            with contextlib.suppress(Exception):
                writer.release()

    def __enter__(self) -> AnnotatedVideoWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _track_colour(track_id: int) -> tuple[int, int, int]:
    """A stable BGR colour per track id, so a reviewer can follow one person by eye."""
    palette = (
        (0, 200, 0),
        (255, 128, 0),
        (0, 160, 255),
        (200, 0, 200),
        (0, 220, 220),
        (255, 80, 80),
        (140, 255, 140),
        (80, 80, 255),
    )
    return palette[track_id % len(palette)]


def read_ndjson(path: Path) -> Iterator[dict[str, Any]]:
    """Read a tracks file back, refusing a schema version this build does not know."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("schema_version") != TRACK_SCHEMA_VERSION:
                raise OutputError("unsupported_schema_version")
            yield record
