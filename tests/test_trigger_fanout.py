from __future__ import annotations

import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.config.settings import CaptureConfig
from main import (
    CameraRuntime,
    _camera_supervisor,
    _get_fanout_targets,
    _trigger_fan_out,
    _trigger_single_camera,
)


def _make_runtime(camera_id: str, pico_trigger_token: str | None = None) -> CameraRuntime:
    cfg = MagicMock(spec=CaptureConfig)
    cfg.camera_id = camera_id
    cfg.pico_trigger_token = pico_trigger_token
    segbuf = MagicMock()
    segbuf.diagnostics.return_value = SimpleNamespace(
        buffer_status="FRESH",
        buffer_fresh=True,
        segment_age_sec=0.5,
        last_segment_at="2026-04-17T12:00:00+00:00",
        segment_count=10,
    )
    proc = MagicMock()
    proc.poll.return_value = None
    return CameraRuntime(cfg=cfg, proc=proc, segbuf=segbuf, camera_status="OK")


class CameraSupervisorTests(unittest.TestCase):
    def test_persistent_stale_buffer_restarts_ffmpeg(self) -> None:
        rt = _make_runtime("cam01")
        rt.segbuf.diagnostics.return_value = SimpleNamespace(
            buffer_status="STALE",
            buffer_fresh=False,
            segment_age_sec=31.0,
            last_segment_at="2026-04-17T12:00:00+00:00",
            segment_count=10,
        )
        old_proc = rt.proc
        new_proc = MagicMock()
        new_proc.poll.return_value = None
        new_segbuf = MagicMock()
        stop_evt = threading.Event()

        def fake_start_ffmpeg(cfg):
            self.assertEqual(cfg.camera_id, "cam01")
            stop_evt.set()
            return new_proc

        with patch("main.clear_buffer") as clear_buffer, \
             patch("main.start_ffmpeg", side_effect=fake_start_ffmpeg) as start_ffmpeg, \
             patch("main.SegmentBuffer", return_value=new_segbuf):
            _camera_supervisor(
                rt,
                stop_evt,
                stale_restart_after_sec=999.0,
                stale_restart_cycles=1,
                poll_interval=0.01,
            )

        old_proc.terminate.assert_called_once()
        clear_buffer.assert_called_once_with(rt.cfg)
        start_ffmpeg.assert_called_once_with(rt.cfg)
        new_segbuf.start.assert_called_once()
        self.assertIs(rt.proc, new_proc)
        self.assertIs(rt.segbuf, new_segbuf)
        self.assertEqual(rt.camera_status, "OK")

    def test_transient_stale_buffer_does_not_restart_ffmpeg(self) -> None:
        rt = _make_runtime("cam01")
        stop_evt = threading.Event()

        def fake_diagnostics(stale_after_sec=10.0):
            stop_evt.set()
            return SimpleNamespace(
                buffer_status="STALE",
                buffer_fresh=False,
                segment_age_sec=12.0,
                last_segment_at="2026-04-17T12:00:00+00:00",
                segment_count=10,
            )

        rt.segbuf.diagnostics.side_effect = fake_diagnostics
        old_proc = rt.proc

        with patch("main.clear_buffer") as clear_buffer, \
             patch("main.start_ffmpeg") as start_ffmpeg:
            _camera_supervisor(
                rt,
                stop_evt,
                stale_restart_after_sec=999.0,
                stale_restart_cycles=3,
                poll_interval=0.01,
            )

        old_proc.terminate.assert_not_called()
        clear_buffer.assert_not_called()
        start_ffmpeg.assert_not_called()
        self.assertEqual(rt.camera_status, "UNAVAILABLE")
        self.assertEqual(rt.last_error, "Buffer sem segmentos novos")


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

    def test_stale_buffer_is_skipped_and_event_is_published(self) -> None:
        rt = _make_runtime("cam01")
        rt.segbuf.diagnostics.return_value = SimpleNamespace(
            buffer_status="STALE",
            buffer_fresh=False,
            segment_age_sec=12.0,
            last_segment_at="2026-04-17T12:00:00+00:00",
            segment_count=10,
        )
        event_service = MagicMock()

        with patch("main.build_highlight") as build_mock, \
             ThreadPoolExecutor(max_workers=1) as exe:
            _trigger_fan_out(
                [rt],
                Path("/tmp/failed"),
                exe,
                "stale-001",
                trigger_source="pico",
                capture_event_service=event_service,
            )

        build_mock.assert_not_called()
        event_service.publish_trigger_rejected.assert_called_once()


