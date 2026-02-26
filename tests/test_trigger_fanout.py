from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config.settings import CaptureConfig
from main import CameraRuntime, _trigger_fan_out


def _make_runtime(camera_id: str) -> CameraRuntime:
    cfg = MagicMock(spec=CaptureConfig)
    cfg.camera_id = camera_id
    segbuf = MagicMock()
    return CameraRuntime(cfg=cfg, proc=MagicMock(), segbuf=segbuf)


class TriggerFanOutTests(unittest.TestCase):
    def test_all_cameras_receive_trigger(self) -> None:
        runtimes = [_make_runtime("cam01"), _make_runtime("cam02")]
        called_ids: list[str] = []

        def fake_build(cfg, segbuf):
            called_ids.append(cfg.camera_id)
            return Path(f"/tmp/highlight_{cfg.camera_id}.mp4")

        with patch("main.build_highlight", side_effect=fake_build), \
             patch("main.enqueue_clip"), \
             ThreadPoolExecutor(max_workers=2) as exe:
            _trigger_fan_out(runtimes, Path("/tmp/failed"), exe, "test-001")

        self.assertIn("cam01", called_ids)
        self.assertIn("cam02", called_ids)
        self.assertEqual(len(called_ids), 2)

    def test_busy_camera_is_skipped(self) -> None:
        rt1 = _make_runtime("cam01")
        rt2 = _make_runtime("cam02")
        # Hold the lock on cam01 to simulate busy
        rt1.capture_lock.acquire()
        called_ids: list[str] = []

        def fake_build(cfg, segbuf):
            called_ids.append(cfg.camera_id)
            return None

        try:
            with patch("main.build_highlight", side_effect=fake_build), \
                 ThreadPoolExecutor(max_workers=2) as exe:
                _trigger_fan_out([rt1, rt2], Path("/tmp/failed"), exe, "test-002")
        finally:
            rt1.capture_lock.release()

        self.assertNotIn("cam01", called_ids)
        self.assertIn("cam02", called_ids)

    def test_camera_failure_does_not_affect_others(self) -> None:
        runtimes = [_make_runtime("cam01"), _make_runtime("cam02")]
        called_ids: list[str] = []

        def fake_build(cfg, segbuf):
            if cfg.camera_id == "cam01":
                raise RuntimeError("camera failure")
            called_ids.append(cfg.camera_id)
            return None

        with patch("main.build_highlight", side_effect=fake_build), \
             ThreadPoolExecutor(max_workers=2) as exe:
            _trigger_fan_out(runtimes, Path("/tmp/failed"), exe, "test-003")

        self.assertIn("cam02", called_ids)

    def test_lock_released_after_build(self) -> None:
        rt = _make_runtime("cam01")

        with patch("main.build_highlight", return_value=None), \
             ThreadPoolExecutor(max_workers=1) as exe:
            _trigger_fan_out([rt], Path("/tmp/failed"), exe, "test-004")

        # Lock should be released – acquiring it should succeed immediately
        acquired = rt.capture_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        if acquired:
            rt.capture_lock.release()


if __name__ == "__main__":
    unittest.main()
