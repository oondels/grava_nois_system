from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.domain.capture import BufferHealth, CameraId, CameraState
from src.infrastructure.media import SegmentBufferRepository, SubprocessCaptureProcess


class SubprocessCaptureProcessTests(unittest.TestCase):
    def test_starts_explicit_command_and_is_idempotent_while_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handle = Mock()
            handle.poll.return_value = None
            spawn = Mock(return_value=handle)
            process = SubprocessCaptureProcess(
                command=("ffmpeg", "-i", "camera"),
                log_path=Path(tmp) / "camera.log",
                spawn=spawn,
            )

            process.start()
            process.start()

            self.assertTrue(process.is_alive())
            spawn.assert_called_once()
            args, kwargs = spawn.call_args
            self.assertEqual(("ffmpeg", "-i", "camera"), args[0])
            self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])
            self.assertEqual(subprocess.STDOUT, kwargs["stderr"])
            self.assertTrue(kwargs["stdout"].closed)

    def test_stop_escalates_to_kill_after_timeout_and_shutdown_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handle = Mock()
            handle.poll.return_value = None
            handle.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 1), 0]
            process = SubprocessCaptureProcess(
                command=("ffmpeg",),
                log_path=Path(tmp) / "camera.log",
                stop_timeout_seconds=1,
                spawn=Mock(return_value=handle),
            )
            process.start()

            process.shutdown()
            process.shutdown()

            handle.terminate.assert_called_once()
            handle.kill.assert_called_once()
            self.assertFalse(process.is_alive())

    def test_rejects_implicit_or_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            SubprocessCaptureProcess(command=(), log_path=Path("capture.log"))
        with self.assertRaises(ValueError):
            SubprocessCaptureProcess(
                command=("ffmpeg",),
                log_path=Path("capture.log"),
                stop_timeout_seconds=0,
            )


class SegmentBufferRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.camera = CameraId("camera-1")
        self.buffer = Mock()
        self.repository = SegmentBufferRepository(
            camera_id=self.camera,
            buffer=self.buffer,
            segment_duration_seconds=2,
            maximum_segments=10,
            stale_after_seconds=12,
            camera_state=lambda: CameraState.OK,
        )

    def test_lists_existing_overlapping_segments_in_chronological_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "buffer000001.ts"
            second = Path(tmp) / "buffer000002.ts"
            missing = Path(tmp) / "buffer000003.ts"
            first.touch()
            second.touch()
            base = datetime.now(tz=UTC)
            first_mtime = (base + timedelta(seconds=2)).timestamp()
            second_mtime = (base + timedelta(seconds=4)).timestamp()
            first.touch()
            second.touch()
            import os

            os.utime(first, (first_mtime, first_mtime))
            os.utime(second, (second_mtime, second_mtime))
            self.buffer.snapshot_last.return_value = [
                str(second),
                str(missing),
                str(first),
            ]

            result = self.repository.list_between(
                self.camera,
                base + timedelta(seconds=1),
                base + timedelta(seconds=3),
            )

            self.assertEqual(
                ("buffer000001.ts", "buffer000002.ts"),
                tuple(segment.segment_id for segment in result),
            )
            self.assertEqual(base, result[0].started_at)
            self.buffer.snapshot_last.assert_called_once_with(10)

    def test_translates_diagnostics_and_delegates_lifecycle(self) -> None:
        newest = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)
        self.buffer.diagnostics.return_value = SimpleNamespace(
            buffer_status="FRESH",
            segment_count=4,
            last_segment_at=newest.isoformat(),
        )

        status = self.repository.status(self.camera)
        self.repository.start()
        self.repository.shutdown()

        self.assertEqual(CameraState.OK, status.state)
        self.assertEqual(BufferHealth.FRESH, status.buffer.health)
        self.assertEqual(4, status.buffer.segment_count)
        self.assertEqual(newest, status.buffer.newest_segment_at)
        self.buffer.diagnostics.assert_called_once_with(stale_after_sec=12)
        self.buffer.start.assert_called_once_with()
        self.buffer.stop.assert_called_once_with(join_timeout=2)

    def test_unknown_diagnostics_and_wrong_camera_are_safe(self) -> None:
        self.buffer.diagnostics.return_value = SimpleNamespace(
            buffer_status="future-value",
            segment_count=-1,
            last_segment_at="invalid",
        )
        status = self.repository.status(self.camera)
        self.assertEqual(BufferHealth.UNKNOWN, status.buffer.health)
        self.assertEqual(0, status.buffer.segment_count)
        self.assertIsNone(status.buffer.newest_segment_at)
        with self.assertRaises(ValueError):
            self.repository.status(CameraId("camera-2"))

    def test_validates_explicit_operational_values(self) -> None:
        base = dict(
            camera_id=self.camera,
            buffer=self.buffer,
            segment_duration_seconds=2,
            maximum_segments=10,
            stale_after_seconds=12,
            camera_state=lambda: CameraState.OK,
        )
        for override in (
            {"segment_duration_seconds": 0},
            {"maximum_segments": 0},
            {"stale_after_seconds": 0},
            {"join_timeout_seconds": 0},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                SegmentBufferRepository(**(base | override))


if __name__ == "__main__":
    unittest.main()
