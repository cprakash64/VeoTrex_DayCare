"""Operator entry point for one recorded-video tracking run (V1-02B1A).

Wires a detector, the pipeline and the output writers together. Kept separate from the CLI so
a whole run is callable - and therefore testable - without going through argument parsing.

Everything the operator gets is opt-in: the tracks file goes where they asked, the annotated
video exists only if they passed ``--annotate``, and the input video is opened read-only and
is never modified, moved or copied.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from veotrex_edge_agent.recorded.detector import PersonDetector
from veotrex_edge_agent.recorded.output import (
    ANNOTATED_FILENAME,
    TRACKS_FILENAME,
    AnnotatedVideoWriter,
    OutputError,
    run_header,
    write_ndjson,
)
from veotrex_edge_agent.recorded.pipeline import RecordedTrackingPipeline
from veotrex_edge_agent.recorded.source import RecordedVideoSource
from veotrex_edge_agent.tracking import TrackingConfig


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    tracks_path: Path
    annotated_path: Path | None
    records_written: int
    unique_tracks: int
    frames_processed: int
    metrics: dict[str, Any]


def run_recorded_tracking(
    video: Path,
    output_dir: Path,
    detector: PersonDetector,
    *,
    tracking_config: TrackingConfig | None = None,
    sample_every: int = 1,
    max_frames: int | None = None,
    annotate: bool = False,
    run_id: str | None = None,
) -> RunResult:
    """Process one video into ``output_dir``.

    The directory is created 0700 if absent, and both artefacts refuse to overwrite an
    existing file, so a second run into the same directory fails loudly rather than quietly
    replacing the evidence from the first.
    """
    logger = structlog.get_logger()
    resolved_run_id = run_id or f"run-{int(time.time() * 1000)}"
    video = Path(video)
    output_dir = Path(output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise OutputError("output_dir_unavailable") from None

    tracks_path = output_dir / TRACKS_FILENAME

    # The header needs the source geometry, and the ndjson header is written before any
    # record. The file is therefore opened once here purely to read its metadata and closed
    # again; that costs a container probe and no decoding, and it means an unusable video is
    # refused before any output file is created.
    with RecordedVideoSource(video, sample_every=sample_every) as probe:
        metadata = probe.metadata

    pipeline = RecordedTrackingPipeline(
        detector,
        tracking_config=tracking_config,
        sample_every=sample_every,
        max_frames=max_frames,
    )
    header = run_header(
        video,
        metadata,
        detector_id=detector.model_id,
        detector_version=detector.model_version,
        run_id=resolved_run_id,
    )

    annotated_path: Path | None = None
    annotator: AnnotatedVideoWriter | None = None
    if annotate:
        annotated_path = output_dir / ANNOTATED_FILENAME
        annotator = AnnotatedVideoWriter(
            annotated_path,
            width=metadata.width,
            height=metadata.height,
            # Sampling changes the real time between emitted frames, so the annotated video's
            # frame rate follows the sampled cadence and plays back at the source's speed.
            fps=(metadata.source_fps or 15.0) / max(1, sample_every),
        )

    seen_tracks: set[int] = set()

    def records() -> Any:
        hook = annotator.write if annotator is not None else None
        for record in pipeline.run(video, run_id=resolved_run_id, frame_hook=hook):
            if record.observation is not None:
                seen_tracks.add(record.observation.track_id)
            yield record

    try:
        written = write_ndjson(
            records(),
            tracks_path,
            header=header,
            # Evaluated after the last record, so the footer holds the run's final numbers.
            metrics_factory=pipeline.metrics.snapshot,
        )
    finally:
        if annotator is not None:
            annotator.close()

    logger.info(
        "recorded_tracking_completed",
        run_id=resolved_run_id,
        frames=pipeline.metrics.video_frames_processed_total,
        detections=pipeline.metrics.person_detections_total,
        tracks=len(seen_tracks),
        annotated=annotate,
    )
    return RunResult(
        run_id=resolved_run_id,
        tracks_path=tracks_path,
        annotated_path=annotated_path,
        records_written=written,
        unique_tracks=len(seen_tracks),
        frames_processed=pipeline.metrics.video_frames_processed_total,
        metrics=pipeline.metrics.snapshot(),
    )
