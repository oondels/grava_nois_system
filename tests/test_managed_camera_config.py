from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config.config_loader import reset_config_cache
from src.config.settings import load_capture_configs


class ManagedCameraConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_config_cache()

    def _load(self, config: dict, env: dict[str, str] | None = None):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path = base / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            values = {
                "GN_CONFIG_PATH": str(config_path),
                "GN_CAMERAS_JSON": ('[{"id":"legacy","rtsp_url":"rtsp://legacy.example/live"}]'),
                "GN_RTSP_URLS": "rtsp://legacy.example/one,rtsp://legacy.example/two",
                "GN_RTSP_URL": "rtsp://legacy.example/live",
            }
            values.update(env or {})
            reset_config_cache()
            with patch.dict(os.environ, values, clear=True):
                return load_capture_configs(base=base, seg_time=1)

    def test_empty_managed_array_disables_all_sources_without_legacy_fallback(self) -> None:
        self.assertEqual([], self._load({"version": 1, "cameras": []}))

    def test_all_managed_cameras_disabled_do_not_fall_back_to_legacy_env(self) -> None:
        configs = self._load(
            {
                "version": 1,
                "cameras": [
                    {
                        "id": "cam01",
                        "enabled": False,
                        "sourceType": "rtsp",
                        "rtspUrl": "rtsp://managed.example/live",
                    }
                ],
            }
        )

        self.assertEqual([], configs)

    def test_missing_env_reference_fails_without_exposing_a_secret(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "camera gerenciada 'cam01'.*GN_CAM01_RTSP_URL",
        ) as raised:
            self._load(
                {
                    "version": 1,
                    "cameras": [
                        {
                            "id": "cam01",
                            "sourceType": "rtsp",
                            "rtspUrl": "env:GN_CAM01_RTSP_URL",
                        }
                    ],
                }
            )

        self.assertNotIn("super-secret", str(raised.exception))

    def test_env_reference_is_resolved_for_managed_camera(self) -> None:
        configs = self._load(
            {
                "version": 1,
                "cameras": [
                    {
                        "id": "cam01",
                        "sourceType": "rtsp",
                        "rtspUrl": "env:GN_CAM01_RTSP_URL",
                    }
                ],
            },
            {"GN_CAM01_RTSP_URL": "rtsp://user:super-secret@camera.example/live"},
        )

        self.assertEqual(1, len(configs))
        self.assertEqual("rtsp://user:super-secret@camera.example/live", configs[0].rtsp_url)

    def test_absent_cameras_field_keeps_legacy_fallback(self) -> None:
        configs = self._load({"version": 1})

        self.assertEqual("legacy", configs[0].camera_id)

    def test_null_cameras_field_is_rejected_and_does_not_become_managed(self) -> None:
        configs = self._load({"version": 1, "cameras": None})

        self.assertEqual("legacy", configs[0].camera_id)


if __name__ == "__main__":
    unittest.main()
