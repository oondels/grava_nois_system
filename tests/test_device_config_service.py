from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.services.mqtt.device_config_service import (
    DeviceConfigService,
    hash_config,
    sign_desired_config_payload,
    sign_reported_config_payload,
)


class _FakeMQTTClient:
    def __init__(self):
        self.is_enabled = True
        self.subscriptions = []
        self.published = []

    def subscribe(self, topic, handler, *, qos=None):
        _ = qos
        self.subscriptions.append((topic, handler))
        return True

    def publish_json(self, topic, payload, *, retain=False, qos=None):
        _ = retain, qos
        self.published.append((topic, payload))
        return True


def _deep_update(target: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


class DeviceConfigServiceTests(unittest.TestCase):
    def _service(self, base: Path, client: _FakeMQTTClient | None = None) -> DeviceConfigService:
        return DeviceConfigService(
            client or _FakeMQTTClient(),
            device_id="edge-01",
            client_id="client-01",
            venue_id="venue-01",
            desired_topic="grn/devices/edge-01/config/desired",
            reported_topic="grn/devices/edge-01/config/reported",
            config_path=base / "config.json",
            device_secret="secret-123",
            agent_version="1.2.3",
        )

    def _payload(
        self,
        desired_config: dict,
        *,
        version: int = 2,
        issued_at: str | None = None,
        expires_at: str | None = None,
        device_secret: str = "secret-123",
    ) -> dict:
        issued = issued_at or datetime.now(timezone.utc).isoformat()
        expires = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        prepared = {
            **desired_config,
            "version": version,
            "updatedAt": issued,
        }
        payload = {
            "type": "config.desired",
            "device_id": "edge-01",
            "client_id": "client-01",
            "venue_id": "venue-01",
            "schema_version": 1,
            "config_version": version,
            "desired_hash": hash_config(prepared),
            "correlation_id": "corr-01",
            "issued_at": issued,
            "expires_at": expires,
            "desired_config": desired_config,
        }
        payload["signature"] = sign_desired_config_payload(
            payload=payload,
            device_secret=device_secret,
        )
        return payload

    def _desired_config(self, overrides: dict | None = None) -> dict:
        config = {
            "capture": {
                "segmentSeconds": 1,
                "preSegments": 6,
                "postSegments": 3,
                "rtsp": {
                    "maxRetries": 10,
                    "timeoutSeconds": 5,
                    "startupCheckSeconds": 1.0,
                    "reencode": True,
                    "fps": "",
                    "gop": 25,
                    "preset": "veryfast",
                    "crf": 23,
                    "useWallclockTimestamps": False,
                },
                "v4l2": {
                    "device": "/dev/video0",
                    "framerate": 30,
                    "videoSize": "1280x720",
                },
            },
            "cameras": [],
            "triggers": {
                "source": "auto",
                "maxWorkers": None,
                "pico": {"globalToken": "BTN_REPLAY"},
                "gpio": {"pin": None, "debounceMs": 300, "cooldownSeconds": 120},
            },
            "processing": {
                "lightMode": False,
                "maxAttempts": 3,
                "mobileFormat": True,
                "verticalFormat": True,
                "watermark": {
                    "preset": "veryfast",
                    "relativeWidth": 0.18,
                    "opacity": 0.8,
                    "margin": 24,
                },
            },
            "operationWindow": {
                "timeZone": "America/Sao_Paulo",
                "start": "07:00",
                "end": "23:30",
            },
            "mqtt": {
                "enabled": False,
                "broker": {"host": "", "port": 1883, "tls": False},
                "keepaliveSeconds": 60,
                "heartbeatIntervalSeconds": 30,
                "topicPrefix": "grn",
                "qos": 1,
                "retainPresence": True,
            },
        }
        if overrides:
            _deep_update(config, overrides)
        return config

    def test_start_subscribes_to_config_desired_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeMQTTClient()
            service = self._service(Path(tmp), client)

            self.assertTrue(service.start())

        self.assertEqual(len(client.subscriptions), 1)
        self.assertEqual(
            client.subscriptions[0][0],
            "grn/devices/edge-01/config/desired",
        )

    def test_applies_hot_reload_safe_config_and_reports_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = _FakeMQTTClient()
            (base / "config.json").write_text(
                json.dumps(self._desired_config()),
                encoding="utf-8",
            )
            service = self._service(base, client)
            payload = self._payload(
                self._desired_config(
                    {"operationWindow": {"start": "08:00", "end": "22:00"}}
                )
            )

            result = service.process_desired_config(payload)
            service.publish_report(result)

            config_data = json.loads((base / "config.json").read_text(encoding="utf-8"))
            state_data = json.loads((base / "config.state.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "applied")
        self.assertFalse((base / "config.pending.json").exists())
        self.assertEqual(config_data["operationWindow"]["start"], "08:00")
        self.assertEqual(state_data["lastAppliedVersion"], 2)
        self.assertEqual(client.published[-1][0], "grn/devices/edge-01/config/reported")
        self.assertEqual(client.published[-1][1]["status"], "applied")
        self.assertEqual(client.published[-1][1]["signature_version"], "hmac-sha256-v1")
        self.assertEqual(
            client.published[-1][1]["signature"],
            sign_reported_config_payload(
                payload=client.published[-1][1],
                device_secret="secret-123",
            ),
        )

    def test_restart_changes_are_kept_pending_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            service = self._service(base)
            payload = self._payload(
                self._desired_config({"capture": {"segmentSeconds": 2}})
            )

            result = service.process_desired_config(payload)

            pending_data = json.loads(
                (base / "config.pending.json").read_text(encoding="utf-8")
            )
            state_data = json.loads((base / "config.state.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "pending_restart")
        self.assertTrue(result.requires_restart)
        self.assertEqual(pending_data["capture"]["segmentSeconds"], 2)
        self.assertFalse((base / "config.json").exists())
        self.assertEqual(state_data["pendingVersion"], 2)

    def test_hot_reload_update_ignores_unchanged_restart_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text(
                json.dumps(self._desired_config()),
                encoding="utf-8",
            )
            service = self._service(base)
            payload = self._payload(
                self._desired_config(
                    {"operationWindow": {"start": "09:00", "end": "21:00"}}
                )
            )

            result = service.process_desired_config(payload)

        self.assertEqual(result.status, "applied")
        self.assertFalse(result.requires_restart)

    def test_rejects_desired_config_with_secret_like_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            payload = self._payload(
                self._desired_config({"mqtt": {"username": "operator"}})
            )

            with self.assertRaises(Exception) as ctx:
                service.process_desired_config(payload)

        self.assertIn("mqtt.username", str(ctx.exception))

    def test_rejects_rtsp_url_with_inline_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            payload = self._payload(
                self._desired_config(
                    {
                        "cameras": [
                            {
                                "id": "cam01",
                                "sourceType": "rtsp",
                                "rtspUrl": "rtsp://user:pass@192.168.1.10/stream",
                            }
                        ]
                    }
                )
            )

            with self.assertRaises(Exception) as ctx:
                service.process_desired_config(payload)

        self.assertIn("rtspUrl", str(ctx.exception))

    def test_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            payload = self._payload(
                self._desired_config({"operationWindow": {"start": "08:00"}})
            )
            payload["desired_hash"] = "sha256:bad"
            payload["signature"] = sign_desired_config_payload(
                payload=payload,
                device_secret="secret-123",
            )

            with self.assertRaises(Exception) as ctx:
                service.process_desired_config(payload)

        self.assertIn("desired_hash", str(ctx.exception))

    def test_rejects_expired_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            payload = self._payload(
                self._desired_config({"operationWindow": {"start": "08:00"}}),
                expires_at=(
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            )

            with self.assertRaises(Exception) as ctx:
                service.process_desired_config(payload)

        self.assertIn("expirada", str(ctx.exception))

    def test_rejects_old_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.state.json").write_text(
                json.dumps({"lastAppliedVersion": 3}),
                encoding="utf-8",
            )
            service = self._service(base)
            payload = self._payload(
                self._desired_config({"operationWindow": {"start": "08:00"}}),
                version=2,
            )

            with self.assertRaises(Exception) as ctx:
                service.process_desired_config(payload)

        self.assertIn("antiga", str(ctx.exception))

    def test_malformed_payload_still_publishes_signed_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeMQTTClient()
            service = self._service(Path(tmp), client)

            service._handle_message(  # noqa: SLF001 - regression test for MQTT handler
                "grn/devices/edge-01/config/desired",
                json.dumps({"type": "config.desired"}).encode("utf-8"),
            )

        report = client.published[-1][1]
        self.assertEqual(report["status"], "rejected")
        self.assertIsNone(report["config_version"])
        self.assertEqual(report["signature_version"], "hmac-sha256-v1")
        self.assertEqual(
            report["signature"],
            sign_reported_config_payload(payload=report, device_secret="secret-123"),
        )


if __name__ == "__main__":
    unittest.main()
