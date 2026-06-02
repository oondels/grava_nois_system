"""Testes do DeviceEnvService — serviço MQTT de .env admin."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.security.env_envelope import seal_env_envelope
from src.security.hmac import hmac_sha256_base64
from src.services.mqtt.device_env_service import DeviceEnvService, _parse_env_keys

DEVICE_SECRET = "test-device-secret-32-chars-long!"
DEVICE_ID = "device-test-001"
CLIENT_ID = "client-test-001"
VENUE_ID = "venue-test-001"

SAMPLE_ENV = """# Test .env
GN_API_URL=https://api.example.com
GN_API_TOKEN=tok_abc123
DEVICE_SECRET=super-secret
GN_MQTT_BROKER_URL=mqtt://broker:1883
SOME_VAR=some_value
"""


def _make_mock_mqtt() -> MagicMock:
    client = MagicMock()
    client.is_enabled = True
    client.is_connected = True
    client.subscribe = MagicMock(return_value=True)
    client.publish_json = MagicMock(return_value=True)
    client.add_on_connect_listener = MagicMock()
    return client


def _make_service(
    env_path: Path, mqtt_client: MagicMock | None = None
) -> DeviceEnvService:
    return DeviceEnvService(
        mqtt_client or _make_mock_mqtt(),
        device_id=DEVICE_ID,
        client_id=CLIENT_ID,
        venue_id=VENUE_ID,
        request_topic=f"grn/devices/{DEVICE_ID}/env/request",
        desired_topic=f"grn/devices/{DEVICE_ID}/env/desired",
        reported_topic=f"grn/devices/{DEVICE_ID}/env/reported",
        env_path=env_path,
        device_secret=DEVICE_SECRET,
        agent_version="test",
    )


def _sign_request(device_id: str, request_id: str, requested_at: str) -> str:
    canonical = f"v1:ENV_REQUEST:{device_id}:{request_id}:{requested_at}"
    return hmac_sha256_base64(DEVICE_SECRET, canonical)


class TestParseEnvKeys(unittest.TestCase):
    def test_basic(self) -> None:
        keys = _parse_env_keys(SAMPLE_ENV)
        self.assertIn("GN_API_URL", keys)
        self.assertIn("GN_API_TOKEN", keys)
        self.assertIn("DEVICE_SECRET", keys)
        self.assertNotIn("# Test .env", keys)

    def test_empty(self) -> None:
        self.assertEqual(_parse_env_keys(""), [])
        self.assertEqual(_parse_env_keys("# comment\n"), [])


class TestDeviceEnvServiceStart(unittest.TestCase):
    def test_start_without_secret_fails(self) -> None:
        mqtt = _make_mock_mqtt()
        svc = DeviceEnvService(
            mqtt,
            device_id=DEVICE_ID,
            client_id=CLIENT_ID,
            venue_id=VENUE_ID,
            request_topic="topic/req",
            desired_topic="topic/des",
            reported_topic="topic/rep",
            device_secret="",
        )
        self.assertFalse(svc.start())

    def test_start_subscribes_topics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(SAMPLE_ENV)
            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)
            svc.start()
            self.assertEqual(mqtt.subscribe.call_count, 2)


class TestDeviceEnvServiceRequest(unittest.TestCase):
    def test_env_request_returns_encrypted_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(SAMPLE_ENV)

            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)

            request_id = "req-001"
            requested_at = "2026-04-13T00:00:00Z"
            payload = {
                "type": "env.request",
                "device_id": DEVICE_ID,
                "request_id": request_id,
                "requested_at": requested_at,
                "signature": _sign_request(DEVICE_ID, request_id, requested_at),
            }

            topic = f"grn/devices/{DEVICE_ID}/env/request"
            svc._handle_message(topic, json.dumps(payload).encode())

            mqtt.publish_json.assert_called_once()
            call_args = mqtt.publish_json.call_args
            report = call_args[0][1]

            self.assertEqual(report["type"], "env.reported")
            self.assertEqual(report["status"], "snapshot")
            self.assertEqual(report["request_id"], request_id)
            self.assertIn("envelope", report)
            self.assertIn("env_hash", report)
            self.assertIn("env_keys", report)
            self.assertIn("GN_API_URL", report["env_keys"])

    def test_env_request_invalid_signature_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(SAMPLE_ENV)

            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)

            payload = {
                "type": "env.request",
                "device_id": DEVICE_ID,
                "request_id": "req-bad",
                "requested_at": "2026-04-13T00:00:00Z",
                "signature": "invalid-signature",
            }

            topic = f"grn/devices/{DEVICE_ID}/env/request"
            svc._handle_message(topic, json.dumps(payload).encode())

            call_args = mqtt.publish_json.call_args
            report = call_args[0][1]
            self.assertEqual(report["status"], "rejected")


class TestDeviceEnvServiceDesired(unittest.TestCase):
    def test_apply_env_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(SAMPLE_ENV)

            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)

            new_env = "GN_API_URL=https://new.api.com\nNEW_KEY=value\n"
            envelope = seal_env_envelope(
                device_secret=DEVICE_SECRET,
                request_id="req-apply-001",
                device_id=DEVICE_ID,
                plaintext=new_env,
            )

            payload = {
                "type": "env.desired",
                "device_id": DEVICE_ID,
                "request_id": "req-apply-001",
                "envelope": envelope,
                "restart_after_apply": False,
            }

            topic = f"grn/devices/{DEVICE_ID}/env/desired"
            svc._handle_message(topic, json.dumps(payload).encode())

            # Verifica que o .env foi atualizado
            new_content = env_path.read_text()
            self.assertEqual(new_content, new_env)

            # Verifica que o backup foi criado
            backups = list(Path(td).glob(".env.bak.grn.*"))
            self.assertEqual(len(backups), 1)

            # Verifica permissões (600)
            mode = oct(env_path.stat().st_mode)[-3:]
            self.assertEqual(mode, "600")

            # Verifica report publicado
            call_args = mqtt.publish_json.call_args
            report = call_args[0][1]
            self.assertEqual(report["status"], "applied_requires_restart")
            self.assertEqual(report["request_id"], "req-apply-001")

    def test_apply_invalid_env_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(SAMPLE_ENV)

            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)

            # .env com bytes nulos
            bad_env = "KEY=value\x00binary"
            envelope = seal_env_envelope(
                device_secret=DEVICE_SECRET,
                request_id="req-bad-001",
                device_id=DEVICE_ID,
                plaintext=bad_env,
            )

            payload = {
                "type": "env.desired",
                "device_id": DEVICE_ID,
                "request_id": "req-bad-001",
                "envelope": envelope,
                "restart_after_apply": False,
            }

            topic = f"grn/devices/{DEVICE_ID}/env/desired"
            svc._handle_message(topic, json.dumps(payload).encode())

            # .env original deve permanecer inalterado
            self.assertEqual(env_path.read_text(), SAMPLE_ENV)

            call_args = mqtt.publish_json.call_args
            report = call_args[0][1]
            self.assertEqual(report["status"], "rejected")

    def test_wrong_device_secret_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(SAMPLE_ENV)

            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)

            # Envelope selado com secret diferente
            envelope = seal_env_envelope(
                device_secret="wrong-secret-wrong-secret-wrong!",
                request_id="req-wrong-001",
                device_id=DEVICE_ID,
                plaintext="KEY=value\n",
            )

            payload = {
                "type": "env.desired",
                "device_id": DEVICE_ID,
                "request_id": "req-wrong-001",
                "envelope": envelope,
                "restart_after_apply": False,
            }

            topic = f"grn/devices/{DEVICE_ID}/env/desired"
            svc._handle_message(topic, json.dumps(payload).encode())

            # .env original inalterado
            self.assertEqual(env_path.read_text(), SAMPLE_ENV)

            call_args = mqtt.publish_json.call_args
            report = call_args[0][1]
            self.assertEqual(report["status"], "rejected")

    def test_restart_after_apply_schedules_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            env_path.write_text(SAMPLE_ENV)

            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)

            new_env = "GN_API_URL=https://new.api.com\n"
            envelope = seal_env_envelope(
                device_secret=DEVICE_SECRET,
                request_id="req-restart-001",
                device_id=DEVICE_ID,
                plaintext=new_env,
            )

            payload = {
                "type": "env.desired",
                "device_id": DEVICE_ID,
                "request_id": "req-restart-001",
                "envelope": envelope,
                "restart_after_apply": True,
            }

            topic = f"grn/devices/{DEVICE_ID}/env/desired"
            with patch("threading.Timer") as mock_timer_cls:
                mock_timer = MagicMock()
                mock_timer_cls.return_value = mock_timer

                svc._handle_message(topic, json.dumps(payload).encode())

                mock_timer_cls.assert_called_once()
                mock_timer.start.assert_called_once()

    def test_env_not_found_on_request(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env_nonexistent"

            mqtt = _make_mock_mqtt()
            svc = _make_service(env_path, mqtt)

            request_id = "req-nofile"
            requested_at = "2026-04-13T00:00:00Z"
            payload = {
                "type": "env.request",
                "device_id": DEVICE_ID,
                "request_id": request_id,
                "requested_at": requested_at,
                "signature": _sign_request(DEVICE_ID, request_id, requested_at),
            }

            topic = f"grn/devices/{DEVICE_ID}/env/request"
            svc._handle_message(topic, json.dumps(payload).encode())

            call_args = mqtt.publish_json.call_args
            report = call_args[0][1]
            self.assertEqual(report["status"], "rejected")
            self.assertIn("não encontrado", report["rejection_reason"])


if __name__ == "__main__":
    unittest.main()
