from __future__ import annotations

import unittest

from src.config.settings import MQTTConfig
from src.services.mqtt.device_presence_service import (
    DevicePresenceService,
    build_runtime_snapshot,
)


class _FakeLock:
    def __init__(self, locked: bool = False):
        self._locked = locked

    def locked(self) -> bool:
        return self._locked


class _FakeProc:
    def __init__(self, alive: bool = True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


class _FakeCfg:
    def __init__(self, camera_id: str, queue_dir):
        self.camera_id = camera_id
        self.camera_name = camera_id.upper()
        self.source_type = "rtsp"
        self.queue_dir = queue_dir


class _FakeRuntime:
    def __init__(self, camera_id: str, queue_dir, *, alive: bool = True, busy: bool = False):
        self.cfg = _FakeCfg(camera_id, queue_dir)
        self.proc = _FakeProc(alive=alive)
        self.capture_lock = _FakeLock(locked=busy)


class _FakeMQTTClient:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.published: list[tuple[str, dict, bool]] = []
        self.last_will = None
        self.connect_listeners = []
        self.is_enabled = True

    def add_on_connect_listener(self, callback):
        self.connect_listeners.append(callback)

    def configure_last_will(self, topic, payload, *, retain):
        self.last_will = (topic, payload, retain)

    def start(self):
        self.started = True
        for callback in self.connect_listeners:
            callback()
        return True

    def publish_json(self, topic, payload, *, retain=False, qos=None):
        _ = qos
        self.published.append((topic, payload, retain))
        return True

    def stop(self):
        self.stopped = True


class DevicePresenceServiceTests(unittest.TestCase):
    def _config(self) -> MQTTConfig:
        return MQTTConfig(
            enabled=True,
            host="broker.example.com",
            port=1883,
            username=None,
            password=None,
            client_id="edge-01",
            keepalive=60,
            heartbeat_interval_sec=30,
            topic_prefix="grn",
            qos=1,
            retain_presence=True,
            use_tls=False,
            agent_version="1.2.3",
        )

    def test_publish_online_and_offline_payloads(self) -> None:
        client = _FakeMQTTClient()
        service = DevicePresenceService(
            client,
            self._config(),
            device_id="edge-01",
            client_id="client-01",
            venue_id="venue-01",
            runtime_snapshot_provider=lambda: {
                "queue_size": 3,
                "health": {"camera_count": 1},
                "cameras": [{"camera_id": "cam01"}],
                "runtime": {"light_mode": True},
            },
        )

        started = service.start()
        service.stop()

        self.assertTrue(started)
        self.assertIsNotNone(client.last_will)
        self.assertGreaterEqual(len(client.published), 3)

        presence_online = client.published[0]
        self.assertEqual(presence_online[0], "grn/devices/edge-01/presence")
        self.assertEqual(presence_online[1]["status"], "online")
        self.assertEqual(presence_online[1]["queue_size"], 3)
        self.assertTrue(presence_online[2])

        state_payload = client.published[1]
        self.assertEqual(state_payload[0], "grn/devices/edge-01/state")
        self.assertEqual(state_payload[1]["runtime"]["light_mode"], True)

        offline_payload = client.published[-1]
        self.assertEqual(offline_payload[1]["status"], "offline")
        self.assertEqual(offline_payload[1]["disconnect_reason"], "clean_shutdown")
        self.assertTrue(client.stopped)

    def test_build_runtime_snapshot_summarizes_camera_health(self) -> None:
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cam01_queue = base / "cam01"
            cam02_queue = base / "cam02"
            cam01_queue.mkdir()
            cam02_queue.mkdir()
            (cam01_queue / "clip1.mp4").write_bytes(b"1")
            (cam01_queue / "clip2.mp4").write_bytes(b"2")
            (cam02_queue / "clip3.mp4").write_bytes(b"3")

            snapshot = build_runtime_snapshot(
                runtimes=[
                    _FakeRuntime("cam01", cam01_queue, alive=True, busy=True),
                    _FakeRuntime("cam02", cam02_queue, alive=False, busy=False),
                ],
                light_mode=False,
                dev_mode=True,
                trigger_source="pico",
            )

        self.assertEqual(snapshot["queue_size"], 3)
        self.assertEqual(snapshot["health"]["camera_count"], 2)
        self.assertEqual(snapshot["health"]["online_cameras"], 1)
        self.assertEqual(snapshot["runtime"]["light_mode"], False)
        self.assertEqual(snapshot["runtime"]["dev_mode"], True)


if __name__ == "__main__":
    unittest.main()
