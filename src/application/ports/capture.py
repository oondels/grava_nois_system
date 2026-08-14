"""Capture-related outbound ports."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from src.application.dto import CaptureStatus
from src.domain.capture import CameraId, CaptureSegment


class CaptureProcess(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def is_alive(self) -> bool: ...


class SegmentRepository(Protocol):
    def list_between(
        self, camera_id: CameraId, start: datetime, end: datetime
    ) -> Sequence[CaptureSegment]: ...

    def status(self, camera_id: CameraId) -> CaptureStatus: ...
