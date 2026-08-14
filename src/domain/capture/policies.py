"""Pure policies used to decide whether captured media can produce a replay."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .models import BufferHealth, CameraId, CameraState, CaptureSegment


class ReadinessFailure(str, Enum):
    CAMERA_NOT_READY = "camera_not_ready"
    BUFFER_NOT_FRESH = "buffer_not_fresh"
    INSUFFICIENT_SEGMENTS = "insufficient_segments"


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    accepted: bool
    failure: ReadinessFailure | None = None


def decide_readiness(
    *,
    camera_state: CameraState,
    buffer_health: BufferHealth,
    segment_count: int,
    minimum_segments: int = 2,
) -> ReadinessDecision:
    if camera_state is not CameraState.OK:
        return ReadinessDecision(False, ReadinessFailure.CAMERA_NOT_READY)
    if buffer_health is not BufferHealth.FRESH:
        return ReadinessDecision(False, ReadinessFailure.BUFFER_NOT_FRESH)
    if segment_count < minimum_segments:
        return ReadinessDecision(False, ReadinessFailure.INSUFFICIENT_SEGMENTS)
    return ReadinessDecision(True)


def select_segments(
    segments: tuple[CaptureSegment, ...] | list[CaptureSegment],
    *,
    camera_id: CameraId,
    start: datetime,
    end: datetime,
) -> tuple[CaptureSegment, ...]:
    """Return unique, chronological segments that overlap the requested window."""

    selected: dict[str, CaptureSegment] = {}
    for segment in segments:
        segment_end = segment.started_at.timestamp() + segment.duration_seconds
        if (
            segment.camera_id == camera_id
            and segment.started_at.timestamp() <= end.timestamp()
            and segment_end >= start.timestamp()
        ):
            selected.setdefault(segment.segment_id, segment)
    return tuple(sorted(selected.values(), key=lambda item: (item.started_at, item.segment_id)))
