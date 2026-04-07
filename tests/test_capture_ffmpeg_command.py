from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config.config_loader import reset_config_cache
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
    # Vars controladas pelos testes — removidas do ambiente antes de aplicar env.
    _CONTROLLED_VARS = {
        "GN_RTSP_REENCODE", "GN_RTSP_FPS", "GN_RTSP_GOP",
        "GN_RTSP_PRESET", "GN_RTSP_CRF", "GN_RTSP_VSYNC", "GN_RTSP_USE_WALLCLOCK",
    }

    def setUp(self) -> None:
        reset_config_cache()

    def tearDown(self) -> None:
        reset_config_cache()

    def _run_start(self, env: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = _make_rtsp_cfg(base)
            cfg.ensure_dirs()
            fake_proc = MagicMock()
            fake_proc.poll.return_value = None

            # Remove vars controladas do ambiente base para que apenas
            # o que for passado em env (ou os defaults do código) seja usado.
            clean_env = {k: v for k, v in os.environ.items() if k not in self._CONTROLLED_VARS}
            clean_env["GN_LOG_DIR"] = str(base / "logs")
            clean_env["GN_FFMPEG_STARTUP_CHECK_SEC"] = "0.1"
            clean_env.update(env)

            with patch.dict(os.environ, clean_env, clear=True), \
                 patch("src.video.capture.check_rtsp_connectivity", return_value=True), \
                 patch("src.video.capture.time.sleep"), \
                 patch("src.video.capture.subprocess.Popen", return_value=fake_proc) as mock_popen:
                proc = start_ffmpeg(cfg)

        self.assertIs(proc, fake_proc)
        cmd = mock_popen.call_args.args[0]
        return list(cmd)

    def test_rtsp_defaults_to_reencode_vfr(self) -> None:
        cmd = self._run_start(env={})

        self.assertIn("-c:v", cmd)
        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "libx264")
        self.assertIn("-force_key_frames", cmd)
        self.assertIn("-fps_mode", cmd)
        self.assertEqual(cmd[cmd.index("-fps_mode") + 1], "vfr")
        # discardcorrupt removed — causes static video when combined with frame duplication
        self.assertNotIn("discardcorrupt", " ".join(cmd))
        # break_non_keyframes removed — not needed with forced keyframes on re-encode
        self.assertNotIn("-break_non_keyframes", cmd)

        ridx = cmd.index("-reset_timestamps")
        self.assertEqual(cmd[ridx + 1], "1")

    def test_rtsp_passthrough_copy_when_reencode_disabled(self) -> None:
        cmd = self._run_start(env={"GN_RTSP_REENCODE": "0"})

        self.assertIn("-c:v", cmd)
        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "copy")
        self.assertNotIn("libx264", cmd)
        self.assertNotIn("-force_key_frames", cmd)
        self.assertNotIn("-fps_mode", cmd)

        ridx = cmd.index("-reset_timestamps")
        self.assertEqual(cmd[ridx + 1], "1")

    def test_rtsp_reencode_respects_tuning_envs(self) -> None:
        cmd = self._run_start(
            env={
                "GN_RTSP_REENCODE": "1",
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

    def test_rtsp_fps_filter_when_configured(self) -> None:
        """GN_RTSP_FPS applies -vf fps=N filter (leve, não re-encode pesado)."""
        cmd = self._run_start(
            env={
                "GN_RTSP_REENCODE": "1",
                "GN_RTSP_FPS": "15",
            }
        )

        self.assertIn("-vf", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "fps=15")
        # fps filter must come before codec options
        self.assertLess(cmd.index("-vf"), cmd.index("-c:v"))

    def test_rtsp_fps_filter_not_when_empty(self) -> None:
        """When GN_RTSP_FPS empty/unset, no fps filter."""
        cmd = self._run_start(env={"GN_RTSP_REENCODE": "1"})
        self.assertNotIn("-vf", cmd)

    def test_rtsp_no_wallclock_timestamps_by_default(self) -> None:
        """Wallclock timestamps disabled by default — may cause jitter on unstable networks."""
        for env in ({}, {"GN_RTSP_REENCODE": "1"}):
            cmd = self._run_start(env=env)
            self.assertNotIn("-use_wallclock_as_timestamps", cmd)

    def test_rtsp_wallclock_timestamps_when_enabled(self) -> None:
        """GN_RTSP_USE_WALLCLOCK=1 enables wallclock timestamps for non-monotonic camera streams."""
        cmd = self._run_start(env={"GN_RTSP_USE_WALLCLOCK": "1"})
        self.assertIn("-use_wallclock_as_timestamps", cmd)
        self.assertEqual(cmd[cmd.index("-use_wallclock_as_timestamps") + 1], "1")
        # Must come before -i (input option)
        self.assertLess(cmd.index("-use_wallclock_as_timestamps"), cmd.index("-i"))

    def test_rtsp_err_detect_ignore_err(self) -> None:
        """err_detect ignore_err forces decoder to reconstruct corrupt frames via error concealment."""
        cmd = self._run_start(env={})
        self.assertIn("-err_detect", cmd)
        self.assertEqual(cmd[cmd.index("-err_detect") + 1], "ignore_err")
        # err_detect must come before -i (input option for decoder)
        self.assertLess(cmd.index("-err_detect"), cmd.index("-i"))

    def test_rtsp_no_frame_duplication_flags(self) -> None:
        """discardcorrupt + CFR causes static video — neither should be present in default mode."""
        cmd = self._run_start(env={})
        self.assertNotIn("discardcorrupt", " ".join(cmd))
        # CFR forcing removed; VFR used instead to avoid frame duplication
        if "-fps_mode" in cmd:
            self.assertNotEqual(cmd[cmd.index("-fps_mode") + 1], "cfr")


if __name__ == "__main__":
    unittest.main()
