"""Real face detection and templates for local qualification only (V1-02B0).

YuNet finds and lands-marks the face, SFace turns the aligned 112x112 crop into a 128-float
embedding. Both are audited OpenCV Zoo weights (``face_models``); YuNet's MIT licence covers
its weights outright, while SFace's weight provenance is unresolved upstream, so this backend
as a whole is LOCAL_EVALUATION_ONLY and must never run in staging or production.

That restriction is enforced three times over, because documentation is not a control:
``Settings`` refuses the backend name outside the permitted environments, ``build`` refuses to
construct the object there, and ``ensure_ready`` refuses to load a weight whose approval does
not permit the environment it is being loaded in. Any one of the three alone would stop it.

``import cv2`` happens inside ``ensure_ready``, never at module import. The control-plane image
does not install OpenCV at all, so this module has to stay importable - and therefore testable,
and safely referenced by settings validation - on a machine that has no OpenCV and no weights.

Nothing here downloads a model, and nothing here logs a pixel, a landmark or a vector.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from veotrex_api.face_backend import (
    MIN_FACE_SIZE_PX,
    FaceBackendError,
    FaceObservation,
    FaceQuery,
    FaceTemplate,
)
from veotrex_api.face_matching import encode, normalize
from veotrex_api.face_models import SFACE, YUNET, FaceModelArtifact, FaceModelError, resolve

if TYPE_CHECKING:  # pragma: no cover - typing only
    from veotrex_api.config import Settings

BACKEND_NAME = "opencv_eval"
# The one composite identity persisted on every template this backend produces. It changes
# whenever either weight changes, so templates from a different pairing can never be compared
# against these: the recognition service matches on model id and version before it compares
# anything, and the edge package is exported for one identity at a time.
MODEL_ID = f"{YUNET.model_id}+{SFACE.model_id}"
MODEL_VERSION = f"{YUNET.model_version}+{SFACE.model_version}"
TEMPLATE_VERSION = 1
DIMENSIONS = 128
DTYPE = "float32"

# Environments this backend may run in. Everything else - staging and production above all -
# is refused. Kept here as well as in Settings so neither file can quietly widen it alone.
EVALUATION_ENVIRONMENTS = frozenset({"local", "development", "test", "ci"})

# YuNet's input is set per image; these bound the work it will do.
DETECTOR_INPUT_LIMIT = 1600
MAX_REPORTED_FACES = 50


class FaceBackendUnavailable(Exception):
    """This backend cannot run here. Raised at construction or startup, never per request."""


def _numpy_bgr(image: Image.Image) -> Any:
    """PIL RGB to the contiguous uint8 BGR array OpenCV expects."""
    import numpy

    return numpy.ascontiguousarray(
        numpy.asarray(image.convert("RGB"), dtype=numpy.uint8)[:, :, ::-1]
    )


class OpenCvEvalFaceBackend:
    """Implements ``FaceEnrollmentBackend`` and ``FaceRecognitionBackend`` over YuNet + SFace.

    Both models are loaded once, at startup, from digest-verified files, and are then reused.
    OpenCV's ``FaceDetectorYN`` carries mutable input-size state, so every call sets the input
    size for the image it is about to process rather than trusting whatever the previous call
    left behind.
    """

    model_id = MODEL_ID
    model_version = MODEL_VERSION
    template_version = TEMPLATE_VERSION
    dimensions = DIMENSIONS

    def __init__(
        self,
        model_dir: Path,
        *,
        environment: str,
        detection_confidence: float = 0.9,
        nms_threshold: float = 0.3,
        min_face_area_ratio: float = 0.015,
        min_sharpness: float = 20.0,
    ) -> None:
        if environment not in EVALUATION_ENVIRONMENTS:
            raise FaceBackendUnavailable(
                "the opencv_eval face backend is permitted only in local/development/test/ci"
            )
        self._model_dir = Path(model_dir)
        self._environment = environment
        self._detection_confidence = detection_confidence
        self._nms_threshold = nms_threshold
        self._min_face_area_ratio = min_face_area_ratio
        self._min_sharpness = min_sharpness
        self._detector: Any | None = None
        self._recognizer: Any | None = None

    @property
    def ready(self) -> bool:
        return self._detector is not None and self._recognizer is not None

    # ------------------------------------------------------------------------- startup
    def _verified(self, artifact: FaceModelArtifact) -> str:
        if artifact.approval == "REJECTED":
            raise FaceBackendUnavailable(f"model {artifact.key} is rejected and must not load")
        if (
            artifact.approval != "PRODUCTION_APPROVED"
            and self._environment not in EVALUATION_ENVIRONMENTS
        ):
            raise FaceBackendUnavailable(f"model {artifact.key} is evaluation-only")
        try:
            return str(resolve(self._model_dir, artifact))
        except FaceModelError as exc:
            # The category names what is wrong with the artifact, never where it was looked for.
            raise FaceBackendUnavailable(f"model {artifact.key} unusable: {exc.category}") from None

    def ensure_ready(self) -> None:
        """Verify both weights and load them, or refuse to start.

        Called from the application lifespan alongside the media store, so a deployment that
        selects this backend without usable models fails at startup rather than answering
        requests that can never succeed.
        """
        if self.ready:
            return
        detector_path = self._verified(YUNET)
        recognizer_path = self._verified(SFACE)
        try:
            import cv2
        except ImportError:
            raise FaceBackendUnavailable(
                "the opencv_eval face backend needs the 'face-eval' dependency group"
            ) from None
        try:
            detector = cv2.FaceDetectorYN.create(
                detector_path,
                "",
                (320, 320),
                self._detection_confidence,
                self._nms_threshold,
                MAX_REPORTED_FACES,
            )
            recognizer = cv2.FaceRecognizerSF.create(recognizer_path, "")
        except cv2.error:
            raise FaceBackendUnavailable("the audited face models could not be loaded") from None
        self._detector, self._recognizer = detector, recognizer

    def _require_loaded(self) -> tuple[Any, Any]:
        if self._detector is None or self._recognizer is None:
            # Startup proves this cannot happen; per-request it is still a closed failure.
            raise FaceBackendError("face_backend_unavailable")
        return self._detector, self._recognizer

    # ------------------------------------------------------------------------ detection
    def _detect(self, image: Image.Image) -> tuple[Any, Any]:
        """Return the BGR frame and YuNet's detections, as an (N, 15) array (possibly empty).

        Each row is x, y, w, h, five landmark pairs and the confidence score; ``alignCrop``
        consumes a whole row, so rows are kept intact rather than unpacked here.
        """
        import numpy

        detector, _ = self._require_loaded()
        frame = _numpy_bgr(image)
        height, width = frame.shape[:2]
        if width < 1 or height < 1 or max(width, height) > DETECTOR_INPUT_LIMIT:
            # The canonical enrollment JPEG is already bounded to 1600 on its longest side;
            # anything larger did not come through the validator.
            raise FaceBackendError("invalid_image")
        try:
            detector.setInputSize((width, height))
            _, faces = detector.detect(frame)
        except Exception:
            # OpenCV surfaces decode and shape problems as cv2.error, which is not an
            # ImportError-shaped type we can name without importing cv2 at module scope.
            raise FaceBackendError("template_failed") from None
        if faces is None:
            faces = numpy.empty((0, 15), dtype=numpy.float32)
        return frame, faces

    def _single_face(self, image: Image.Image) -> tuple[Any, Any, FaceObservation]:
        """Detect, and insist on exactly one face large enough to be worth enrolling."""
        frame, faces = self._detect(image)
        if len(faces) == 0:
            raise FaceBackendError("no_face_detected")
        if len(faces) > 1:
            raise FaceBackendError("multiple_faces")
        row = faces[0]
        box_width, box_height = float(row[2]), float(row[3])
        face_size = int(max(box_width, box_height))
        height, width = frame.shape[:2]
        area_ratio = (box_width * box_height) / float(width * height)
        if face_size < MIN_FACE_SIZE_PX or area_ratio < self._min_face_area_ratio:
            raise FaceBackendError("face_too_small")
        score = max(0.0, min(1.0, float(row[14])))
        return frame, row, FaceObservation(1, face_size, round(score * 100))

    def _aligned(self, frame: Any, row: Any) -> Any:
        """The 112x112 crop SFace expects, warped onto the five detected landmarks.

        Alignment is what makes two photos of one person comparable, so it is never skipped
        and never approximated by a plain bounding-box crop.
        """
        _, recognizer = self._require_loaded()
        try:
            return recognizer.alignCrop(frame, row)
        except Exception:
            raise FaceBackendError("template_failed") from None

    def _sharpness(self, aligned: Any) -> float:
        """Variance of the Laplacian of the aligned crop: a deterministic, single-number
        measure of how much high-frequency detail survived. It is an extreme-blur guard, not
        a quality score - the default is set low enough that only a photo with essentially no
        detail is refused."""
        import cv2
        import numpy

        grey = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        return float(numpy.var(cv2.Laplacian(grey, cv2.CV_64F)))

    def _embedding(self, aligned: Any) -> list[float]:
        _, recognizer = self._require_loaded()
        try:
            feature = recognizer.feature(aligned)
        except Exception:
            raise FaceBackendError("template_failed") from None
        values = [float(value) for value in feature.reshape(-1)]
        if len(values) != DIMENSIONS:
            raise FaceBackendError("template_failed")
        return values

    def _face(self, image: Image.Image) -> tuple[list[float], FaceObservation]:
        frame, row, observation = self._single_face(image)
        aligned = self._aligned(frame, row)
        if self._sharpness(aligned) < self._min_sharpness:
            raise FaceBackendError("face_too_blurry")
        return self._embedding(aligned), observation

    # ------------------------------------------------------------------------ protocols
    def analyze(self, image: Image.Image) -> FaceObservation:
        """Face count and size only. Zero and many faces are reported rather than raised, so
        the enrollment service keeps deciding which category the caller is told."""
        frame, faces = self._detect(image)
        if len(faces) != 1:
            return FaceObservation(min(len(faces), MAX_REPORTED_FACES), None, None)
        row = faces[0]
        height, width = frame.shape[:2]
        box_width, box_height = float(row[2]), float(row[3])
        if (box_width * box_height) / float(width * height) < self._min_face_area_ratio:
            # Too small in the frame to enroll: reported as a size the caller will refuse,
            # so "face_too_small" comes from the one place that owns that rule.
            return FaceObservation(1, 0, None)
        score = max(0.0, min(1.0, float(row[14])))
        return FaceObservation(1, int(max(box_width, box_height)), round(score * 100))

    def extract_template(self, image: Image.Image) -> FaceTemplate:
        values, observation = self._face(image)
        return FaceTemplate(
            self.model_id,
            self.model_version,
            self.template_version,
            DIMENSIONS,
            DTYPE,
            encode(normalize(values)),
            observation.quality,
        )

    def extract_query(self, image: Image.Image) -> FaceQuery:
        values, observation = self._face(image)
        return FaceQuery(
            normalize(values),
            observation.face_size_px or MIN_FACE_SIZE_PX,
            observation.quality or 0,
        )


def build(settings: Settings) -> OpenCvEvalFaceBackend:
    """Construct the evaluation backend from settings, refusing a forbidden environment.

    The second of the three independent refusals: ``Settings`` has already refused the name,
    and ``ensure_ready`` will refuse an evaluation-only weight. This one exists so that code
    which builds a backend without going through settings validation still cannot get one.
    """
    if settings.environment not in EVALUATION_ENVIRONMENTS:
        raise FaceBackendUnavailable(
            "the opencv_eval face backend is permitted only in local/development/test/ci"
        )
    if not settings.staff_face_model_dir:
        raise FaceBackendUnavailable("VEOTREX_STAFF_FACE_MODEL_DIR is not configured")
    return OpenCvEvalFaceBackend(
        Path(settings.staff_face_model_dir),
        environment=settings.environment,
        detection_confidence=settings.staff_face_detection_confidence,
        min_face_area_ratio=settings.staff_face_min_area_ratio,
        min_sharpness=settings.staff_face_min_sharpness,
    )
