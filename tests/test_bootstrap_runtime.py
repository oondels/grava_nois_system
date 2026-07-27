from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.bootstrap import (
    EdgeRuntime,
    RuntimeStartupError,
    RuntimeState,
    build_container,
)
from src.domain.configuration import DeviceIdentity


class _Component:
    def __init__(self, name: str, events: list[str], *, fail_start: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail_start = fail_start

    def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError(self.name)

    def shutdown(self) -> None:
        self.events.append(f"shutdown:{self.name}")


class EdgeRuntimeTests(unittest.TestCase):
    def test_starts_in_order_and_shutdown_is_reverse_and_idempotent(self) -> None:
        events: list[str] = []
        runtime = EdgeRuntime(
            (
                _Component("config", events),
                _Component("mqtt", events),
                _Component("capture", events),
            )
        )

        runtime.start()
        runtime.start()
        runtime.shutdown()
        runtime.shutdown()

        self.assertEqual(
            [
                "start:config",
                "start:mqtt",
                "start:capture",
                "shutdown:capture",
                "shutdown:mqtt",
                "shutdown:config",
            ],
            events,
        )
        self.assertIs(runtime.state, RuntimeState.STOPPED)

    def test_start_failure_rolls_back_only_started_components(self) -> None:
        events: list[str] = []
        runtime = EdgeRuntime(
            (
                _Component("config", events),
                _Component("broken", events, fail_start=True),
                _Component("capture", events),
            )
        )

        with self.assertRaises(RuntimeStartupError):
            runtime.start()
        runtime.shutdown()

        self.assertEqual(
            ["start:config", "start:broken", "shutdown:config"],
            events,
        )
        self.assertIs(runtime.state, RuntimeState.FAILED)

    def test_shutdown_before_start_is_safe(self) -> None:
        runtime = EdgeRuntime()
        runtime.shutdown()
        runtime.shutdown()
        self.assertIs(runtime.state, RuntimeState.STOPPED)


class ContainerTests(unittest.TestCase):
    def test_wiring_loads_one_snapshot_without_hardware(self) -> None:
        operational = SimpleNamespace(
            cameras=(),
            capture=SimpleNamespace(stale_after_seconds=30.0),
            processing=SimpleNamespace(development_mode=False),
        )
        repository = Mock()
        repository.load.return_value = operational
        identity = DeviceIdentity(device_id="edge-1", client_id="client", venue_id="venue")

        container = build_container(
            config_repository=repository,
            identity=identity,
            clock=Mock(),
        )

        repository.load.assert_called_once_with()
        self.assertIs(container.snapshot.operational, operational)
        self.assertEqual(identity, container.snapshot.identity)
        self.assertEqual({}, dict(container.capture_replays))
        self.assertIsNone(container.process_clip_job)
        container.runtime.start()
        container.runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
