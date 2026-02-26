from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config.settings import load_capture_configs


class LegacyCompatibilityTests(unittest.TestCase):
    """Validates backward compatibility: legacy GN_RTSP_URL mode and fallback chain."""

    # ------------------------------------------------------------------ helpers

    def _patched(self, overrides: dict):
        """patch.dict context that clears the three config sources first."""
        env = {"GN_CAMERAS_JSON": "", "GN_RTSP_URLS": "", "GN_RTSP_URL": ""}
        env.update(overrides)
        return patch.dict(os.environ, env, clear=False)

    # ------------------------------------------------------------------ GN_RTSP_URL (legacy)

    def test_legacy_rtsp_url_single_camera(self) -> None:
        """GN_RTSP_URL produces exactly one camera with id 'cam01' and source_type 'rtsp'."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({"GN_RTSP_URL": "rtsp://192.168.1.100/live"}):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].camera_id, "cam01")
        self.assertEqual(cfgs[0].rtsp_url, "rtsp://192.168.1.100/live")
        self.assertEqual(cfgs[0].source_type, "rtsp")

    def test_legacy_rtsp_url_no_camera_subdirectory(self) -> None:
        """Legacy GN_RTSP_URL: paths are flat (no camera_id subdirectory)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({"GN_RTSP_URL": "rtsp://10.0.0.9/live"}):
                cfgs = load_capture_configs(base=base, seg_time=1)
        cfg = cfgs[0]
        self.assertEqual(cfg.clips_dir, base / "recorded_clips")
        self.assertEqual(cfg.queue_dir, base / "queue_raw")
        self.assertEqual(cfg.failed_dir_highlight, base / "failed_clips")

    def test_legacy_rtsp_url_default_segments(self) -> None:
        """GN_RTSP_URL with no segment overrides uses defaults: pre=6, post=3 (seg_time=1)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({
                "GN_RTSP_URL": "rtsp://10.0.0.1/live",
                "GN_RTSP_PRE_SEGMENTS": "",
                "GN_RTSP_POST_SEGMENTS": "",
            }):
                cfgs = load_capture_configs(base=base, seg_time=1)
        cfg = cfgs[0]
        self.assertEqual(cfg.pre_seconds, 6)
        self.assertEqual(cfg.post_seconds, 3)
        self.assertEqual(cfg.pre_segments, 6)
        self.assertEqual(cfg.post_segments, 3)

    def test_legacy_rtsp_url_custom_segments(self) -> None:
        """GN_RTSP_PRE_SEGMENTS and GN_RTSP_POST_SEGMENTS override defaults in legacy mode."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({
                "GN_RTSP_URL": "rtsp://10.0.0.1/live",
                "GN_RTSP_PRE_SEGMENTS": "10",
                "GN_RTSP_POST_SEGMENTS": "5",
            }):
                cfgs = load_capture_configs(base=base, seg_time=1)
        cfg = cfgs[0]
        self.assertEqual(cfg.pre_seconds, 10)
        self.assertEqual(cfg.post_seconds, 5)
        self.assertEqual(cfg.pre_segments, 10)
        self.assertEqual(cfg.post_segments, 5)

    # ------------------------------------------------------------------ priority / fallback chain

    def test_cameras_json_overrides_legacy_rtsp_url(self) -> None:
        """GN_CAMERAS_JSON takes precedence over GN_RTSP_URL."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({
                "GN_CAMERAS_JSON": '[{"id":"json_cam","rtsp_url":"rtsp://1.2.3.4/live","enabled":true}]',
                "GN_RTSP_URL": "rtsp://legacy/live",
            }):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].camera_id, "json_cam")
        self.assertEqual(cfgs[0].rtsp_url, "rtsp://1.2.3.4/live")

    def test_rtsp_urls_csv_overrides_legacy_rtsp_url(self) -> None:
        """GN_RTSP_URLS takes precedence over GN_RTSP_URL."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({
                "GN_RTSP_URLS": "rtsp://10.0.0.1/live",
                "GN_RTSP_URL": "rtsp://legacy/live",
            }):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].rtsp_url, "rtsp://10.0.0.1/live")

    # ------------------------------------------------------------------ GN_RTSP_URLS (CSV)

    def test_rtsp_urls_csv_single_url_no_isolation(self) -> None:
        """Single URL in GN_RTSP_URLS: flat paths (no camera_id subdirectory)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({"GN_RTSP_URLS": "rtsp://10.0.0.1/live"}):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].clips_dir, base / "recorded_clips")

    def test_rtsp_urls_csv_multiple_urls_isolated_dirs(self) -> None:
        """Multiple URLs in GN_RTSP_URLS: each camera gets its own subdirectory."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({"GN_RTSP_URLS": "rtsp://10.0.0.1/live,rtsp://10.0.0.2/live"}):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 2)
        self.assertEqual(cfgs[0].clips_dir, base / "recorded_clips" / "cam01")
        self.assertEqual(cfgs[1].clips_dir, base / "recorded_clips" / "cam02")

    # ------------------------------------------------------------------ GN_CAMERAS_JSON edge cases

    def test_cameras_json_disabled_camera_excluded(self) -> None:
        """Cameras with enabled:false are not included in the result."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({
                "GN_CAMERAS_JSON": (
                    '[{"id":"cam01","rtsp_url":"rtsp://10.0.0.1/live","enabled":true},'
                    '{"id":"cam02","rtsp_url":"rtsp://10.0.0.2/live","enabled":false}]'
                ),
            }):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].camera_id, "cam01")

    def test_cameras_json_single_camera_no_isolation(self) -> None:
        """Single enabled camera in JSON: flat paths (no camera_id subdirectory)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({
                "GN_CAMERAS_JSON": '[{"id":"cam01","rtsp_url":"rtsp://10.0.0.1/live","enabled":true}]',
            }):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].clips_dir, base / "recorded_clips")

    # ------------------------------------------------------------------ no env vars

    def test_no_env_vars_returns_v4l2_default(self) -> None:
        """When no RTSP env vars are set, a v4l2 default config is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self._patched({}):
                cfgs = load_capture_configs(base=base, seg_time=1)
        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].source_type, "v4l2")
        self.assertEqual(cfgs[0].camera_id, "cam01")


if __name__ == "__main__":
    unittest.main()