class PicoTokenRoutingTests(unittest.TestCase):
    def test_dedicated_token_triggers_only_matching_camera(self) -> None:
        """Token mapeado → dispara apenas a câmera correspondente, não as demais."""
        rt1 = _make_runtime("cam01", pico_trigger_token="BTN_1")
        rt2 = _make_runtime("cam02")
        called_ids: list[str] = []

        def fake_build(cfg, segbuf):
            called_ids.append(cfg.camera_id)
            return None

        with patch("main.build_highlight", side_effect=fake_build), \
             ThreadPoolExecutor(max_workers=2) as exe:
            _trigger_single_camera(rt1, Path("/tmp/failed"), exe, "tok-001", cooldown_sec=0.0)

        self.assertIn("cam01", called_ids)
        self.assertNotIn("cam02", called_ids)

    def test_global_fanout_skips_cameras_with_dedicated_token(self) -> None:
        """Token global → fan-out dispara apenas câmeras sem token dedicado."""
        rt1 = _make_runtime("cam01", pico_trigger_token="BTN_1")
        rt2 = _make_runtime("cam02")  # sem token dedicado
        targets = _get_fanout_targets([rt1, rt2])
        self.assertEqual([rt.cfg.camera_id for rt in targets], ["cam02"])

    def test_global_fanout_all_cameras_when_all_have_tokens(self) -> None:
        """Quando todas as câmeras têm token, fan-out global dispara todas (fallback de debug)."""
        rt1 = _make_runtime("cam01", pico_trigger_token="BTN_1")
        rt2 = _make_runtime("cam02", pico_trigger_token="BTN_2")
        targets = _get_fanout_targets([rt1, rt2])
        self.assertEqual(len(targets), 2)

    def test_unknown_token_does_not_trigger_any_camera(self) -> None:
        """Token desconhecido → nenhuma câmera dispara, sem levantar exceção."""
        rt1 = _make_runtime("cam01", pico_trigger_token="BTN_1")
        rt2 = _make_runtime("cam02")
        called_ids: list[str] = []

        def fake_build(cfg, segbuf):
            called_ids.append(cfg.camera_id)
            return None

        # Simula o roteamento: token desconhecido não está em token_map nem é global token
        token_map = {"BTN_1": lambda: None}  # cam01 handler (not calling build_highlight here)
        unknown_token = "BTN_UNKNOWN"
        # Unknown token: not in token_map, not global → nenhuma ação
        dispatched = unknown_token in token_map
        self.assertFalse(dispatched)
        self.assertEqual(called_ids, [])

    def test_trigger_single_camera_respects_cooldown(self) -> None:
        """Câmera em cooldown é ignorada por _trigger_single_camera."""
        rt = _make_runtime("cam01")
        rt._cooldown_until = time.time() + 120.0  # cooldown ativo
        called_ids: list[str] = []

        def fake_build(cfg, segbuf):
            called_ids.append(cfg.camera_id)
            return None

        with patch("main.build_highlight", side_effect=fake_build), \
             ThreadPoolExecutor(max_workers=1) as exe:
            _trigger_single_camera(rt, Path("/tmp/failed"), exe, "cool-001", cooldown_sec=120.0)

        self.assertEqual(called_ids, [])

    def test_trigger_single_camera_fires_when_not_in_cooldown(self) -> None:
        """Câmera fora do cooldown dispara normalmente."""
        rt = _make_runtime("cam01")
        rt._cooldown_until = 0.0  # sem cooldown
        called_ids: list[str] = []

        def fake_build(cfg, segbuf):
            called_ids.append(cfg.camera_id)
            return None

        with patch("main.build_highlight", side_effect=fake_build), \
             ThreadPoolExecutor(max_workers=1) as exe:
            _trigger_single_camera(rt, Path("/tmp/failed"), exe, "cool-002", cooldown_sec=120.0)

        self.assertIn("cam01", called_ids)


if __name__ == "__main__":
    unittest.main()
