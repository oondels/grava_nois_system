"""DEV camera opt-out without hardware, credentials, or external services."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import main as runtime
from src.config.config_loader import reset_config_cache
from src.config.settings import load_capture_configs


class DevCameraModeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(reset_config_cache)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config_path = self.base / "config.json"
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "GN_CONFIG_PATH": str(self.config_path),
                    "DEV": "true",
                },
                clear=True,
            )
        )
        reset_config_cache()

    def load(self, config, **env):
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        reset_config_cache()
        with patch.dict(os.environ, env):
            return load_capture_configs(self.base, seg_time=1)

    def test_disabled_skips_all_sources_and_missing_credentials(self):
        sources = [
            ({"cameras": [{"id": "managed", "rtspUrl": "env:MISSING_CAMERA_URL"}]}, {}),
            ({"cameras": [{"id": "webcam", "sourceType": "v4l2"}]}, {}),
            ({}, {"GN_CAMERAS_JSON": "invalid JSON is not read"}),
            ({}, {"GN_RTSP_URLS": "rtsp://camera.example/one,rtsp://camera.example/two"}),
            ({}, {"GN_RTSP_URL": "rtsp://camera.example/live"}),
            ({}, {}),  # No implicit V4L2 fallback.
        ]
        for config, env in sources:
            with self.subTest(config=config, env=env):
                self.assertEqual([], self.load(config, DEV_USE_CAMERA="false", **env))

    def test_default_and_explicit_enable_preserve_camera_selection(self):
        config = {"cameras": [{"id": "webcam", "sourceType": "v4l2"}]}
        for env in ({}, {"DEV_USE_CAMERA": "true"}, {"DEV_USE_CAMERA": "1"}):
            with self.subTest(env=env):
                cameras = self.load(config, **env)
                self.assertEqual(["webcam"], [cam.camera_id for cam in cameras])
                self.assertEqual("v4l2", cameras[0].source_type)

    def test_disabled_flag_is_ignored_outside_dev(self):
        for dev in ("false", "0", ""):
            with self.subTest(dev=dev):
                cameras = self.load({}, DEV=dev, DEV_USE_CAMERA="false")
                self.assertEqual(1, len(cameras))
                self.assertEqual("v4l2", cameras[0].source_type)
        os.environ.pop("DEV")
        self.assertEqual(1, len(self.load({}, DEV_USE_CAMERA="false")))

    def test_boolean_spellings_match_dev_mode(self):
        for dev in ("1", "true", "yes", "y", "on", " TRUE "):
            for flag in ("0", "false", "off", " FALSE "):
                with self.subTest(dev=dev, flag=flag):
                    self.assertEqual([], self.load({}, DEV=dev, DEV_USE_CAMERA=flag))

    def test_enabled_flag_respects_managed_disabled_cameras(self):
        for cameras in ([], [{"id": "webcam", "sourceType": "v4l2", "enabled": False}]):
            with self.subTest(cameras=cameras):
                self.assertEqual([], self.load({"cameras": cameras}, DEV_USE_CAMERA="true"))

    def test_enabled_still_validates_rtsp_credentials(self):
        with self.assertRaisesRegex(ValueError, "MISSING_CAMERA_URL"):
            self.load(
                {"cameras": [{"id": "managed", "rtspUrl": "env:MISSING_CAMERA_URL"}]},
                DEV_USE_CAMERA="true",
            )

    def test_bootstrap_keeps_mqtt_without_starting_camera_or_touching_pending_clip(self):
        self.config_path.write_text(
            json.dumps(
                {
                    "cameras": [{"id": "managed", "rtspUrl": "env:MISSING_CAMERA_URL"}],
                    "mqtt": {"enabled": True, "broker": {"host": "localhost"}},
                }
            ),
            encoding="utf-8",
        )
        os.environ.update(
            {
                "DEV_USE_CAMERA": "false",
                "GN_VENUE_ID": "test-venue",
                "DEVICE_ID": "test-device",
            }
        )
        pending = self.base / "queue_raw" / "managed" / "pending.mp4"
        pending.parent.mkdir(parents=True)
        pending.write_bytes(b"pending clip")
        self.stack.enter_context(patch.object(runtime, "__file__", str(self.base / "main.py")))
        self.stack.enter_context(
            patch.object(runtime, "apply_pending_config_on_startup", return_value=None)
        )
        self.stack.enter_context(
            patch.object(runtime, "resolve_trigger_source", return_value="none")
        )
        mocks = {}
        for name in (
            "MQTTClient",
            "DevicePresenceService",
            "CommandDispatcher",
            "DeviceConfigService",
            "DeviceEnvService",
            "CaptureEventService",
            "DeviceDiagnosticEventService",
            "DockerActionRequestService",
            "ProcessingWorker",
            "start_ffmpeg",
            "clear_buffer",
            "SegmentBuffer",
            "logger",
        ):
            mocks[name] = self.stack.enter_context(patch.object(runtime, name))
        threads = self.stack.enter_context(patch.object(runtime.threading, "Thread"))
        trigger_queue = self.stack.enter_context(patch.object(runtime.queue, "Queue"))
        trigger_queue.return_value.get.side_effect = KeyboardInterrupt

        self.assertEqual(0, runtime.main())

        mocks["DevicePresenceService"].return_value.start.assert_called_once()
        snapshot = mocks["DevicePresenceService"].call_args.kwargs["runtime_snapshot_provider"]()
        self.assertEqual([], snapshot["cameras"])
        self.assertEqual(
            ["trigger-enter"], [call.kwargs["name"] for call in threads.call_args_list]
        )
        for name in ("ProcessingWorker", "start_ffmpeg", "clear_buffer", "SegmentBuffer"):
            mocks[name].assert_not_called()
        self.assertEqual(b"pending clip", pending.read_bytes())
        mocks["logger"].info.assert_any_call(
            "Serviço ativo sem câmeras; gatilhos não geram clipes (Ctrl+C sai)"
        )


if __name__ == "__main__":
    unittest.main()
