"""Trigger dispatch orchestration without concrete executor dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from src.application.ports import TaskScheduler
from src.domain.capture import CameraId
from src.domain.replay import CameraRoute, CooldownPolicy, TriggerRouter


@dataclass(frozen=True, slots=True)
class TriggerDispatchResult:
    scheduled: tuple[CameraId, ...]
    cooling_down: tuple[CameraId, ...]


class EdgeTriggerCoordinator:
    def __init__(
        self,
        *,
        router: TriggerRouter,
        scheduler: TaskScheduler,
        cooldown: CooldownPolicy,
    ) -> None:
        self._router = router
        self._scheduler = scheduler
        self._cooldown = cooldown
        self._last_triggered_at: dict[CameraId, datetime] = {}

    def dispatch(
        self,
        *,
        token: str,
        cameras: tuple[CameraRoute, ...] | list[CameraRoute],
        triggered_at: datetime,
        handler: Callable[[CameraId], None],
        skip_cooldown: bool = False,
    ) -> TriggerDispatchResult:
        scheduled: list[CameraId] = []
        cooling_down: list[CameraId] = []
        for camera_id in self._router.route(token, cameras):
            if not skip_cooldown and not self._cooldown.allows(
                last_triggered_at=self._last_triggered_at.get(camera_id),
                triggered_at=triggered_at,
            ):
                cooling_down.append(camera_id)
                continue
            if not skip_cooldown:
                self._last_triggered_at[camera_id] = triggered_at
            self._scheduler.submit(self._isolated(handler, camera_id))
            scheduled.append(camera_id)
        return TriggerDispatchResult(tuple(scheduled), tuple(cooling_down))

    @staticmethod
    def _isolated(handler: Callable[[CameraId], None], camera_id: CameraId) -> Callable[[], None]:
        def run() -> None:
            try:
                handler(camera_id)
            except Exception:
                # Failure reporting is owned by the invoked use case/event adapter.
                # A handler must never cancel sibling camera work.
                return

        return run
