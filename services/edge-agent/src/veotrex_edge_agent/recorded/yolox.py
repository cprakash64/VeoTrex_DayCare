"""The real TensorRT person detector, behind the ``PersonDetector`` boundary (V1-02B1A).

A thin adapter over machinery that already exists and is already qualified: the isolated
``/usr/bin/python3 -I`` GPU worker (ADR 0008), the YOLOX-S FP16 engine it validates by SHA-256
before deserialising (ADR 0010), and ``ReferenceImageDetector``'s preprocessing and
source-coordinate mapping (ADR 0011). Nothing about the model, the engine or the IPC is
re-implemented here; this class exists so the pipeline can depend on a protocol instead of on
CUDA.

**Local evaluation only.** ADR 0009 accepted YOLOX-S provisionally: the upstream code is
Apache-2.0, but upstream states no separate terms for the pretrained artifact, so redistribution
remains subject to licence review. Like the face backend of V1-02B0, it therefore refuses to
construct outside local/development/test/ci, and the environment is checked here as well as by
the caller so the refusal does not depend on one call site remembering to ask.

The engine is a locally built, git-ignored artifact that the worker resolves itself from a
fixed candidate path. Nothing downloads a model, here or anywhere below here.

Only the person class is returned. The GPU worker's decoder already restricts YOLOX-S's 80
COCO classes to person before anything leaves the worker, so the check here is a second one
at the boundary rather than the only one - a non-person box reaching the tracker would become
a person track that no downstream stage could tell apart from a real one.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from veotrex_edge_agent.recorded.model import DetectedPerson

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np
    from numpy.typing import NDArray

# Environments a provisionally-licensed, evaluation-only detector may run in. Staging and
# production are absent by construction, and a test asserts it.
EVALUATION_ENVIRONMENTS = frozenset({"local", "development", "test", "ci"})

MODEL_ID = "yolox-s-fp16"
MODEL_VERSION = "r4b"
# COCO class 0. The worker returns class indices; this adapter is where they stop being a
# model implementation detail.
PERSON_CLASS_ID = 0


class DetectorUnavailable(Exception):
    """This detector cannot run here. Raised at construction or start, never per frame."""


class YoloxPersonDetector:
    """YOLOX-S FP16 in the isolated GPU worker, exposed as a ``PersonDetector``.

    The worker process is owned for the lifetime of this object: ``start`` launches and loads,
    ``close`` stops it, and both are idempotent so a caller unwinding an error path cannot
    leave a CUDA context behind.
    """

    model_id = MODEL_ID
    model_version = MODEL_VERSION

    def __init__(self, *, environment: str, high_recall: bool = True) -> None:
        if environment not in EVALUATION_ENVIRONMENTS:
            raise DetectorUnavailable(
                "the YOLOX evaluation detector is permitted only in local/development/test/ci"
            )
        self._environment = environment
        self._high_recall = high_recall
        self._supervisor: Any | None = None
        self._detector: Any | None = None

    @property
    def ready(self) -> bool:
        return self._detector is not None

    def start(self) -> dict[str, Any]:
        """Launch the worker and load the engine, or refuse.

        The imports are local: a machine with no TensorRT must still be able to import this
        module, so that the environment gate and the class contract remain testable there.
        """
        if self._detector is not None:
            return {"model_id": self.model_id, "already_started": True}
        try:
            from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
            from veotrex_edge_agent.image_inference import ReferenceImageDetector
        except ImportError:
            raise DetectorUnavailable("gpu_worker_unavailable") from None
        supervisor = GpuWorkerSupervisor()
        try:
            supervisor.start()
            loaded = supervisor.load_model()
        except Exception:
            # A worker that started but failed to load must not be left running; stopping it
            # must not mask the load failure that is about to be reported.
            with contextlib.suppress(Exception):
                supervisor.stop()
            raise DetectorUnavailable("gpu_worker_start_failed") from None
        self._supervisor = supervisor
        self._detector = ReferenceImageDetector(supervisor)
        return {
            "model_id": self.model_id,
            "model_artifact_sha256": loaded.get("model_artifact_sha256"),
            "load_ms": loaded.get("load_ms"),
        }

    def detect(
        self, image: NDArray[np.uint8], *, frame_index: int, timestamp_ms: float
    ) -> list[DetectedPerson]:
        if self._detector is None:
            raise DetectorUnavailable("detector_not_started")
        from veotrex_edge_agent.image_inference import DetectionProfile
        from veotrex_edge_agent.image_pipeline import PixelFormat

        # The frame arrives BGR from the video source and the existing preprocessing handles
        # both orderings, so the format is declared rather than the frame being copied into a
        # converted one - a full-resolution copy per frame is exactly the cost to avoid here.
        result = self._detector.infer_decoded(
            image,
            pixel_format=PixelFormat.BGR8,
            frame_id=f"recorded-{frame_index}",
            profile=(
                DetectionProfile.TRACKING_HIGH_RECALL
                if self._high_recall
                else DetectionProfile.NORMAL
            ),
        )
        people: list[DetectedPerson] = []
        for item in result.get("detections", ()):
            if not isinstance(item, dict):
                continue
            if int(item.get("class_id", -1)) != PERSON_CLASS_ID:
                continue
            box = item.get("bbox_xyxy_source")
            score = item.get("score")
            if not isinstance(box, dict) or not isinstance(score, int | float):
                continue
            try:
                people.append(
                    DetectedPerson(
                        (
                            float(box["x1"]),
                            float(box["y1"]),
                            float(box["x2"]),
                            float(box["y2"]),
                        ),
                        float(score),
                        frame_index,
                        timestamp_ms,
                    )
                )
            except (KeyError, TypeError, ValueError):
                # One malformed box is dropped; the frame's other detections still stand.
                continue
        return people

    def close(self) -> None:
        """Stop the worker. Safe to call more than once and before ``start``."""
        supervisor, self._supervisor = self._supervisor, None
        self._detector = None
        if supervisor is not None:
            # Shutdown must never raise over the error that triggered it.
            with contextlib.suppress(Exception):
                supervisor.stop()

    def __enter__(self) -> YoloxPersonDetector:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
