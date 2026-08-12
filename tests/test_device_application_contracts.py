from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.application.device import (
    BuildDevicePresence,
    CollectRuntimeDiagnostics,
    DeviceIdentity,
    HandleDeviceCommand,
    RuntimeDiagnostics,
)
from src.config.settings import MQTTConfig
from src.infrastructure.mqtt import LegacyMqttAdapter
from src.infrastructure.security import LegacyEnvEnvelopeCipher, LegacyHmacSigner
from src.security.env_envelope import open_env_envelope
from src.security.hmac import hmac_sha256_base64
from src.services.mqtt.command_dispatcher import CommandDispatcher
from src.services.mqtt.device_presence_service import DevicePresenceService


class _MqttClient:
    def __init__(self) -> None:
        self.is_enabled = True
        self.published: list[tuple[str, dict, bool]] = []
        self.subscriptions: list[tuple[str, object]] = []

    def publish_json(self, topic, payload, *, retain=False, qos=None):
        _ = qos
        self.published.append((topic, payload, retain))
        return True

    def subscribe(self, topic, handler, *, qos=None):
        _ = qos
        self.subscriptions.append((topic, handler))
        return True


def _mqtt_config() -> MQTTConfig:
    return MQTTConfig(
        enabled=True,
        host="broker",
        port=1883,
        username="",
        password="",
        client_id="edge-client",
        use_tls=False,
        keepalive=60,
        topic_prefix="grn",
        qos=1,
        retain_presence=True,
        heartbeat_interval_sec=30,
        agent_version="1.4.0",
    )


class DeviceApplicationContractTests(unittest.TestCase):
    def test_diagnostics_failure_uses_legacy_safe_defaults(self) -> None:
        def unavailable():
            raise RuntimeError("runtime is starting")

        self.assertEqual(RuntimeDiagnostics(), CollectRuntimeDiagnostics(unavailable)())

    def test_presence_and_state_match_legacy_wire_contract(self) -> None:
        snapshot = {
            "queue_size": 3,
            "health": {"online_cameras": 1},
            "cameras": [{"camera_id": "cam-1"}],
            "runtime": {"light_mode": True},
        }
        legacy = DevicePresenceService(
            _MqttClient(),
            _mqtt_config(),
            device_id="edge-01",
            client_id="client-01",
            venue_id="venue-01",
            boot_id="boot-01",
            runtime_snapshot_provider=lambda: snapshot,
        )
        identity = DeviceIdentity("edge-01", "client-01", "venue-01", "1.4.0", "boot-01")
        current = BuildDevicePresence(
            identity,
            now_iso=lambda: "2026-07-27T12:00:00+00:00",
            hostname=lambda: "edge-host",
        )
        diagnostics = RuntimeDiagnostics.from_mapping(snapshot)

        with (
            patch.object(
                DevicePresenceService,
                "_now_iso",
                return_value="2026-07-27T12:00:00+00:00",
            ),
            patch(
                "src.services.mqtt.device_presence_service.socket.gethostname",
                return_value="edge-host",
            ),
        ):
            self.assertEqual(
                legacy.build_presence_payload(status="online"),
                current.presence(diagnostics, status="online", heartbeat_seq=0),
            )
            self.assertEqual(
                legacy.build_presence_payload(status="offline", disconnect_reason="clean_shutdown"),
                current.presence(
                    diagnostics,
                    status="offline",
                    heartbeat_seq=0,
                    disconnect_reason="clean_shutdown",
                ),
            )
            self.assertEqual(
                legacy.build_state_payload(),
                current.state(diagnostics, heartbeat_seq=0),
            )

    def test_rental_presence_keeps_null_venue_in_wire_contract(self) -> None:
        identity = DeviceIdentity(
            "edge-rental-01",
            "client-rental",
            None,
            "1.4.0",
            "boot-rental",
            "rental",
        )
        current = BuildDevicePresence(
            identity,
            now_iso=lambda: "2026-08-12T12:00:00+00:00",
            hostname=lambda: "rental-host",
        )

        payload = current.presence(
            RuntimeDiagnostics(),
            status="online",
            heartbeat_seq=1,
        )

        self.assertIn("venue_id", payload)
        self.assertIsNone(payload["venue_id"])

    def test_command_handler_matches_legacy_rejection_payload(self) -> None:
        raw = json.dumps({"command": "restart_service"}).encode()
        topic_in = "grn/devices/edge-01/commands/in"
        topic_out = "grn/devices/edge-01/commands/out"

        legacy_client = _MqttClient()
        legacy = CommandDispatcher(
            legacy_client,
            device_id="edge-01",
            command_in_topic=topic_in,
            command_out_topic=topic_out,
        )
        legacy._handle_message(topic_in, raw)

        new_client = _MqttClient()
        handler = HandleDeviceCommand(
            LegacyMqttAdapter(new_client),
            device_id="edge-01",
            output_topic=topic_out,
        )
        self.assertTrue(handler(topic_in, raw))
        self.assertEqual(legacy_client.published, new_client.published)

    def test_security_adapters_delegate_without_wire_changes(self) -> None:
        signer = LegacyHmacSigner()
        self.assertEqual(
            hmac_sha256_base64("secret", "canonical"),
            signer.sign("secret", "canonical"),
        )

        cipher = LegacyEnvEnvelopeCipher()
        envelope = cipher.seal(
            "secret",
            "request-1",
            "edge-01",
            "A=1\n",
            issued_at="2026-07-27T12:00:00+00:00",
        )
        self.assertEqual("A=1\n", cipher.open("secret", envelope))
        self.assertEqual("A=1\n", open_env_envelope("secret", envelope))
        self.assertEqual(
            {
                "version",
                "request_id",
                "device_id",
                "issued_at",
                "iv",
                "ciphertext",
                "auth_tag",
                "content_hash",
                "signature",
            },
            set(envelope),
        )


if __name__ == "__main__":
    unittest.main()
