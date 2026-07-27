"""Expose the existing in-memory segment index through application ports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from src.application.dto import BufferSnapshot, CaptureStatus
from src.domain.capture import (
    BufferHealth,
    CameraId,
    CameraState,
    CaptureSegment,
)


class SegmentBufferLike(Protocol):
    def start(self) -> None: ...

    def stop(self, join_timeout: float = 2.0) -> None: ...

    def snapshot_last(self, n: int) -> list[str]: ...

    def diagnostics(self, *, stale_after_sec: float) -> object: ...


class SegmentBufferRepository:
    """Adapt one legacy ``SegmentBuffer`` without leaking it into application.

    File mtimes are interpreted as segment completion times, matching the
    legacy buffer freshness calculation. The segment start is therefore one
    configured segment duration before the mtime.
    """

    def __init__(
        self,
        *,
        camera_id: CameraId,
        buffer: SegmentBufferLike,
        segment_duration_seconds: float,
        maximum_segments: int,
        stale_after_seconds: float,
        camera_state: Callable[[], CameraState],
        join_timeout_seconds: float = 2,
    ) -> None:
        if segment_duration_seconds <= 0:
            raise ValueError("segment_duration_seconds must be positive")
        if maximum_segments < 1:
            raise ValueError("maximum_segments must be positive")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if join_timeout_seconds <= 0:
            raise ValueError("join_timeout_seconds must be positive")
        self._camera_id = camera_id
        self._buffer = buffer
        self._segment_duration = segment_duration_seconds
        self._maximum_segments = maximum_segments
        self._stale_after = stale_after_seconds
        self._camera_state = camera_state
        self._join_timeout = join_timeout_seconds

    def start(self) -> None:
        """Start the underlying index when registered as a lifecycle component."""

        self._buffer.start()

    def shutdown(self) -> None:
        self._buffer.stop(join_timeout=self._join_timeout)

    def list_between(
        self,
        camera_id: CameraId,
        start: datetime,
        end: datetime,
    ) -> tuple[CaptureSegment, ...]:
        self._require_camera(camera_id)
        if end < start:
            raise ValueError("segment query end must not precede start")

        segments: list[CaptureSegment] = []
        for location in self._buffer.snapshot_last(self._maximum_segments):
            path = Path(location)
            try:
                completed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except (FileNotFoundError, OSError):
                continue
            started_at = completed_at - timedelta(seconds=self._segment_duration)
            if started_at <= end and completed_at >= start:
                segments.append(
                    CaptureSegment(
                        segment_id=path.name,
                        camera_id=self._camera_id,
                        location=str(path),
                        started_at=started_at,
                        duration_seconds=self._segment_duration,
                    )
                )
        return tuple(sorted(segments, key=lambda item: (item.started_at, item.segment_id)))

    def status(self, camera_id: CameraId) -> CaptureStatus:
        self._require_camera(camera_id)
        diagnostics = self._buffer.diagnostics(stale_after_sec=self._stale_after)
        health = self._health(str(getattr(diagnostics, "buffer_status", "UNKNOWN")))
        newest = self._parse_datetime(getattr(diagnostics, "last_segment_at", None))
        return CaptureStatus(
            camera_id=self._camera_id,
            state=self._camera_state(),
            buffer=BufferSnapshot(
                camera_id=self._camera_id,
                health=health,
                segment_count=max(0, int(getattr(diagnostics, "segment_count", 0))),
                newest_segment_at=newest,
            ),
        )

    def _require_camera(self, camera_id: CameraId) -> None:
        if camera_id != self._camera_id:
            raise ValueError(
                f"repository for {self._camera_id.value} cannot serve {camera_id.value}"
            )

    @staticmethod
    def _health(value: str) -> BufferHealth:
        try:
            return BufferHealth(value.upper())
        except ValueError:
            return BufferHealth.UNKNOWN

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

