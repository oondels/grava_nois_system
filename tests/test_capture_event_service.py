from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.services.mqtt.capture_event_service import (
    CaptureEventService,
    sign_capture_event_payload,
)


class _FakeMQTTClient:
    def __init__(self, publish_result: bool = True):
        self.publish_result = publish_result
        self.published: list[tuple[str, dict, bool]] = []

    def publish_json(self, topic, payload, *, retain=False):
        self.published.append((topic, payload, retain))
        return self.publish_result


class CaptureEventServiceTests(unittest.TestCase):
    def test_outbox_saturation_is_bounded_and_counted(self) -> None:
        client = _FakeMQTTClient(publish_result=False)
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "capture-outbox"
            service = CaptureEventService(
                client,
                topic="capture/events",
                device_id="edge",
                client_id=None,
                venue_id=None,
                device_secret="secret",
                agent_version="test",
                outbox_dir=outbox,
            )
            service.OUTBOX_LIMIT = 128
            service.CRITICAL_RESERVE = 32
            service._store({"event_id": "event-1", "severity": "warning", "data": "x" * 100})

            self.assertFalse((outbox / "event-1.json").exists())
            status = json.loads((outbox / "status").read_text())
            self.assertEqual(status["suppressed"], 1)
            self.assertTrue(status["saturated"])

    def test_camera_reconnecting_event_is_signed_and_published(self) -> None:
        client = _FakeMQTTClient()
        with tempfile.TemporaryDirectory() as tmp:
            service = CaptureEventService(
                client,
                topic="grn/devices/edge-01/capture/events",
                device_id="edge-01",
                client_id="client-01",
                venue_id="venue-01",
                device_secret="secret-01",
                agent_version="1.2.3",
                outbox_dir=Path(tmp),
            )

            service.publish_camera_reconnecting(
                camera_id="cam01",
                reason="FFmpeg indisponível",
                restart_attempts=2,
                ffmpeg_alive=False,
                buffer_status="NO_BUFFER",
                segment_age_sec=None,
                last_segment_at=None,
            )

        self.assertEqual(len(client.published), 1)
        topic, payload, retain = client.published[0]
        self.assertEqual(topic, "grn/devices/edge-01/capture/events")
        self.assertFalse(retain)
        self.assertEqual(payload["type"], "camera.reconnecting")
        self.assertEqual(payload["camera_status"], "RECONNECTING")
        self.assertEqual(payload["restart_attempts"], 2)
        self.assertEqual(
            payload["signature"],
            sign_capture_event_payload(payload=payload, device_secret="secret-01"),
        )


if __name__ == "__main__":
    unittest.main()
