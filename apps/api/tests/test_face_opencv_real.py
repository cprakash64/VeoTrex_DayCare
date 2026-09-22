"""The real YuNet + SFace backend against the audited weights (V1-02B0). EVALUATION ONLY.

Skipped whenever OpenCV or the weights are absent, which is deliberately the case in CI: the
gate, the registry, the matching arithmetic and the whole HTTP path are covered by tests that
need neither, and CI must not depend on a 37 MiB evaluation-only model.

Run them locally after ``infra/local/fetch-face-eval-models.sh``; they are what proves that the
code paths those other suites exercise with a scripted backend behave the same way when a real
detector and a real embedding model are underneath.

No real person's photograph is committed to this repository. The one image with a face in it
is built from the recorded-video validation footage already in the tree, and only when that
footage is present.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest
from PIL import Image

from veotrex_api.face_backend import FaceBackendError
from veotrex_api.face_matching import decode, normalize, similarity
from veotrex_api.face_models import SFACE, YUNET
from veotrex_api.face_opencv import DIMENSIONS, MODEL_ID, MODEL_VERSION, OpenCvEvalFaceBackend

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_DIR = Path(
    # The same default the fetch script writes to; an operator with the models elsewhere
    # points the suite at them without editing it.
    __import__("os").environ.get("VEOTREX_STAFF_FACE_MODEL_DIR")
    or REPOSITORY_ROOT / "artifacts" / "models" / "face"
)
FOOTAGE = (
    REPOSITORY_ROOT / "artifacts/validation/r4d/overlays/silk-road-walker-5fps-contact-sheet.jpg"
)

cv2 = pytest.importorskip("cv2", reason="the face-eval dependency group is not installed")

pytestmark = pytest.mark.skipif(
    not all((MODEL_DIR / artifact.file_name).is_file() for artifact in (YUNET, SFACE)),
    reason="audited face models absent; run infra/local/fetch-face-eval-models.sh",
)


@pytest.fixture(scope="module")
def backend() -> OpenCvEvalFaceBackend:
    loaded = OpenCvEvalFaceBackend(MODEL_DIR, environment="local")
    loaded.ensure_ready()
    return loaded


def blank(size: tuple[int, int] = (480, 480), color: tuple[int, int, int] = (40, 60, 90)):  # type: ignore[no-untyped-def]
    return Image.new("RGB", size, color)


def _portrait(scale: float, padding: float) -> Image.Image:
    """A portrait-shaped crop around the first face in the validation footage.

    Two different scales and paddings of the same face stand in for "two photos of one person":
    the pixels differ, the identity does not.
    """
    import numpy

    frame = cv2.imread(str(FOOTAGE))
    height, width = frame.shape[:2]
    detector = cv2.FaceDetectorYN.create(
        str(MODEL_DIR / YUNET.file_name), "", (320, 320), 0.85, 0.3, 50
    )
    detector.setInputSize((width, height))
    _, faces = detector.detect(frame)
    assert faces is not None and len(faces) >= 1, "no face in the validation footage"
    row = faces[0]
    centre_x, centre_y = row[0] + row[2] / 2, row[1] + row[3] / 2
    half = max(float(row[2]), float(row[3])) * padding / 2
    left, top = int(max(centre_x - half, 0)), int(max(centre_y - half, 0))
    right, bottom = int(min(centre_x + half, width)), int(min(centre_y + half, height))
    crop = cv2.resize(
        frame[top:bottom, left:right], None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
    )
    return Image.fromarray(numpy.ascontiguousarray(crop[:, :, ::-1]))


requires_footage = pytest.mark.skipif(
    not FOOTAGE.is_file(), reason="recorded-video validation footage is not in this checkout"
)


# ------------------------------------------------------------------------------ model loading
def test_the_audited_models_load_and_report_their_identity(
    backend: OpenCvEvalFaceBackend,
) -> None:
    assert backend.ready
    assert backend.model_id == MODEL_ID == "yunet+sface"
    assert backend.model_version == MODEL_VERSION == "2023mar+2021dec"
    assert backend.template_version == 1


def test_the_model_identity_names_both_weights(backend: OpenCvEvalFaceBackend) -> None:
    """A template is only comparable to another produced by the same detector *and* the same
    embedding model, so changing either must change the stored identity."""
    assert YUNET.model_id in backend.model_id and SFACE.model_id in backend.model_id
    assert YUNET.model_version in backend.model_version
    assert SFACE.model_version in backend.model_version


def test_loading_is_idempotent(backend: OpenCvEvalFaceBackend) -> None:
    backend.ensure_ready()
    assert backend.ready


# --------------------------------------------------------------------------------- detection
def test_an_image_with_no_face_is_reported_as_zero_faces(
    backend: OpenCvEvalFaceBackend,
) -> None:
    observation = backend.analyze(blank())
    assert observation.face_count == 0
    assert observation.face_size_px is None


def test_extracting_a_template_from_an_image_with_no_face_is_refused(
    backend: OpenCvEvalFaceBackend,
) -> None:
    with pytest.raises(FaceBackendError) as raised:
        backend.extract_template(blank())
    assert raised.value.category == "no_face_detected"


def test_a_query_with_no_face_is_refused_the_same_way(
    backend: OpenCvEvalFaceBackend,
) -> None:
    with pytest.raises(FaceBackendError) as raised:
        backend.extract_query(blank())
    assert raised.value.category == "no_face_detected"


@requires_footage
def test_a_real_face_is_detected_measured_and_scored(
    backend: OpenCvEvalFaceBackend,
) -> None:
    observation = backend.analyze(_portrait(3.0, 1.9))
    assert observation.face_count == 1
    assert observation.face_size_px is not None and observation.face_size_px >= 80
    assert observation.quality is not None and 0 <= observation.quality <= 100


@requires_footage
def test_a_face_too_small_in_the_frame_is_refused(backend: OpenCvEvalFaceBackend) -> None:
    """A real, detectable face wide enough in pixels, but occupying too little of the frame.

    The face is pasted unscaled into a much larger canvas, so its pixel width still clears the
    absolute minimum and only the relative-area rule can refuse it. That is the rule under
    test: a person standing far back in a wide shot is not an enrollment photo.
    """
    face = _portrait(1.0, 1.9)
    canvas = Image.new("RGB", (1500, 1500), (30, 30, 30))
    canvas.paste(face, (600, 600))

    assert backend.analyze(face).face_count == 1, "the unscaled crop must still show a face"
    with pytest.raises(FaceBackendError) as raised:
        backend.extract_template(canvas)
    assert raised.value.category == "face_too_small"


@requires_footage
def test_an_image_showing_two_people_is_refused(backend: OpenCvEvalFaceBackend) -> None:
    """The validation contact sheet shows more than one face, which is exactly the enrollment
    photo an operator must not be allowed to submit."""
    whole_sheet = Image.open(FOOTAGE).convert("RGB")
    observation = backend.analyze(whole_sheet)
    if observation.face_count < 2:
        pytest.skip("this checkout's footage yields fewer than two detectable faces")
    with pytest.raises(FaceBackendError) as raised:
        backend.extract_template(whole_sheet)
    assert raised.value.category == "multiple_faces"


# ----------------------------------------------------------------------- template extraction
@requires_footage
def test_a_real_template_matches_the_stored_contract(
    backend: OpenCvEvalFaceBackend,
) -> None:
    template = backend.extract_template(_portrait(3.0, 1.9))
    assert template.dimensions == DIMENSIONS == 128
    assert template.dtype == "float32"
    assert len(template.data) == DIMENSIONS * 4
    assert template.model_id == MODEL_ID
    # Decoding enforces finiteness and normalisation, which is what the recognition path relies
    # on; a template that fails here would be dropped as unusable at comparison time.
    vector = decode(template.data, dimensions=template.dimensions, dtype=template.dtype)
    assert math.isclose(math.sqrt(sum(value**2 for value in vector.values)), 1.0, abs_tol=1e-3)


@requires_footage
def test_extraction_is_deterministic(backend: OpenCvEvalFaceBackend) -> None:
    """The same bytes must give the same template every time, or a revision hash means nothing
    and an enrolled person's score would drift between runs."""
    image = _portrait(3.0, 1.9)
    assert backend.extract_template(image).data == backend.extract_template(image).data


