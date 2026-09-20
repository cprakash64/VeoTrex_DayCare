"""Detection overlay drawing.

Restrained on purpose: a thin box per confirmed track and a small identifier, nothing else.
Debug metadata belongs in the state payload, not burned into imagery a customer is watching.
"""

from __future__ import annotations

import io

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

from veotrex_edge_agent.tracking import TrackView

BOX_COLOR = (109, 224, 178)
LABEL_TEXT = (6, 26, 21)
BOX_WIDTH = 2


def annotate(
    rgb: NDArray[np.uint8],
    tracks: tuple[TrackView, ...],
    *,
    jpeg_quality: int = 85,
) -> bytes:
    """Draw confirmed tracks onto a copy of the frame and return JPEG bytes."""
    image = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    for track in tracks:
        x1, y1, x2, y2 = track.bbox_xyxy_source
        left = max(0.0, min(float(x1), width - 1.0))
        top = max(0.0, min(float(y1), height - 1.0))
        right = max(left + 1.0, min(float(x2), float(width)))
        bottom = max(top + 1.0, min(float(y2), float(height)))
        draw.rectangle((left, top, right, bottom), outline=BOX_COLOR, width=BOX_WIDTH)
        label = f"#{track.track_id}"
        text_box = draw.textbbox((0, 0), label)
        text_width = text_box[2] - text_box[0] + 8
        text_height = text_box[3] - text_box[1] + 6
        badge_top = max(0.0, top - text_height)
        draw.rectangle(
            (left, badge_top, left + text_width, badge_top + text_height), fill=BOX_COLOR
        )
        draw.text((left + 4, badge_top + 3), label, fill=LABEL_TEXT)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=jpeg_quality)
    return buffer.getvalue()
