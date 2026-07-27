from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.config.config_loader import reset_config_cache
from src.config.settings import CaptureConfig, load_capture_configs
from src.security.request_signer import sign_request
from src.services.api_error_policy import parse_api_error_from_response
from src.video.capture import start_ffmpeg
from src.video.processor import enqueue_clip

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_contracts.json"


class _Response:
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self._message = message
        self.text = ""

    def json(self) -> dict[str, object]:
        return {
            "success": False,
            "message": self._message,
            "error": {"code": "FIXTURE"},
            "requestId": "request-fixture",
        }


def _capture_config(base: Path, camera_id: str = "cam-a") -> CaptureConfig:
    return CaptureConfig(
        camera_id=camera_id,
        camera_name=camera_id,
        source_type="rtsp",
        rtsp_url="rtsp://user:pass@camera.example/live",
        buffer_dir=base / "buffer" / camera_id,
        clips_dir=base / "recorded_clips" / camera_id,
        queue_dir=base / "queue_raw" / camera_id,
        failed_dir_highlight=base / "failed_clips" / camera_id,
        seg_time=1,
        pre_seconds=5,
        post_seconds=3,
        pre_segments=5,
        post_segments=3,
    )


class LegacyContractCharacterizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = json.loads(FIXTURE.read_text())

    def setUp(self) -> None:
        reset_config_cache()

    def tearDown(self) -> None:
        reset_config_cache()

    def test_enqueue_sidecar_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _capture_config(Path(tmp))
            cfg.ensure_dirs()
            clip = cfg.clips_dir / "highlight_cam-a_contract.mp4"
            clip.touch()
            metadata = {
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
                "duration_sec": 8.5,
            }

            with (
                patch("src.video.processor.ffprobe_metadata", return_value=metadata),
                patch.dict(os.environ, {"GN_LIGHT_MODE": "0"}, clear=False),
            ):
                queued = enqueue_clip(cfg, clip)

            sidecar = json.loads(queued.with_suffix(".json").read_text())
            sidecar["created_at"] = "<TIMESTAMP>"

            self.assertEqual(sidecar, self.expected["enqueue_sidecar"])
            self.assertFalse(clip.exists())
            self.assertEqual(queued, cfg.queue_dir / clip.name)

    def test_api_retry_delete_policy_matrix(self) -> None:
        actual = []
        for case in self.expected["api_error_policy"]:
            info = parse_api_error_from_response(_Response(case["status_code"], case["message"]))
            self.assertIsNotNone(info)
            assert info is not None
            actual.append(
                {
                    "message": case["message"],
                    "status_code": info.status_code,
                    "delete_local_record": info.should_delete_local_record,
                }
            )
        self.assertEqual(actual, self.expected["api_error_policy"])

    def test_multi_camera_directory_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            buffer_root = base / "shared-buffer"
            env = {
                "GN_BUFFER_DIR": str(buffer_root),
                "GN_CAMERAS_JSON": json.dumps(
                    [
                        {"id": "north", "rtsp_url": "rtsp://north/live"},
                        {"id": "south", "rtsp_url": "rtsp://south/live"},
                    ]
                ),
                "GN_CONFIG_PATH": str(base / "missing.json"),
            }
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("GN_RTSP_URLS", None)
                os.environ.pop("GN_RTSP_URL", None)
                configs = load_capture_configs(base=base, seg_time=1)

            actual = [
                {
                    "camera_id": cfg.camera_id,
                    "buffer_dir": str(cfg.buffer_dir).replace(str(buffer_root), "<BUFFER>"),
                    "clips_dir": str(cfg.clips_dir).replace(str(base), "<BASE>"),
                    "queue_dir": str(cfg.queue_dir).replace(str(base), "<BASE>"),
                    "failed_dir": str(cfg.failed_dir_highlight).replace(str(base), "<BASE>"),
                }
                for cfg in configs
            ]
        self.assertEqual(actual, self.expected["multi_camera_paths"])

    def test_request_signature_snapshot(self) -> None:
        signed = sign_request(
            method="POST",
            path="/api/videos/clip123/uploaded",
            body_string='{"size_bytes":10,"sha256":"abc"}',
            device_id="device-01",
            device_secret="super-secret",
            client_id="client-01",
            timestamp="1700000000",
            nonce="11111111-2222-4333-8444-555555555555",
        )
        actual = {
            "body_sha256": signed.body_sha256,
            "canonical_string": signed.canonical_string,
            "headers": signed.headers,
        }
        self.assertEqual(actual, self.expected["signed_request"])

    def test_default_rtsp_ffmpeg_command_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = _capture_config(base)
            cfg.ensure_dirs()
            fake_process = MagicMock()
            fake_process.poll.return_value = None
            clean_env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GN_RTSP_") and key != "GN_LIGHT_MODE"
            }
            clean_env.update(
                {
                    "GN_CONFIG_PATH": str(base / "missing.json"),
                    "GN_LOG_DIR": str(base / "logs"),
                    "GN_FFMPEG_STARTUP_CHECK_SEC": "0.1",
                }
            )
            with (
                patch.dict(os.environ, clean_env, clear=True),
                patch("src.video.capture.check_rtsp_connectivity", return_value=True),
                patch("src.video.capture.time.sleep"),
                patch(
                    "src.video.capture.subprocess.Popen",
                    return_value=fake_process,
                ) as popen,
            ):
                start_ffmpeg(cfg)

            command = list(popen.call_args.args[0])
            normalized = [
                item.replace(cfg.rtsp_url or "", "<RTSP_URL>").replace(
                    str(cfg.buffer_dir), "<BUFFER>"
                )
                for item in command
            ]
        self.assertEqual(normalized, self.expected["rtsp_hq_command"])


if __name__ == "__main__":
    unittest.main()