@requires_footage
def test_a_template_never_shows_its_contents_in_a_repr(
    backend: OpenCvEvalFaceBackend,
) -> None:
    assert repr(backend.extract_template(_portrait(3.0, 1.9))) == "FaceTemplate(REDACTED)"


@requires_footage
def test_two_crops_of_one_person_are_far_more_similar_than_a_crop_and_noise(
    backend: OpenCvEvalFaceBackend,
) -> None:
    """The property the whole pipeline exists for, on real pixels: alignment makes two
    differently-framed photographs of one face comparable.

    This is a sanity check on the plumbing, not a calibration: one identity from recorded
    footage says nothing about the impostor distribution, and the configured threshold is an
    evaluation value that only a supervised test with several consenting people can inform.
    """
    first = backend.extract_query(_portrait(3.0, 1.9)).vector
    second = backend.extract_query(_portrait(2.6, 2.1)).vector
    assert similarity(first, second) > 0.6

    noise = normalize([1.0] + [0.0] * (DIMENSIONS - 1))
    assert similarity(first, second) > similarity(first, noise)


# ------------------------------------------------------------------------------ input bounds
def test_an_image_larger_than_the_canonical_bound_is_refused(
    backend: OpenCvEvalFaceBackend,
) -> None:
    """Enrollment images are canonicalised to at most 1600 on the long side before they reach a
    backend, so anything larger did not come through the validator."""
    with pytest.raises(FaceBackendError) as raised:
        backend.analyze(blank((2000, 1200)))
    assert raised.value.category == "invalid_image"


def test_a_greyscale_image_is_handled_rather_than_crashing(
    backend: OpenCvEvalFaceBackend,
) -> None:
    grey = Image.new("L", (480, 480), 120)
    assert backend.analyze(grey).face_count == 0


def test_the_backend_reads_a_canonical_jpeg_the_validator_would_produce(
    backend: OpenCvEvalFaceBackend,
) -> None:
    buffer = io.BytesIO()
    blank().save(buffer, format="JPEG", quality=90)
    with Image.open(io.BytesIO(buffer.getvalue())) as decoded:
        assert backend.analyze(decoded).face_count == 0
