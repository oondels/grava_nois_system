"""Business vocabulary for replay requests."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from src.domain.capture import CameraId
from src.domain.exceptions import InvariantViolation


class TriggerSource(str, Enum):
    KEYBOARD = "keyboard"
    GPIO = "gpio"
    PICO = "pico"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class ReplayWindow:
    pre_seconds: float
    post_seconds: float

    def __post_init__(self) -> None:
        if self.pre_seconds < 0 or self.post_seconds < 0:
            raise InvariantViolation("replay window values must not be negative")
        if self.pre_seconds + self.post_seconds <= 0:
            raise InvariantViolation("replay window must have a positive duration")


@dataclass(frozen=True, slots=True)
class ReplayRequest:
    request_id: str
    camera_id: CameraId
    triggered_at: datetime
    source: TriggerSource
    window: ReplayWindow

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise InvariantViolation("replay request id must not be empty")
