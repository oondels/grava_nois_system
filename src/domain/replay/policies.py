"""Pure replay-window, cooldown and trigger-routing policies."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.domain.capture import CameraId
from src.domain.exceptions import InvariantViolation

from .models import ReplayWindow


def replay_window_from_segments(
    *, pre_segments: int, post_segments: int, segment_seconds: float
) -> ReplayWindow:
    if pre_segments < 1 or post_segments < 1 or segment_seconds <= 0:
        raise InvariantViolation("replay segment policy values must be positive")
    return ReplayWindow(
        pre_seconds=pre_segments * segment_seconds,
        post_seconds=post_segments * segment_seconds,
    )


@dataclass(frozen=True, slots=True)
class CooldownPolicy:
    seconds: float

    def __post_init__(self) -> None:
        if self.seconds < 0:
            raise InvariantViolation("cooldown must not be negative")

    def allows(self, *, last_triggered_at: datetime | None, triggered_at: datetime) -> bool:
        if last_triggered_at is None:
            return True
        return triggered_at >= last_triggered_at + timedelta(seconds=self.seconds)


@dataclass(frozen=True, slots=True)
class CameraRoute:
    camera_id: CameraId
    dedicated_token: str | None = None
    enabled: bool = True

    def normalized_token(self) -> str | None:
        token = (self.dedicated_token or "").strip().upper()
        return token or None


class TriggerRouter:
    """Resolve Pico tokens without depending on serial, threads or camera runtimes."""

    def __init__(self, *, global_token: str) -> None:
        normalized = global_token.strip().upper()
        if not normalized:
            raise InvariantViolation("global trigger token must not be empty")
        self._global_token = normalized

    def route(
        self, token: str, cameras: tuple[CameraRoute, ...] | list[CameraRoute]
    ) -> tuple[CameraId, ...]:
        normalized = token.strip().upper()
        enabled = tuple(camera for camera in cameras if camera.enabled)
        dedicated = tuple(
            camera.camera_id for camera in enabled if camera.normalized_token() == normalized
        )
        if dedicated:
            return dedicated
        if normalized != self._global_token:
            return ()
        global_targets = tuple(
            camera.camera_id for camera in enabled if camera.normalized_token() is None
        )
        return global_targets or tuple(camera.camera_id for camera in enabled)
