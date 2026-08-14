"""Transport-neutral inputs and outputs of application use cases."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from src.domain.capture import BufferHealth, CameraId, CameraState
from src.domain.delivery import ClipJobState


@dataclass(frozen=True, slots=True)
class BufferSnapshot:
    camera_id: CameraId
    health: BufferHealth
    segment_count: int
    newest_segment_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    camera_id: CameraId
    state: CameraState
    buffer: BufferSnapshot


@dataclass(frozen=True, slots=True)
class ClipJobSnapshot:
    job_id: str
    state: ClipJobState
    attempts: int
    artifact_location: str


@dataclass(frozen=True, slots=True)
class RemoteClipRegistration:
    clip_id: str
    upload_url: str
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    status_code: int
    response_headers: Mapping[str, str]
    size_bytes: int
    sha256: str
    etag: str | None = None
