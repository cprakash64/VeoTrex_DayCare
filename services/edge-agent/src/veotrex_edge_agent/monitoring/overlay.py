"""Detection overlay drawing.

Restrained on purpose: a thin box per confirmed track, a compact identifier pill, and the
track's own recent path. Every point on a trail is a position the tracker actually reported -
nothing is interpolated or smoothed beyond drawing straight segments between real
observations, and no identity is ever shown.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont

from veotrex_edge_agent.tracking import TrackView

BOX_COLOR = (109, 224, 178)
LABEL_TEXT = (6, 26, 21)
BASE_BOX_WIDTH = 2
BASE_TRAIL_WIDTH = 3
BASE_MARKER_RADIUS = 3
# Oldest segment colour. The trail is drawn from this toward BOX_COLOR so the newest part is
# the brightest; interpolating the colour avoids an RGBA composite pass on every frame.
TRAIL_FADE = (18, 62, 50)

Point = tuple[float, float]


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _mix(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, ...]:
    return tuple(int(a + (b - a) * ratio) for a, b in zip(start, end, strict=True))


def _draw_trail(draw: ImageDraw.ImageDraw, points: Sequence[Point], width: int) -> None:
    if len(points) < 2:
        return
    segments = len(points) - 1
    for index in range(segments):
        # Ratio runs 0 at the oldest segment to 1 at the newest.
        colour = _mix(TRAIL_FADE, BOX_COLOR, (index + 1) / segments)
        draw.line((points[index], points[index + 1]), fill=colour, width=width, joint="curve")


def annotate(
    rgb: NDArray[np.uint8],
    tracks: tuple[TrackView, ...],
    *,
    trails: Mapping[int, Sequence[Point]] | None = None,
    jpeg_quality: int = 85,
) -> bytes:
    """Draw confirmed tracks and their real paths onto a copy of the frame."""
    image = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    label_font = _font(max(13, min(24, height // 34)))
    scale = max(1, height // 540)
    box_width = BASE_BOX_WIDTH * scale
    trail_width = BASE_TRAIL_WIDTH * scale
    marker = BASE_MARKER_RADIUS * scale

    # Trails first so boxes and labels stay legible on top of them.
    for track in tracks:
        path = (trails or {}).get(track.track_id)
        if path:
            _draw_trail(draw, path, trail_width)
            latest = path[-1]
            draw.ellipse(
                (
                    latest[0] - marker,
                    latest[1] - marker,
                    latest[0] + marker,
                    latest[1] + marker,
                ),
                fill=BOX_COLOR,
            )

    for track in tracks:
        x1, y1, x2, y2 = track.bbox_xyxy_source
        left = max(0.0, min(float(x1), width - 1.0))
        top = max(0.0, min(float(y1), height - 1.0))
        right = max(left + 1.0, min(float(x2), float(width)))
        bottom = max(top + 1.0, min(float(y2), float(height)))
        draw.rectangle((left, top, right, bottom), outline=BOX_COLOR, width=box_width)

        # Dwell time is the track's real age, not an identity and not a recognition claim.
        label = f"ID {track.track_id}"
        if track.age_seconds >= 1:
            label = f"{label} | {int(track.age_seconds)}s"
        text_box = draw.textbbox((0, 0), label, font=label_font)
        pad_x, pad_y = 7, 4
        badge_width = text_box[2] - text_box[0] + pad_x * 2
        badge_height = text_box[3] - text_box[1] + pad_y * 2
        badge_top = max(0.0, top - badge_height)
        badge_left = min(left, max(0.0, width - badge_width))
        draw.rounded_rectangle(
            (badge_left, badge_top, badge_left + badge_width, badge_top + badge_height),
            radius=max(3, badge_height // 4),
            fill=BOX_COLOR,
        )
        draw.text(
            (badge_left + pad_x - text_box[0], badge_top + pad_y - text_box[1]),
            label,
            fill=LABEL_TEXT,
            font=label_font,
        )

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=jpeg_quality)
    return buffer.getvalue()
