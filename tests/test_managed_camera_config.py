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

    def test_two_cameras_keep_distinct_venues(self):
        venues = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
        configs = self._load({"version": 1, "cameras": [
            {"id": f"cam{i}", "venueId": venue, "rtspUrl": "rtsp://camera.example/live"}
            for i, venue in enumerate(venues)
        ]})
        self.assertEqual(venues, [cfg.venue_id for cfg in configs])

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

    def test_missing_url_skips_camera_without_legacy_fallback_or_secret_logging(self) -> None:
        for reference in (
            None, "", "env:GN_CAM01_RTSP_URL", "env:GN_RTSP_URL", "env:",
            "env:rtsp://user:super-secret@camera.example/live",
        ):
            for missing_value in ("", "   "):
                with self.subTest(reference=reference, missing_value=missing_value):
                    with patch("src.config.settings.logger") as logger:
                        configs = self._load(
                            {"cameras": [{"id": "cam01", "rtspUrl": reference}]},
                            {"GN_CAM01_RTSP_URL": missing_value, "GN_RTSP_URL": missing_value},
                        )
                    self.assertEqual([], configs)
                    logger.warning.assert_called_once()
                    logged = str(logger.warning.call_args)
                    self.assertIn("cam01", logged)
                    self.assertNotIn("super-secret", logged)

    def test_missing_camera_does_not_stop_healthy_camera_or_change_its_queue(self) -> None:
        configs = self._load({"cameras": [
            {"id": "cam01", "rtspUrl": "env:GN_CAM01_RTSP_URL"},
            {"id": "cam02", "rtspUrl": "rtsp://camera.example/live"},
        ]})

        self.assertEqual(["cam02"], [cfg.camera_id for cfg in configs])
        self.assertEqual("cam02", configs[0].queue_dir.name)
        self.assertEqual("cam02", configs[0].buffer_dir.name)

    def test_camera_is_loaded_after_missing_env_is_supplied(self) -> None:
        config = {"cameras": [{"id": "cam01", "rtspUrl": "env:GN_CAM01_RTSP_URL"}]}
        self.assertEqual([], self._load(config))
        configs = self._load(config, {"GN_CAM01_RTSP_URL": "rtsp://camera.example/live"})
        self.assertEqual(["cam01"], [cfg.camera_id for cfg in configs])

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
