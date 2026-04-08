from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config.config_loader import reset_config_cache
from src.config.settings import load_capture_configs


class MultiCameraSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_config_cache()

    def tearDown(self) -> None:
        reset_config_cache()

    def test_loads_multiple_cameras_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "GN_CAMERAS_JSON": (
                        '[{"id":"cam01","rtsp_url":"rtsp://10.0.0.1/live","enabled":true},'
                        '{"id":"cam02","rtsp_url":"rtsp://10.0.0.2/live","enabled":true}]'
                    )
                },
                clear=False,
            ):
                os.environ.pop("GN_RTSP_URLS", None)
                os.environ.pop("GN_RTSP_URL", None)
                cfgs = load_capture_configs(base=base, seg_time=1)

        self.assertEqual(len(cfgs), 2)
        self.assertEqual(cfgs[0].camera_id, "cam01")
        self.assertEqual(cfgs[1].camera_id, "cam02")
        self.assertEqual(cfgs[0].buffer_dir.name, "cam01")
        self.assertEqual(cfgs[1].buffer_dir.name, "cam02")

    def test_keeps_legacy_paths_for_single_legacy_rtsp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(
                os.environ, {"GN_RTSP_URL": "rtsp://10.0.0.9/live"}, clear=False
            ):
                os.environ.pop("GN_CAMERAS_JSON", None)
                os.environ.pop("GN_RTSP_URLS", None)
                cfgs = load_capture_configs(base=base, seg_time=1)

        self.assertEqual(len(cfgs), 1)
        self.assertEqual(cfgs[0].camera_id, "cam01")
        self.assertEqual(cfgs[0].buffer_dir.name, "grn_buffer")
        self.assertEqual(cfgs[0].clips_dir.name, "recorded_clips")


if __name__ == "__main__":
    unittest.main()
