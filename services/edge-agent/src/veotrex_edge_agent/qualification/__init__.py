"""Ring media qualification primitives.

This package is deliberately isolated from the production edge-agent lifecycle. Importing it does
not import PyGObject/GStreamer or open a camera connection.
"""

from veotrex_edge_agent.qualification.models import (
    CameraTarget,
    QualificationMode,
    SessionClass,
)

__all__ = ["CameraTarget", "QualificationMode", "SessionClass"]
