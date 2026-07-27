import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from src.application.capture import (
    CameraSupervisionCoordinator,
    CameraSupervisor,
    SupervisionAction,
)
from src.application.dto import BufferSnapshot, CaptureStatus
from src.application.replay import EdgeTriggerCoordinator
from src.domain.capture import BufferHealth, CameraId, CameraState
from src.domain.replay import CameraRoute, CooldownPolicy, TriggerRouter
from src.infrastructure.observability import ExecutorTaskScheduler

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeProcess:
    def __init__(self, *, alive: bool = False, failures: int = 0) -> None:
        self.alive = alive
        self.failures = failures
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("camera offline")
        self.alive = True

    def stop(self) -> None:
        self.stops += 1
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


def _status(camera: CameraId, health: BufferHealth) -> CaptureStatus:
    return CaptureStatus(
        camera,
        CameraState.OK,
        BufferSnapshot(camera, health, 5, NOW),
    )


class CameraSupervisorTests(unittest.TestCase):
    def test_initial_start_is_immediate_and_healthy_resets_state(self) -> None:
        camera = CameraId("one")
        clock = FakeClock()
        process = FakeProcess()
        segments = Mock()
        segments.status.return_value = _status(camera, BufferHealth.FRESH)
        supervisor = CameraSupervisor(
            camera_id=camera, process=process, segments=segments, clock=clock
        )

        started = supervisor.tick()
        healthy = supervisor.tick()

        self.assertEqual(SupervisionAction.STARTED, started.action)
        self.assertEqual(SupervisionAction.HEALTHY, healthy.action)
        self.assertEqual(1, process.starts)
        self.assertEqual(5, healthy.retry_delay_seconds)

    def test_dead_established_process_waits_five_seconds_before_restart(self) -> None:
        camera = CameraId("one")
        clock = FakeClock()
        process = FakeProcess()
        segments = Mock()
        segments.status.return_value = _status(camera, BufferHealth.FRESH)
        supervisor = CameraSupervisor(
            camera_id=camera, process=process, segments=segments, clock=clock
        )
        supervisor.tick()
        process.alive = False

        self.assertEqual(SupervisionAction.WAITING_BACKOFF, supervisor.tick().action)
        clock.advance(4.9)
        self.assertEqual(SupervisionAction.WAITING_BACKOFF, supervisor.tick().action)
        clock.advance(0.1)
        self.assertEqual(SupervisionAction.STARTED, supervisor.tick().action)

    def test_failed_starts_exponentially_back_off_and_cap_at_300(self) -> None:
        camera = CameraId("one")
        clock = FakeClock()
        process = FakeProcess(failures=20)
        supervisor = CameraSupervisor(
            camera_id=camera,
            process=process,
            segments=Mock(),
            clock=clock,
        )
        delays = []
        for expected in (5, 10, 20, 40, 80, 160, 300, 300):
            result = supervisor.tick()
            self.assertEqual(SupervisionAction.START_FAILED, result.action)
            delays.append(result.retry_delay_seconds)
            self.assertEqual("camera offline", result.error)
            clock.advance(expected)
        self.assertEqual([5, 10, 20, 40, 80, 160, 300, 300], delays)

    def test_failed_start_does_not_retry_before_deadline(self) -> None:
        camera = CameraId("one")
        clock = FakeClock()
        process = FakeProcess(failures=1)
        supervisor = CameraSupervisor(
            camera_id=camera, process=process, segments=Mock(), clock=clock
        )
        self.assertEqual(SupervisionAction.START_FAILED, supervisor.tick().action)
        clock.advance(4)
        self.assertEqual(SupervisionAction.WAITING_BACKOFF, supervisor.tick().action)
        self.assertEqual(1, process.starts)

    def test_persistent_stale_restarts_by_cycles(self) -> None:
        camera = CameraId("one")
        clock = FakeClock()
        process = FakeProcess(alive=True)
        segments = Mock()
        segments.status.return_value = _status(camera, BufferHealth.STALE)
        supervisor = CameraSupervisor(
            camera_id=camera, process=process, segments=segments, clock=clock
        )
        self.assertEqual(SupervisionAction.MONITORING_STALE, supervisor.tick().action)
        self.assertEqual(SupervisionAction.MONITORING_STALE, supervisor.tick().action)
        self.assertEqual(SupervisionAction.STARTED, supervisor.tick().action)
        self.assertEqual(1, process.stops)
        self.assertEqual(1, process.starts)

    def test_persistent_stale_restarts_by_elapsed_time_and_fresh_clears_it(self) -> None:
        camera = CameraId("one")
        clock = FakeClock()
        process = FakeProcess(alive=True)
        segments = Mock()
        segments.status.side_effect = [
            _status(camera, BufferHealth.MISSING),
            _status(camera, BufferHealth.FRESH),
            _status(camera, BufferHealth.UNKNOWN),
            _status(camera, BufferHealth.UNKNOWN),
        ]
        supervisor = CameraSupervisor(
            camera_id=camera,
            process=process,
            segments=segments,
            clock=clock,
            stale_restart_cycles=10,
        )
        self.assertEqual(SupervisionAction.MONITORING_STALE, supervisor.tick().action)
        self.assertEqual(SupervisionAction.HEALTHY, supervisor.tick().action)
        self.assertEqual(SupervisionAction.MONITORING_STALE, supervisor.tick().action)
        clock.advance(30)
        self.assertEqual(SupervisionAction.STARTED, supervisor.tick().action)

    def test_policy_validation(self) -> None:
        base = dict(
            camera_id=CameraId("one"),
            process=FakeProcess(),
            segments=Mock(),
            clock=FakeClock(),
        )
        invalid = (
            {"initial_backoff_seconds": 0},
            {"initial_backoff_seconds": 10, "maximum_backoff_seconds": 5},
            {"stale_restart_after_seconds": 0},
            {"stale_restart_cycles": 0},
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                CameraSupervisor(**base, **override)

    def test_coordinator_isolates_camera_failure(self) -> None:
        first = Mock()
        first.tick.side_effect = RuntimeError("broken")
        expected = Mock()
        second = Mock()
        second.tick.return_value = expected
        results = CameraSupervisionCoordinator((first, second)).tick_all()
        self.assertEqual((expected,), results)
        second.tick.assert_called_once()


class RecordingScheduler:
    def __init__(self) -> None:
        self.tasks = []

    def submit(self, task) -> None:
        self.tasks.append(task)


class TriggerCoordinatorTests(unittest.TestCase):
    def test_dispatch_schedules_routed_cameras_and_tracks_cooldown_per_camera(self) -> None:
        one, two = CameraId("one"), CameraId("two")
        scheduler = RecordingScheduler()
        coordinator = EdgeTriggerCoordinator(
            router=TriggerRouter(global_token="GLOBAL"),
            scheduler=scheduler,
            cooldown=CooldownPolicy(10),
        )
        routes = [CameraRoute(one), CameraRoute(two)]
        handled = []

        first = coordinator.dispatch(
            token="GLOBAL", cameras=routes, triggered_at=NOW, handler=handled.append
        )
        second = coordinator.dispatch(
            token="GLOBAL",
            cameras=routes,
            triggered_at=NOW + timedelta(seconds=5),
            handler=handled.append,
        )
        for task in scheduler.tasks:
            task()

        self.assertEqual((one, two), first.scheduled)
        self.assertEqual((), second.scheduled)
        self.assertEqual((one, two), second.cooling_down)
        self.assertEqual([one, two], handled)

    def test_skip_cooldown_does_not_mutate_cooldown_history(self) -> None:
        camera = CameraId("one")
        scheduler = RecordingScheduler()
        coordinator = EdgeTriggerCoordinator(
            router=TriggerRouter(global_token="GLOBAL"),
            scheduler=scheduler,
            cooldown=CooldownPolicy(10),
        )
        route = [CameraRoute(camera)]
        coordinator.dispatch(
            token="GLOBAL",
            cameras=route,
            triggered_at=NOW,
            handler=lambda _: None,
            skip_cooldown=True,
        )
        result = coordinator.dispatch(
            token="GLOBAL",
            cameras=route,
            triggered_at=NOW,
            handler=lambda _: None,
        )
        self.assertEqual((camera,), result.scheduled)

    def test_handler_failure_is_isolated_from_sibling_tasks(self) -> None:
        one, two = CameraId("one"), CameraId("two")
        scheduler = RecordingScheduler()
        coordinator = EdgeTriggerCoordinator(
            router=TriggerRouter(global_token="GLOBAL"),
            scheduler=scheduler,
            cooldown=CooldownPolicy(0),
        )
        handled = []

        def handler(camera: CameraId) -> None:
            if camera == one:
                raise RuntimeError("one failed")
            handled.append(camera)

        coordinator.dispatch(
            token="GLOBAL",
            cameras=[CameraRoute(one), CameraRoute(two)],
            triggered_at=NOW,
            handler=handler,
        )
        for task in scheduler.tasks:
            task()
        self.assertEqual([two], handled)

    def test_executor_adapter_delegates_submit(self) -> None:
        executor = Mock()

        def task() -> None:
            return

        ExecutorTaskScheduler(executor).submit(task)
        executor.submit.assert_called_once_with(task)


if __name__ == "__main__":
    unittest.main()
