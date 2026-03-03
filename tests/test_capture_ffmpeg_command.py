from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config.settings import CaptureConfig
from src.video.capture import start_ffmpeg


def _make_rtsp_cfg(base: Path) -> CaptureConfig:
    return CaptureConfig(
        camera_id="cam01",
        camera_name="cam01",
        source_type="rtsp",
        rtsp_url="rtsp://user:pass@192.168.1.20:554/stream1",
        buffer_dir=base / "buffer",
        clips_dir=base / "clips",
        queue_dir=base / "queue",
        failed_dir_highlight=base / "failed",
        seg_time=1,
        pre_seconds=20,
        post_seconds=10,
        pre_segments=20,
        post_segments=10,
    )


class CaptureFfmpegCommandTests(unittest.TestCase):
    def _run_start(self, env: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = _make_rtsp_cfg(base)
            cfg.ensure_dirs()
            fake_proc = MagicMock()
            fake_proc.poll.return_value = None

            effective_env = {"GN_LOG_DIR": str(base / "logs"), "GN_FFMPEG_STARTUP_CHECK_SEC": "0.1"}
            effective_env.update(env)

            with patch.dict(os.environ, effective_env, clear=False), \
                 patch("src.video.capture.check_rtsp_connectivity", return_value=True), \
                 patch("src.video.capture.time.sleep"), \
                 patch("src.video.capture.subprocess.Popen", return_value=fake_proc) as mock_popen:
                proc = start_ffmpeg(cfg)

        self.assertIs(proc, fake_proc)
        cmd = mock_popen.call_args.args[0]
        return list(cmd)

    def test_rtsp_defaults_to_stable_reencode_mode(self) -> None:
        cmd = self._run_start(env={})

        self.assertIn("-c:v", cmd)
        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "libx264")
        self.assertIn("-force_key_frames", cmd)
        self.assertIn("+genpts+discardcorrupt", cmd)
        self.assertNotIn("nobuffer", cmd)
        self.assertNotIn("low_delay", cmd)

        ridx = cmd.index("-reset_timestamps")
        self.assertEqual(cmd[ridx + 1], "1")

    def test_rtsp_can_use_legacy_passthrough_mode(self) -> None:
        cmd = self._run_start(env={"GN_RTSP_PASSTHROUGH": "1"})

        self.assertIn("-c:v", cmd)
        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "copy")
        self.assertNotIn("libx264", cmd)
        self.assertNotIn("-force_key_frames", cmd)

        ridx = cmd.index("-reset_timestamps")
        self.assertEqual(cmd[ridx + 1], "1")

    def test_rtsp_reencode_respects_tuning_envs(self) -> None:
        cmd = self._run_start(
            env={
                "GN_RTSP_GOP": "30",
                "GN_RTSP_CRF": "20",
                "GN_RTSP_PRESET": "ultrafast",
            }
        )

        self.assertIn("-preset", cmd)
        self.assertEqual(cmd[cmd.index("-preset") + 1], "ultrafast")
        self.assertIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "20")
        self.assertIn("-g", cmd)
        self.assertEqual(cmd[cmd.index("-g") + 1], "30")
        self.assertIn("-keyint_min", cmd)
        self.assertEqual(cmd[cmd.index("-keyint_min") + 1], "30")


if __name__ == "__main__":
    unittest.main()

