"""Capture domain types."""

from .models import BufferHealth, CameraId, CameraState, CaptureSegment
from .policies import (
    ReadinessDecision,
    ReadinessFailure,
    decide_readiness,
    select_segments,
)

__all__ = [
    "BufferHealth",
    "CameraId",
    "CameraState",
    "CaptureSegment",
    "ReadinessDecision",
    "ReadinessFailure",
    "decide_readiness",
    "select_segments",
]
