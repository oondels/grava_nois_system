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
        "GN_RTSP_PROFILE", "GN_RTSP_LOW_LATENCY_INPUT", "GN_RTSP_LOW_DELAY_CODEC_FLAGS",
        "GN_LIGHT_MODE",
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

    # -------------------------------------------------------------------------
    # Default behavior: no profile, no lightMode → infers hq → passthrough
    # -------------------------------------------------------------------------

    def test_rtsp_default_is_hq_passthrough(self) -> None:
        """Default (no profile, lightMode=false) → hq profile → -c:v copy."""
        cmd = self._run_start(env={})

        self.assertIn("-c:v", cmd)
        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "copy")
        self.assertNotIn("libx264", cmd)
        self.assertNotIn("-force_key_frames", cmd)
        self.assertNotIn("-fps_mode", cmd)

        ridx = cmd.index("-reset_timestamps")
        self.assertEqual(cmd[ridx + 1], "1")

    def test_rtsp_audio_always_discarded(self) -> None:
        """RTSP capture always strips audio (-an)."""
        for env in ({}, {"GN_RTSP_REENCODE": "1"}, {"GN_RTSP_PROFILE": "compatible"}):
            cmd = self._run_start(env=env)
            self.assertIn("-an", cmd)

    # -------------------------------------------------------------------------
    # Profile: hq (explicit)
    # -------------------------------------------------------------------------

    def test_rtsp_profile_hq_uses_copy(self) -> None:
        """Explicit profile=hq → -c:v copy, no fps_mode, no force_key_frames."""
        cmd = self._run_start(env={"GN_RTSP_PROFILE": "hq"})

        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "copy")
        self.assertNotIn("libx264", cmd)
        self.assertNotIn("-force_key_frames", cmd)
        self.assertNotIn("-fps_mode", cmd)

    def test_rtsp_profile_hq_no_wallclock_by_default(self) -> None:
        """hq profile should not add -use_wallclock_as_timestamps by default."""
        cmd = self._run_start(env={"GN_RTSP_PROFILE": "hq"})
        self.assertNotIn("-use_wallclock_as_timestamps", cmd)

    # -------------------------------------------------------------------------
    # Profile: compatible (explicit)
    # -------------------------------------------------------------------------

    def test_rtsp_profile_compatible_uses_reencode(self) -> None:
        """Explicit profile=compatible → libx264, force_key_frames, fps_mode=vfr."""
        cmd = self._run_start(env={"GN_RTSP_PROFILE": "compatible"})

        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "libx264")
        self.assertIn("-force_key_frames", cmd)
        self.assertIn("-fps_mode", cmd)
        self.assertEqual(cmd[cmd.index("-fps_mode") + 1], "vfr")

    # -------------------------------------------------------------------------
    # lightMode inference
    # -------------------------------------------------------------------------

    def test_rtsp_lightmode_false_infers_hq(self) -> None:
        """lightMode=false without explicit profile → infers hq → passthrough."""
        cmd = self._run_start(env={"GN_LIGHT_MODE": "0"})

        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "copy")
        self.assertNotIn("libx264", cmd)

    def test_rtsp_lightmode_true_infers_compatible(self) -> None:
        """lightMode=true without explicit profile → infers compatible → reencode."""
        cmd = self._run_start(env={"GN_LIGHT_MODE": "1"})

        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "libx264")
        self.assertIn("-force_key_frames", cmd)
        self.assertIn("-fps_mode", cmd)
        self.assertEqual(cmd[cmd.index("-fps_mode") + 1], "vfr")

    # -------------------------------------------------------------------------
    # Explicit reencode override (wins over profile)
    # -------------------------------------------------------------------------

    def test_rtsp_reencode_true_overrides_hq_profile(self) -> None:
        """profile=hq + reencode=true → explicit reencode wins → libx264."""
        cmd = self._run_start(env={"GN_RTSP_PROFILE": "hq", "GN_RTSP_REENCODE": "1"})

        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "libx264")
        self.assertIn("-force_key_frames", cmd)

    def test_rtsp_reencode_false_overrides_compatible_profile(self) -> None:
        """profile=compatible + reencode=false → explicit passthrough wins."""
        cmd = self._run_start(env={"GN_RTSP_PROFILE": "compatible", "GN_RTSP_REENCODE": "0"})

        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "copy")
        self.assertNotIn("libx264", cmd)
        self.assertNotIn("-force_key_frames", cmd)

    # -------------------------------------------------------------------------
    # Reencode tuning params (only apply when reencode active)
    # -------------------------------------------------------------------------

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
        """GN_RTSP_FPS applies -vf fps=N filter when reencode is active."""
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

    # -------------------------------------------------------------------------
    # Wallclock timestamps
    # -------------------------------------------------------------------------

    def test_rtsp_no_wallclock_timestamps_by_default(self) -> None:
        """Wallclock timestamps disabled by default."""
        for env in ({}, {"GN_RTSP_REENCODE": "1"}):
            cmd = self._run_start(env=env)
            self.assertNotIn("-use_wallclock_as_timestamps", cmd)

    def test_rtsp_wallclock_timestamps_when_enabled(self) -> None:
        """GN_RTSP_USE_WALLCLOCK=1 enables wallclock timestamps."""
        cmd = self._run_start(env={"GN_RTSP_USE_WALLCLOCK": "1"})
        self.assertIn("-use_wallclock_as_timestamps", cmd)
        self.assertEqual(cmd[cmd.index("-use_wallclock_as_timestamps") + 1], "1")
        # Must come before -i (input option)
        self.assertLess(cmd.index("-use_wallclock_as_timestamps"), cmd.index("-i"))

    # -------------------------------------------------------------------------
    # Decoder / stream options
    # -------------------------------------------------------------------------

    def test_rtsp_err_detect_ignore_err(self) -> None:
        """err_detect ignore_err forces decoder to reconstruct corrupt frames."""
        cmd = self._run_start(env={})
        self.assertIn("-err_detect", cmd)
        self.assertEqual(cmd[cmd.index("-err_detect") + 1], "ignore_err")
        # err_detect must come before -i (input option for decoder)
        self.assertLess(cmd.index("-err_detect"), cmd.index("-i"))

    def test_rtsp_no_frame_duplication_flags(self) -> None:
        """discardcorrupt + CFR causes static video — neither should be present."""
        cmd = self._run_start(env={})
        self.assertNotIn("discardcorrupt", " ".join(cmd))
        if "-fps_mode" in cmd:
            self.assertNotEqual(cmd[cmd.index("-fps_mode") + 1], "cfr")

    # -------------------------------------------------------------------------
    # Experimental flags
    # -------------------------------------------------------------------------

    def test_low_latency_input_injects_nobuffer_before_genpts(self) -> None:
        """lowLatencyInput=true adds -fflags nobuffer before -fflags +genpts."""
        cmd = self._run_start(env={"GN_RTSP_LOW_LATENCY_INPUT": "1"})

        self.assertIn("-fflags", cmd)
        # nobuffer appears before +genpts
        indices_fflags = [i for i, v in enumerate(cmd) if v == "-fflags"]
        fflags_values = [cmd[i + 1] for i in indices_fflags]
        self.assertIn("nobuffer", fflags_values)
        self.assertIn("+genpts", fflags_values)
        nobuffer_idx = next(i for i in indices_fflags if cmd[i + 1] == "nobuffer")
        genpts_idx = next(i for i in indices_fflags if cmd[i + 1] == "+genpts")
        self.assertLess(nobuffer_idx, genpts_idx)

    def test_low_latency_input_disabled_by_default(self) -> None:
        """lowLatencyInput off by default — no -fflags nobuffer."""
        cmd = self._run_start(env={})
        # +genpts is expected; nobuffer should NOT appear
        fflags_values = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-fflags"]
        self.assertNotIn("nobuffer", fflags_values)

    def test_low_delay_codec_flags_injects_flags_low_delay_with_reencode(self) -> None:
        """lowDelayCodecFlags=true adds -flags low_delay when reencode is active."""
        cmd = self._run_start(
            env={"GN_RTSP_REENCODE": "1", "GN_RTSP_LOW_DELAY_CODEC_FLAGS": "1"}
        )

        self.assertIn("-flags", cmd)
        self.assertEqual(cmd[cmd.index("-flags") + 1], "low_delay")

    def test_low_delay_codec_flags_absent_without_reencode(self) -> None:
        """lowDelayCodecFlags=true with passthrough (-c:v copy) must NOT add -flags low_delay."""
        cmd = self._run_start(
            env={"GN_RTSP_PROFILE": "hq", "GN_RTSP_LOW_DELAY_CODEC_FLAGS": "1"}
        )

        cidx = cmd.index("-c:v")
        self.assertEqual(cmd[cidx + 1], "copy")
        self.assertNotIn("-flags", cmd)

    def test_low_delay_codec_flags_disabled_by_default(self) -> None:
        """lowDelayCodecFlags off by default — no -flags low_delay."""
        cmd = self._run_start(env={"GN_RTSP_REENCODE": "1"})
        self.assertNotIn("-flags", cmd)


if __name__ == "__main__":
    unittest.main()
