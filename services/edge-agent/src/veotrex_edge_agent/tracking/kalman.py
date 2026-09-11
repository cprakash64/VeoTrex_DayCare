"""ByteTrack-compatible bounding-box Kalman filter.

Derived from FoundationVision/ByteTrack at d1bf0191adff59bc8fcfeaa0b33d3d1642552a99,
Copyright (c) 2021 Yifu Zhang, used under the MIT License. Production adaptations
use float64, explicit elapsed time, and defensive numerical validation.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


class KalmanStateError(ValueError):
    pass


class BoxKalmanFilter:
    dimension = 4

    def initiate(
        self, measurement: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        self._validate_measurement(measurement)
        mean = np.r_[measurement, np.zeros_like(measurement)]
        h = measurement[3]
        deviations = np.array(
            [
                2 * h / 20,
                2 * h / 20,
                1e-2,
                2 * h / 20,
                10 * h / 160,
                10 * h / 160,
                1e-5,
                10 * h / 160,
            ],
            dtype=np.float64,
        )
        return mean, np.diag(deviations * deviations)

    def predict(
        self, mean: NDArray[np.float64], covariance: NDArray[np.float64], elapsed: float
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        self._validate_state(mean, covariance)
        if not np.isfinite(elapsed) or elapsed < 0:
            raise KalmanStateError("invalid_elapsed_time")
        motion = np.eye(8, dtype=np.float64)
        for index in range(4):
            motion[index, index + 4] = elapsed
        h = max(mean[3], 1e-6)
        deviations = np.array(
            [h / 20, h / 20, 1e-2, h / 20, h / 160, h / 160, 1e-5, h / 160],
            dtype=np.float64,
        )
        result_mean = motion @ mean
        result_covariance = motion @ covariance @ motion.T + np.diag(deviations * deviations)
        self._validate_state(result_mean, result_covariance)
        return result_mean, result_covariance

    def update(
        self,
        mean: NDArray[np.float64],
        covariance: NDArray[np.float64],
        measurement: NDArray[np.float64],
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        self._validate_state(mean, covariance)
        self._validate_measurement(measurement)
        projected_mean = mean[:4]
        h = max(mean[3], 1e-6)
        deviations = np.array([h / 20, h / 20, 1e-1, h / 20], dtype=np.float64)
        projected_covariance = covariance[:4, :4] + np.diag(deviations * deviations)
        try:
            gain = np.linalg.solve(projected_covariance, covariance[:4, :]).T
        except np.linalg.LinAlgError as exc:
            raise KalmanStateError("singular_covariance") from exc
        innovation = measurement - projected_mean
        result_mean = mean + gain @ innovation
        result_covariance = covariance - gain @ projected_covariance @ gain.T
        result_covariance = (result_covariance + result_covariance.T) / 2
        self._validate_state(result_mean, result_covariance)
        return result_mean, result_covariance

    @staticmethod
    def _validate_measurement(measurement: NDArray[np.float64]) -> None:
        if measurement.shape != (4,) or not np.all(np.isfinite(measurement)):
            raise KalmanStateError("invalid_measurement")
        if measurement[2] <= 0 or measurement[3] <= 0:
            raise KalmanStateError("invalid_box_geometry")

    @staticmethod
    def _validate_state(mean: NDArray[np.float64], covariance: NDArray[np.float64]) -> None:
        if mean.shape != (8,) or covariance.shape != (8, 8):
            raise KalmanStateError("invalid_state_shape")
        if not np.all(np.isfinite(mean)) or not np.all(np.isfinite(covariance)):
            raise KalmanStateError("non_finite_state")
        if mean[2] <= 0 or mean[3] <= 0:
            raise KalmanStateError("invalid_state_geometry")


def xyxy_to_xyah(box: tuple[float, float, float, float]) -> NDArray[np.float64]:
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    if width <= 0 or height <= 0:
        raise KalmanStateError("invalid_box_geometry")
    value = np.array([(x1 + x2) / 2, (y1 + y2) / 2, width / height, height])
    BoxKalmanFilter._validate_measurement(value)
    return value


def xyah_to_xyxy(mean: NDArray[np.float64]) -> tuple[float, float, float, float]:
    width = mean[2] * mean[3]
    return (mean[0] - width / 2, mean[1] - mean[3] / 2, mean[0] + width / 2, mean[1] + mean[3] / 2)
