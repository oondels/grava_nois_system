"""Application orchestration for producing one replay artifact."""

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from src.application.ports import MediaTool, SegmentRepository
from src.domain.capture import ReadinessFailure, decide_readiness, select_segments
from src.domain.replay import ReplayRequest


class CaptureReplayFailure(str, Enum):
    CAMERA_NOT_READY = ReadinessFailure.CAMERA_NOT_READY.value
    BUFFER_NOT_FRESH = ReadinessFailure.BUFFER_NOT_FRESH.value
    INSUFFICIENT_SEGMENTS = ReadinessFailure.INSUFFICIENT_SEGMENTS.value


@dataclass(frozen=True, slots=True)
class CaptureReplayResult:
    artifact_location: str | None
    selected_segment_ids: tuple[str, ...] = ()
    failure: CaptureReplayFailure | None = None

    @property
    def succeeded(self) -> bool:
        return self.artifact_location is not None


class CaptureReplay:
    """Select captured segments and concatenate them through a media port.

    The caller schedules this use case after the post-trigger window has elapsed.
    Sleeping and executor ownership intentionally remain in the bootstrap layer.
    """

    def __init__(
        self,
        *,
        segments: SegmentRepository,
        media: MediaTool,
        minimum_segments: int = 2,
    ) -> None:
        if minimum_segments < 1:
            raise ValueError("minimum_segments must be positive")
        self._segments = segments
        self._media = media
        self._minimum_segments = minimum_segments

    def execute(self, request: ReplayRequest, *, output_location: str) -> CaptureReplayResult:
        if not output_location.strip():
            raise ValueError("output_location must not be empty")

        status = self._segments.status(request.camera_id)
        readiness = decide_readiness(
            camera_state=status.state,
            buffer_health=status.buffer.health,
            segment_count=status.buffer.segment_count,
            minimum_segments=self._minimum_segments,
        )
        if not readiness.accepted:
            assert readiness.failure is not None
            return CaptureReplayResult(
                None,
                failure=CaptureReplayFailure(readiness.failure.value),
            )

        start = request.triggered_at - timedelta(seconds=request.window.pre_seconds)
        end = request.triggered_at + timedelta(seconds=request.window.post_seconds)
        candidates = self._segments.list_between(request.camera_id, start, end)
        selected = select_segments(
            list(candidates),
            camera_id=request.camera_id,
            start=start,
            end=end,
        )
        ids = tuple(segment.segment_id for segment in selected)
        if len(selected) < self._minimum_segments:
            return CaptureReplayResult(
                None,
                selected_segment_ids=ids,
                failure=CaptureReplayFailure.INSUFFICIENT_SEGMENTS,
            )

        self._media.concatenate(tuple(segment.location for segment in selected), output_location)
        return CaptureReplayResult(output_location, selected_segment_ids=ids)
