"""Camera origin survives durable queues and never follows current config."""
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from src.config.settings import CaptureConfig
from src.video.processor import enqueue_clip
from src.domain.capture import CameraId
from src.domain.delivery import ClipJob
from src.infrastructure.http.deferred_gateway import DeferredVideoGateway
from src.infrastructure.filesystem.deferred_repository import DeferredJobRepository


class CameraVenueDeliveryTests(unittest.TestCase):
    def test_immediate_queue_snapshots_camera_and_venue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            queue.mkdir()
            clip = root / "clip.mp4"
            clip.write_bytes(b"fake-test-artifact")
            cfg = CaptureConfig("cam2", root, root, queue, root, venue_id="court-b")
            with patch("src.video.processor.ffprobe_metadata", return_value={}), patch(
                "src.video.processor._sha256_file", return_value="a" * 64
            ):
                enqueue_clip(cfg, clip)
            payload = json.loads((queue / "clip.json").read_text())
            self.assertEqual("cam2", payload["camera_id"])
            self.assertEqual("court-b", payload["capture_venue_id"])
            self.assertTrue(payload["camera_assignment_enabled"])

    def test_deferred_restart_uses_original_court_after_config_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = DeferredJobRepository(Path(tmp))
            job = ClipJob("job", CameraId("cam2"), "artifacts/raw.mp4", datetime.now(UTC),
                artifact_location="artifacts/final.mp4", schema_version=3, details={
                    "device_id": "d1", "client_id": "c1", "venue_id": "principal",
                    "capture_venue_id": "original-court", "camera_assignment_enabled": True,
                    "captured_at": "2026-01-01T12:00:00Z", "policy": {},
                })
            jobs.save(job)
            client = Mock(device_id="d1", client_id="c1", venue_id="principal")
            client.is_configured.return_value = True
            client.extract_clip_registration.return_value = {"clip_id": "remote", "upload_url": "test"}
            gateway = DeferredVideoGateway(jobs, client)
            with patch("src.infrastructure.http.deferred_gateway._sha256_file", return_value="a" * 64):
                gateway.register(jobs.get("job"), {"duration_sec": 2})
            payload = client.register_clip_metadados.call_args.args[0]
            self.assertEqual("original-court", payload["venue_id"])
            self.assertEqual("cam2", payload["camera_id"])
