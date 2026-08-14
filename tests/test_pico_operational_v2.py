from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.services.pico_operations import ConfirmedActionArm, MaintenanceMode, camera_state
from src.services.pico_serial_controller import PicoSerialController, PicoStartedHandshake

FIRMWARE_PATH = (
    Path(__file__).resolve().parents[1] / "raspberry_pico" / "main_operational_v2.py"
)
SPEC = importlib.util.spec_from_file_location("pico_operational_v2", FIRMWARE_PATH)
assert SPEC is not None and SPEC.loader is not None
firmware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(firmware)


class AdminGestureTests(unittest.TestCase):
    def _clicks(self, count: int) -> list[str]:
        gesture = firmware.AdminGesture()
        events: list[str] = []
        now = 0
        for _ in range(count):
            events.extend(gesture.update(True, now))
            now += 100
            events.extend(gesture.update(False, now))
            now += 200
        events.extend(gesture.update(False, now + firmware.SEQUENCE_GAP_MS))
        return events

    def test_click_sequences_preserve_actions(self) -> None:
        self.assertEqual(self._clicks(2), ["REQUEST_DIAGNOSTIC"])
        self.assertEqual(self._clicks(3), ["TOGGLE_MAINTENANCE"])
        self.assertEqual(self._clicks(5), ["RESTART_DOCKER"])

    def test_holds_map_to_self_test_trigger_shutdown_and_pull(self) -> None:
        cases = (
            (2500, "RUN_SELF_TEST"),
            (4500, "TRIGGER_GLOBAL"),
            (8500, "ARM_SHUTDOWN"),
            (12000, "PULL_DOCKER"),
        )
        for held_ms, expected in cases:
            with self.subTest(expected=expected):
                gesture = firmware.AdminGesture()
                gesture.update(True, 0)
                self.assertEqual(gesture.update(False, held_ms), [expected])

    def test_shutdown_requires_click_confirmation(self) -> None:
        gesture = firmware.AdminGesture()
        gesture.update(True, 0)
        gesture.update(False, 8500)
        gesture.update(True, 9000)
        self.assertEqual(gesture.update(False, 9100), ["SHUTDOWN_HOST"])

    def test_admin_button_debounce_ignores_short_bounce(self) -> None:
        button = firmware.DebouncedButton(False)
        self.assertFalse(button.update(True, 10))
        self.assertFalse(button.update(False, 20))
        self.assertFalse(button.update(True, 30))
        self.assertTrue(button.update(True, 30 + firmware.DEBOUNCE_MS))


class OperationalStateTests(unittest.TestCase):
    def test_handshake_capabilities_and_heartbeat(self) -> None:
        state = firmware.OperationalState()
        self.assertEqual(
            state.apply_command("GRN_STARTED", 100),
            ["ACK_GRN_STARTED", "PICO_CAPS:2:DUAL_LED,HEARTBEAT,ACTIONS"],
        )
        self.assertEqual(state.apply_command("PING:7", 200), ["PONG:7"])

    def test_upload_blink_overrides_connected_mqtt(self) -> None:
        state = firmware.OperationalState()
        state.edge_started = False
        state.camera = "READY"
        state.mqtt = "CONNECTED"
        state.upload = "PENDING"
        self.assertEqual(state.leds(0), (True, True))
        self.assertEqual(state.leds(300), (True, False))


class PicoOperationsTests(unittest.TestCase):
    def test_camera_state_requires_all_enabled_runtimes_ready(self) -> None:
        snapshot = {
            "cameras": [
                {"ffmpeg_alive": True, "buffer_fresh": True},
                {"ffmpeg_alive": True, "buffer_fresh": False},
            ]
        }
        self.assertEqual(camera_state(snapshot), "DEGRADED")

    def test_maintenance_toggle(self) -> None:
        maintenance = MaintenanceMode(duration_sec=60)
        self.assertTrue(maintenance.toggle())
        self.assertTrue(maintenance.active)
        self.assertFalse(maintenance.toggle())
        self.assertFalse(maintenance.active)

    def test_shutdown_arm_is_single_use_and_expires(self) -> None:
        now = [10.0]
        arm = ConfirmedActionArm(window_sec=5.0, clock=lambda: now[0])
        self.assertTrue(arm.arm(True))
        self.assertTrue(arm.consume())
        self.assertFalse(arm.consume())
        self.assertTrue(arm.arm(True))
        now[0] = 16.0
        self.assertFalse(arm.consume())

    def test_serial_controller_negotiates_v2_and_pong(self) -> None:
        callback = MagicMock()
        controller = PicoSerialController(
            port="/dev/null",
            on_token=callback,
            logger=MagicMock(),
        )
        handshake = PicoStartedHandshake()
        controller._handle_line(
            "PICO_CAPS:2:DUAL_LED,HEARTBEAT,ACTIONS", handshake
        )
        controller._handle_line("PONG:1", handshake)
        self.assertEqual(controller.protocol_version, 2)
        self.assertIn("HEARTBEAT", controller.capabilities)
        self.assertTrue(controller.has_recent_pong())
        callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
