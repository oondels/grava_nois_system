"""Terminal input and deferred feedback, without cameras or services."""

import json
import os
import pty
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from main import _listen_for_enter, _trigger_fan_out
from src.services.mqtt.operational_event_service import OperationalEventService


class TerminalTriggerTests(unittest.TestCase):
    def test_enter_records_trigger_time_and_reports_eof_without_stopping_runtime(self):
        signals = queue.Queue()
        stop = threading.Event()
        before = time.monotonic()
        with patch("sys.stdin.fileno", return_value=0), patch("select.select", return_value=([0], [], [])), patch("os.read", side_effect=[b"\n", b""]), patch("main.logger") as log:
            _listen_for_enter(signals, stop)
        source, captured_at, triggered_mono = signals.get_nowait()
        self.assertEqual(source, "enter")
        self.assertIsNotNone(datetime.fromisoformat(captured_at).tzinfo)
        self.assertGreaterEqual(triggered_mono, before)
        self.assertLessEqual(triggered_mono, time.monotonic())
        self.assertTrue(signals.empty())
        self.assertFalse(stop.is_set())
        log.info.assert_called_once()
        log.warning.assert_called_once()

    def test_keyboard_interrupt_stops_without_enqueuing_trigger(self):
        signals = queue.Queue()
        stop = threading.Event()
        with patch("sys.stdin.fileno", return_value=0), patch("select.select", side_effect=KeyboardInterrupt):
            _listen_for_enter(signals, stop)
        self.assertTrue(stop.is_set())
        self.assertTrue(signals.empty())

    def test_no_camera_reports_rejection_without_starting_work(self):
        executor = Mock()
        with patch("main.logger") as log:
            _trigger_fan_out([], Path("unused"), executor, "trigger", trigger_source="enter")
        executor.submit.assert_not_called()
        log.warning.assert_called_once()

    def test_real_terminal_newline_enqueues_one_enter(self):
        root = Path(__file__).resolve().parents[1]
        program = """
import json, logging, queue, threading
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
logging.disable(logging.CRITICAL)
from main import _listen_for_enter
signals = queue.Queue()
stop = threading.Event()
reader = threading.Thread(target=_listen_for_enter, args=(signals, stop))
reader.start()
signal = signals.get(timeout=5)
stop.set()
reader.join(1)
assert not reader.is_alive()
print(json.dumps({'source': signal[0], 'captured_at': signal[1]}), flush=True)
"""
        master, slave = pty.openpty()
        try:
            with tempfile.TemporaryDirectory(prefix="gn-terminal-test-") as temporary:
                env = {
                    "PATH": os.environ["PATH"],
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "GN_CONFIG_PATH": str(Path(temporary) / "config.json"),
                    "GN_LOG_DIR": temporary,
                }
                with subprocess.Popen(
                    [sys.executable, "-c", program],
                    cwd=root,
                    env=env,
                    stdin=slave,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ) as process:
                    try:
                        os.write(master, b"\n")
                        stdout, stderr = process.communicate(timeout=10)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["source"], "enter")
        finally:
            os.close(master)
            os.close(slave)

    def test_preservation_outcome_is_logged_separately_from_transport_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="gn-event-test-") as temporary:
            events = OperationalEventService(
                Path(temporary),
                Mock(),
                lambda suffix: suffix,
                {"device_id": "test-device", "client_id": "test-client", "venue_id": "test-venue"},
                "test-secret",
            )
            with patch("src.services.mqtt.operational_event_service.logger") as log:
                events.emit(
                    "capture.preserved", code="preserved", context={"camera_id": "notebook"}
                )
                events.emit(
                    "capture.not_preserved",
                    code="preservation_incomplete",
                    context={"camera_id": "notebook"},
                )
            log.info.assert_called_once()
            log.warning.assert_called_once()
            self.assertEqual(len(list(events.outbox.glob("*.json"))), 2)
            self.assertIsNone(events.last_ack_at)


if __name__ == "__main__":
    unittest.main()
