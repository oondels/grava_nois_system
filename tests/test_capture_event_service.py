from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.mqtt.capture_event_service import (
    CaptureEventService,
    sign_capture_event_payload,
)


class _Receipt:
    acknowledged = False

    def is_published(self):
        return self.acknowledged


class _FakeMQTTClient:
    def __init__(self, publish_result: bool = True):
        self.publish_result = publish_result
        self.is_connected = True
        self.receipts = []
        self.published: list[tuple[str, dict, bool]] = []

    def publish_json_tracked(self, topic, payload, *, retain=False, qos=None):
        assert qos == 1
        self.published.append((topic, payload, retain))
        if not self.publish_result:
            return None
        receipt = _Receipt()
        self.receipts.append(receipt)
        return receipt


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

            service.flush_outbox()

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


def _service(path, client, clock):
    return CaptureEventService(
        client,
        topic="test/capture/events",
        device_id="edge",
        client_id=None,
        venue_id=None,
        device_secret="test-secret",
        agent_version="test",
        outbox_dir=path,
        clock=lambda: clock[0],
    )


def _failure(service, reason="unavailable", camera="cam01"):
    service.publish_camera_restart_failed(
        camera_id=camera,
        reason=reason,
        restart_attempts=1,
        buffer_status="EMPTY",
        segment_age_sec=None,
        last_segment_at=None,
    )


def test_offline_repetitions_do_not_scan_or_publish(tmp_path):
    client, clock = _FakeMQTTClient(), [0.0]
    client.is_connected = False
    service = _service(tmp_path, client, clock)
    _failure(service)
    with patch.object(Path, "glob", side_effect=AssertionError("outbox rescanned")):
        for _ in range(100):
            _failure(service)
            service.flush_outbox()
    assert client.published == []
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert json.loads((tmp_path / "status").read_text())["coalesced"] == 100
    clock[0] = 300
    _failure(service)
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_changed_reason_other_camera_and_recovery_are_preserved(tmp_path):
    client, clock = _FakeMQTTClient(), [0.0]
    service = _service(tmp_path, client, clock)
    _failure(service)
    _failure(service, reason="different failure")
    _failure(service, camera="cam02")
    service.publish_camera_reconnected(camera_id="cam01", reason="recovered", restart_attempts=1)
    _failure(service, reason="different failure")
    assert len(list(tmp_path.glob("*.json"))) == 5
    assert client.published == []  # Producers only persist; network work is isolated.


def test_legacy_backlog_is_paced_and_ordered_by_occurrence(tmp_path):
    # Deliberately reverse UUID filename order relative to occurred_at.
    for i in range(200):
        (tmp_path / f"{200 - i:04}.json").write_text(
            json.dumps(
                {
                    "event_id": f"old-{i}",
                    "type": "camera.restart_failed",
                    "camera_id": "cam01",
                    "occurred_at": f"2026-10-15T10:{i // 60:02}:{i % 60:02}+00:00",
                }
            )
        )
    client, clock = _FakeMQTTClient(), [0.0]
    service = _service(tmp_path, client, clock)
    service.flush_outbox()
    assert len(client.published) == 1
    assert client.published[0][1]["event_id"] == "old-0"
    assert len(list(tmp_path.glob("*.json"))) == 200  # Until PUBACK, retain the file.
    client.receipts[0].acknowledged = True
    for _ in range(50):
        service.flush_outbox()
    assert len(client.published) == 1
    assert len(list(tmp_path.glob("*.json"))) == 199
    clock[0] = 1
    service.flush_outbox()
    assert len(client.published) == 2
    assert client.published[1][1]["event_id"] == "old-1"


def test_no_ack_or_reconnection_never_enqueues_another_copy(tmp_path):
    client, clock = _FakeMQTTClient(), [0.0]
    service = _service(tmp_path, client, clock)
    _failure(service)
    service.flush_outbox()
    for i in range(20):
        clock[0] += 10
        client.is_connected = i % 2 == 0
        service.flush_outbox()
    assert len(client.published) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    client.receipts[0].acknowledged = True
    service.flush_outbox()
    assert not list(tmp_path.glob("*.json"))


def test_unacknowledged_event_survives_process_restart_with_same_signature(tmp_path):
    client, clock = _FakeMQTTClient(), [0.0]
    first = _service(tmp_path, client, clock)
    _failure(first)
    first.flush_outbox()
    original = client.published[0][1]
    second_client = _FakeMQTTClient()
    restarted = _service(tmp_path, second_client, clock)
    restarted.flush_outbox()
    assert second_client.published[0][1] == original
    assert original["signature"] == sign_capture_event_payload(
        payload=original, device_secret="test-secret"
    )


def test_publish_failure_retains_file_and_waits_before_retry(tmp_path):
    client, clock = _FakeMQTTClient(publish_result=False), [0.0]
    service = _service(tmp_path, client, clock)
    _failure(service)
    for _ in range(20):
        service.flush_outbox()
    assert len(client.published) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1
    clock[0] = 1
    client.publish_result = True
    service.flush_outbox()
    assert len(client.published) == 2
    assert client.published[0][1] == client.published[1][1]


def test_trigger_rejections_are_not_coalesced(tmp_path):
    service = _service(tmp_path, _FakeMQTTClient(), [0.0])
    for trigger in ["trigger-1", "trigger-2"]:
        service.publish_trigger_rejected(
            camera_id="cam01",
            trigger_id=trigger,
            trigger_source="pico",
            reason="unavailable",
            camera_status="UNAVAILABLE",
            ffmpeg_alive=False,
            buffer_status="EMPTY",
            segment_age_sec=None,
            last_segment_at=None,
        )
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_sender_starts_once_and_stops(tmp_path):
    import threading

    tick = threading.Event()
    service = _service(tmp_path, _FakeMQTTClient(), [0.0])
    with patch.object(service, "flush_outbox", side_effect=lambda: tick.set()):
        try:
            service.start()
            thread = service._thread
            service.start()
            assert service._thread is thread
            assert tick.wait(2)
        finally:
            service.stop()
        assert not thread.is_alive()
