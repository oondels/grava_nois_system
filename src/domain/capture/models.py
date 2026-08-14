"""Business vocabulary for continuous camera capture."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from src.domain.exceptions import InvariantViolation


@dataclass(frozen=True, slots=True)
class CameraId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise InvariantViolation("camera id must not be empty")


class CameraState(str, Enum):
    STARTING = "STARTING"
    OK = "OK"
    RECONNECTING = "RECONNECTING"
    UNAVAILABLE = "UNAVAILABLE"
    STOPPED = "STOPPED"


class BufferHealth(str, Enum):
    EMPTY = "EMPTY"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
    FRESH = "FRESH"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class CaptureSegment:
    """A captured media segment addressable through a SegmentRepository."""

    segment_id: str
    camera_id: CameraId
    location: str
    started_at: datetime
    duration_seconds: float

    def __post_init__(self) -> None:
        if not self.segment_id.strip():
            raise InvariantViolation("segment id must not be empty")
        if not self.location.strip():
            raise InvariantViolation("segment location must not be empty")
        if self.duration_seconds <= 0:
            raise InvariantViolation("segment duration must be positive")
