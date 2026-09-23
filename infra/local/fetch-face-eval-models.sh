#!/usr/bin/env bash
# Fetch the audited face-recognition weights for LOCAL qualification (V1-02B0).
#
# The application never downloads a model: not at startup, not on a request, not ever. This
# script is the only fetch path, it is run once by the operator, and it is deterministic - a
# pinned upstream tag, a fixed file name, and a SHA-256 that must match the digest recorded in
# apps/api/src/veotrex_api/face_models.py. A file that does not match is deleted, not kept.
#
#   ./infra/local/fetch-face-eval-models.sh [destination]
#
# Default destination: artifacts/models/face (git-ignored). Files are written 0600 into a 0700
# directory: SFace weights are evaluation-only material, not something to leave world-readable.
#
# LOCAL / DEVELOPMENT / TEST ONLY. Do not place these weights on the Hostinger control plane;
# the opencv_eval backend refuses to start there in any case.

set -euo pipefail

DESTINATION="${1:-artifacts/models/face}"

# Pinned opencv_zoo release tag. Weights are git-lfs objects, so they are fetched through the
# media host; a moving branch could publish a different object under the same path, which is
# exactly what pinning and the digest check together prevent.
REVISION="4.10.0"
BASE="https://media.githubusercontent.com/media/opencv/opencv_zoo/${REVISION}/models"

# directory|file|sha256|bytes  - the digests are the git-lfs pointers published at ${REVISION}
# and are duplicated in veotrex_api.face_models; a test asserts the two agree.
ARTIFACTS=(
  "face_detection_yunet|face_detection_yunet_2023mar.onnx|8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4|232589"
  "face_recognition_sface|face_recognition_sface_2021dec.onnx|0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79|38696353"
)

mkdir -p "${DESTINATION}"
chmod 700 "${DESTINATION}"

for entry in "${ARTIFACTS[@]}"; do
  IFS='|' read -r directory file expected_sha expected_bytes <<<"${entry}"
  target="${DESTINATION}/${file}"

  if [ -e "${target}" ]; then
    actual_sha="$(sha256sum "${target}" | cut -d' ' -f1)"
    if [ "${actual_sha}" = "${expected_sha}" ]; then
      echo "ok (cached)   ${file}"
      continue
    fi
    echo "stale         ${file}: digest mismatch, refetching" >&2
    rm -f "${target}"
  fi

  echo "fetching      ${file}"
  temporary="${target}.partial"
  rm -f "${temporary}"
  # Written to a temporary name first, so an interrupted download can never be mistaken for a
  # verified model; only a file that passes both checks is given the real name.
  curl --fail --location --silent --show-error --output "${temporary}" \
    "${BASE}/${directory}/${file}"
  chmod 600 "${temporary}"

  actual_bytes="$(wc -c <"${temporary}" | tr -d ' ')"
  actual_sha="$(sha256sum "${temporary}" | cut -d' ' -f1)"
  if [ "${actual_bytes}" != "${expected_bytes}" ] || [ "${actual_sha}" != "${expected_sha}" ]; then
    rm -f "${temporary}"
    echo "REFUSED       ${file}: expected ${expected_bytes} bytes / ${expected_sha}" >&2
    echo "              got      ${actual_bytes} bytes / ${actual_sha}" >&2
    exit 1
  fi
  mv "${temporary}" "${target}"
  echo "verified      ${file}"
done

echo
echo "Models are in ${DESTINATION}."
echo "Enable the evaluation backend for a LOCAL run only:"
echo "  export VEOTREX_ENVIRONMENT=local"
echo "  export VEOTREX_STAFF_FACE_BACKEND=opencv_eval"
echo "  export VEOTREX_STAFF_FACE_MODEL_DIR=\"\$(cd "${DESTINATION}" && pwd)\""
