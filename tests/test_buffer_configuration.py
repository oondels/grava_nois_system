import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config.config_loader import (
    _apply_json,
    _build_from_env,
    reset_config_cache,
)
from src.config.config_schema import validate_config_dict
from src.config.settings import load_capture_configs


class BufferConfigurationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_config_cache()

    def test_schema_rejects_buffer_smaller_than_replay_window(self) -> None:
        errors = validate_config_dict(
            {
                "capture": {
                    "segmentSeconds": 2,
                    "preSegments": 6,
                    "postSegments": 3,
                    "bufferSeconds": 20,
                }
            }
        )
        self.assertTrue(any("deve ser >= 22" in error for error in errors))

    def test_schema_accounts_for_per_camera_window_override(self) -> None:
        errors = validate_config_dict(
            {
                "capture": {
                    "segmentSeconds": 2,
                    "preSegments": 6,
                    "postSegments": 3,
                    "bufferSeconds": 30,
                },
                "cameras": [{"id": "cam01", "preSegments": 12, "postSegments": 4}],
            }
        )
        self.assertTrue(any("deve ser >= 36" in error for error in errors))

    def test_json_buffer_overrides_legacy_environment(self) -> None:
        with patch.dict(os.environ, {"GN_MAX_BUFFER_SECONDS": "50"}, clear=True):
            config = _apply_json(_build_from_env(), {"capture": {"bufferSeconds": 60}})
        self.assertEqual(60, config.capture.buffer_seconds)

    def test_default_buffer_grows_with_replay_window(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "GN_SEG_TIME": "4",
                    "GN_RTSP_PRE_SEGMENTS": "8",
                    "GN_RTSP_POST_SEGMENTS": "4",
                    "GN_RTSP_URL": "rtsp://camera/live",
                },
                clear=True,
            ),
        ):
            configs = load_capture_configs(Path(tmp), seg_time=4)
        self.assertEqual(56, configs[0].max_buffer_seconds)

    def test_legacy_override_smaller_than_window_is_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "GN_SEG_TIME": "2",
                    "GN_RTSP_PRE_SEGMENTS": "8",
                    "GN_RTSP_POST_SEGMENTS": "4",
                    "GN_MAX_BUFFER_SECONDS": "20",
                    "GN_RTSP_URL": "rtsp://camera/live",
                },
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "deve ser >= 28s"),
        ):
            load_capture_configs(Path(tmp), seg_time=2)


if __name__ == "__main__":
    unittest.main()
