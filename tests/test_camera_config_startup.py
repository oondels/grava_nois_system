"""Camera configuration failures must not block presence or the main loop."""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import main as runtime
from src.config.config_loader import reset_config_cache


class CameraConfigStartupTests(unittest.TestCase):
    def _boot(self, config, env, expected_cameras):
        with ExitStack() as stack:
            stack.callback(reset_config_cache)
            base = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            config_path = base / "config.json"
            config_path.write_text(json.dumps({
                **config,
                "mqtt": {"enabled": True, "broker": {"host": "localhost"}},
            }), encoding="utf-8")
            stack.enter_context(patch.dict(os.environ, {
                "GN_CONFIG_PATH": str(config_path),
                "GN_BUFFER_DIR": str(base / "buffer"),
                "GN_RUNTIME_CONFIG_DIR": str(base / "runtime"),
                "GN_VENUE_ID": "test-venue",
                "DEVICE_ID": "test-device",
                "DEV": "false",
                **env,
            }, clear=True))
            reset_config_cache()
            pending = base / "queue_raw" / "cam01" / "pending.mp4"
            pending.parent.mkdir(parents=True)
            pending.write_bytes(b"pending clip")
            stack.enter_context(patch.object(runtime, "__file__", str(base / "main.py")))
            stack.enter_context(patch.object(runtime, "apply_pending_config_on_startup", return_value=None))
            stack.enter_context(patch.object(runtime, "resolve_trigger_source", return_value="none"))
            mocks = {}
            for name in (
                "MQTTClient", "DevicePresenceService", "CommandDispatcher",
                "DeviceConfigService", "DeviceEnvService", "CaptureEventService",
                "DeviceDiagnosticEventService", "DockerActionRequestService",
                "ProcessingWorker", "start_ffmpeg", "clear_buffer", "SegmentBuffer",
            ):
                mocks[name] = stack.enter_context(patch.object(runtime, name))
            threads = stack.enter_context(patch.object(runtime.threading, "Thread"))
            trigger_queue = stack.enter_context(patch.object(runtime.queue, "Queue"))
            # Exit only once bootstrap has reached the live trigger loop.
            trigger_queue.return_value.get.side_effect = KeyboardInterrupt
            logger = stack.enter_context(patch("src.config.settings.logger"))

            self.assertEqual(0, runtime.main())
            trigger_queue.return_value.get.assert_called_once()
            mocks["DevicePresenceService"].return_value.start.assert_called_once()
            mocks["DeviceConfigService"].return_value.start.assert_called_once()
            mocks["DeviceEnvService"].return_value.start.assert_called_once()
            snapshot = mocks["DevicePresenceService"].call_args.kwargs["runtime_snapshot_provider"]()
            self.assertEqual(expected_cameras, [cam["camera_id"] for cam in snapshot["cameras"]])
            supervisors = [
                call.kwargs["name"] for call in threads.call_args_list
                if call.kwargs["name"].startswith("supervisor-")
            ]
            self.assertEqual([f"supervisor-{cam}" for cam in expected_cameras], supervisors)
            mocks["start_ffmpeg"].assert_not_called()
            self.assertEqual(b"pending clip", pending.read_bytes())
            self.assertNotIn("super-secret", str(logger.mock_calls))
            if not expected_cameras:
                mocks["ProcessingWorker"].assert_not_called()
                mocks["clear_buffer"].assert_not_called()

    def test_missing_managed_camera_urls_keep_presence_and_admin_services(self):
        for reference in (None, "", "env:GN_CAM01_RTSP_URL", "env:GN_RTSP_URL"):
            for env in ({}, {"GN_CAM01_RTSP_URL": " ", "GN_RTSP_URL": " "}):
                with self.subTest(reference=reference, env=env):
                    self._boot({"cameras": [{"id": "cam01", "rtspUrl": reference}]}, env, [])

    def test_bad_camera_does_not_prevent_healthy_camera_bootstrap(self):
        self._boot({"cameras": [
            {"id": "cam01", "rtspUrl": "env:GN_CAM01_RTSP_URL"},
            {"id": "cam02", "rtspUrl": "rtsp://camera.example/live"},
        ]}, {}, ["cam02"])

    def test_invalid_legacy_json_keeps_device_services_without_leaking_payload(self):
        for raw in ('[{"rtsp_url":"rtsp://user:super-secret@camera/live"}',
                    '{"rtsp_url":"rtsp://user:super-secret@camera/live"}', "null"):
            with self.subTest(raw=raw):
                self._boot({}, {"GN_CAMERAS_JSON": raw}, [])

    def test_missing_legacy_variables_preserve_nonblocking_local_camera_fallback(self):
        for env in ({}, {"GN_CAMERAS_JSON": " ", "GN_RTSP_URL": " ", "GN_CAM01_RTSP_URL": " "}):
            with self.subTest(env=env):
                self._boot({}, env, ["cam01"])

    def test_legacy_json_ignores_camera_without_url_and_starts_healthy_camera(self):
        self._boot({}, {"GN_CAMERAS_JSON": json.dumps([
            {"id": "cam01"},
            {"id": "cam02", "rtsp_url": "rtsp://camera.example/live"},
        ])}, ["cam02"])


if __name__ == "__main__":
    unittest.main()
