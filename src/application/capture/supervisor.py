"""Deterministic camera supervision independent from threads and real time."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from src.application.ports import CaptureProcess, Clock, SegmentRepository
from src.domain.capture import BufferHealth, CameraId


class SupervisionAction(str, Enum):
    HEALTHY = "healthy"
    MONITORING_STALE = "monitoring_stale"
    WAITING_BACKOFF = "waiting_backoff"
    STARTED = "started"
    START_FAILED = "start_failed"


@dataclass(frozen=True, slots=True)
class SupervisionResult:
    camera_id: CameraId
    action: SupervisionAction
    restart_attempts: int
    retry_delay_seconds: float
    stale_cycles: int = 0
    error: str | None = None


class CameraSupervisor:
    """Advance supervision by one poll cycle.

    A bootstrap scheduler owns polling. This object owns only per-camera policy
    state, so separate instances guarantee that one camera failure cannot poison
    another camera's backoff or stale counters.
    """

    _STALE_HEALTH = frozenset({BufferHealth.STALE, BufferHealth.MISSING, BufferHealth.UNKNOWN})

    def __init__(
        self,
        *,
        camera_id: CameraId,
        process: CaptureProcess,
        segments: SegmentRepository,
        clock: Clock,
        initial_backoff_seconds: float = 5,
        maximum_backoff_seconds: float = 300,
        stale_restart_after_seconds: float = 30,
        stale_restart_cycles: int = 3,
    ) -> None:
        if (
            initial_backoff_seconds <= 0
            or maximum_backoff_seconds < initial_backoff_seconds
            or stale_restart_after_seconds <= 0
            or stale_restart_cycles < 1
        ):
            raise ValueError("invalid camera supervision policy")
        self._camera_id = camera_id
        self._process = process
        self._segments = segments
        self._clock = clock
        self._initial_backoff = initial_backoff_seconds
        self._maximum_backoff = maximum_backoff_seconds
        self._stale_after = stale_restart_after_seconds
        self._stale_limit = stale_restart_cycles
        self._retry_delay = initial_backoff_seconds
        self._next_attempt_at: datetime | None = None
        self._stale_since: datetime | None = None
        self._stale_cycles = 0
        self._ever_started = False
        self._restart_attempts = 0

    def tick(self) -> SupervisionResult:
        now = self._clock.now()
        if self._process.is_alive():
            status = self._segments.status(self._camera_id)
            if status.buffer.health in self._STALE_HEALTH:
                return self._handle_stale(now)
            self._reset_healthy_state()
            return self._result(SupervisionAction.HEALTHY)

        self._reset_stale_state()
        if self._next_attempt_at is None and self._ever_started:
            self._next_attempt_at = now + timedelta(seconds=self._retry_delay)
            return self._result(SupervisionAction.WAITING_BACKOFF)
        if self._next_attempt_at is not None and now < self._next_attempt_at:
            return self._result(SupervisionAction.WAITING_BACKOFF)
        return self._start()

    def _handle_stale(self, now: datetime) -> SupervisionResult:
        if self._stale_since is None:
            self._stale_since = now
            self._stale_cycles = 1
        else:
            self._stale_cycles += 1
        elapsed = (now - self._stale_since).total_seconds()
        if elapsed < self._stale_after and self._stale_cycles < self._stale_limit:
            self._retry_delay = self._initial_backoff
            self._next_attempt_at = None
            return self._result(SupervisionAction.MONITORING_STALE)

        self._process.stop()
        self._reset_stale_state()
        self._next_attempt_at = None
        return self._start()

    def _start(self) -> SupervisionResult:
        self._restart_attempts += 1
        try:
            self._process.start()
        except Exception as exc:
            delay = self._retry_delay
            self._next_attempt_at = self._clock.now() + timedelta(seconds=delay)
            self._retry_delay = min(delay * 2, self._maximum_backoff)
            return self._result(
                SupervisionAction.START_FAILED,
                retry_delay=delay,
                error=str(exc),
            )
        self._ever_started = True
        self._retry_delay = self._initial_backoff
        self._next_attempt_at = None
        return self._result(SupervisionAction.STARTED)

    def _reset_stale_state(self) -> None:
        self._stale_since = None
        self._stale_cycles = 0

    def _reset_healthy_state(self) -> None:
        self._reset_stale_state()
        self._retry_delay = self._initial_backoff
        self._next_attempt_at = None

    def _result(
        self,
        action: SupervisionAction,
        *,
        retry_delay: float | None = None,
        error: str | None = None,
    ) -> SupervisionResult:
        return SupervisionResult(
            camera_id=self._camera_id,
            action=action,
            restart_attempts=self._restart_attempts,
            retry_delay_seconds=self._retry_delay if retry_delay is None else retry_delay,
            stale_cycles=self._stale_cycles,
            error=error,
        )


class CameraSupervisionCoordinator:
    """Tick independent camera supervisors with failure isolation."""

    def __init__(self, supervisors: tuple[CameraSupervisor, ...]) -> None:
        self._supervisors = supervisors

    def tick_all(self) -> tuple[SupervisionResult, ...]:
        results: list[SupervisionResult] = []
        for supervisor in self._supervisors:
            try:
                results.append(supervisor.tick())
            except Exception:
                continue
        return tuple(results)
