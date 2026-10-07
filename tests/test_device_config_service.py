from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.config.operational_env import persist_operational_config
from src.services.mqtt.device_config_service import (
    DeviceConfigService,
    RemoteConfigError,
    apply_pending_config_on_startup,
    hash_config,
    sign_desired_config_payload,
    sign_request_config_payload,
    sign_reported_config_payload,
    sign_state_snapshot_payload,
)


class _FakeMQTTClient:
    def __init__(self):
        self.is_enabled = True
        self.is_connected = False
        self.subscriptions = []
        self.published = []
        self.connect_listeners = []
        self.allow_publish_when_disconnected = True

    def subscribe(self, topic, handler, *, qos=None):
        _ = qos
        self.subscriptions.append((topic, handler))
        return True

    def publish_json(self, topic, payload, *, retain=False, qos=None):
        _ = retain, qos
        if not self.is_connected and not self.allow_publish_when_disconnected:
            return False
        self.published.append((topic, payload))
        return True

    def add_on_connect_listener(self, callback):
        self.connect_listeners.append(callback)


def _deep_update(target: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


class DeviceConfigServiceTests(unittest.TestCase):
    def test_unchanged_operational_env_does_not_create_second_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("CUSTOM_OPTION=fixture-only\n")
            config = self._desired_config()
            persist_operational_config(env_path, config)
            first = list(env_path.parent.glob(".env.bak.grn.config.*"))
            persist_operational_config(env_path, config)
            self.assertEqual(len(first), 1)
            self.assertEqual(list(env_path.parent.glob(".env.bak.grn.config.*")), first)

    def _service(
        self,
        base: Path,
        client: _FakeMQTTClient | None = None,
        env_path: Path | None = None,
    ) -> DeviceConfigService:
        return DeviceConfigService(
            client or _FakeMQTTClient(),
            device_id="edge-01",
            client_id="client-01",
            venue_id="venue-01",
            desired_topic="grn/devices/edge-01/config/desired",
            reported_topic="grn/devices/edge-01/config/reported",
            request_topic="grn/devices/edge-01/config/request",
            state_topic="grn/devices/edge-01/config/state",
            config_path=base / "config.json",
            env_path=env_path,
            device_secret="secret-123",
            agent_version="1.2.3",
        )

    def _rental_service(self, base: Path, client: _FakeMQTTClient | None = None) -> DeviceConfigService:
        return DeviceConfigService(
            client or _FakeMQTTClient(),
            device_id="edge-rental-01",
            client_id=None,
            venue_id=None,
            desired_topic="grn/devices/edge-rental-01/config/desired",
            reported_topic="grn/devices/edge-rental-01/config/reported",
            request_topic="grn/devices/edge-rental-01/config/request",
            state_topic="grn/devices/edge-rental-01/config/state",
            config_path=base / "config.json",
            device_secret="secret-rental",
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
                "verticalFormat": False,
                "hqCrf": 18,
                "hqPreset": "medium",
                "lmCrf": 26,
                "lmPreset": "veryfast",
                "watermark": {
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

    def _request_payload(self, *, requested_at: str | None = None, request_id: str = "req-01") -> dict:
        payload = {
            "type": "config.request",
            "device_id": "edge-01",
            "client_id": "client-01",
            "venue_id": "venue-01",
            "schema_version": 1,
            "request_id": request_id,
            "requested_at": requested_at or datetime.now(timezone.utc).isoformat(),
        }
        payload["signature"] = sign_request_config_payload(
            payload=payload,
            device_secret="secret-123",
        )
        return payload

    def test_start_subscribes_to_config_desired_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeMQTTClient()
            service = self._service(Path(tmp), client)

            self.assertTrue(service.start())

        self.assertEqual(len(client.subscriptions), 2)
        self.assertEqual(
            client.subscriptions[0][0],
            "grn/devices/edge-01/config/desired",
        )
        self.assertEqual(
            client.subscriptions[1][0],
            "grn/devices/edge-01/config/request",
        )
        self.assertEqual(len(client.connect_listeners), 1)

    def test_rental_accepts_and_reports_null_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = _FakeMQTTClient()
            service = self._rental_service(base, client)
            desired = self._desired_config()
            issued = datetime.now(timezone.utc).isoformat()
            payload = {
                "type": "config.desired",
                "device_id": "edge-rental-01",
                "client_id": None,
                "venue_id": None,
                "schema_version": 1,
                "config_version": 2,
                "desired_hash": hash_config({**desired, "version": 2, "updatedAt": issued}),
                "correlation_id": "corr-rental",
                "issued_at": issued,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "desired_config": desired,
            }
            payload["signature"] = sign_desired_config_payload(
                payload=payload,
                device_secret="secret-rental",
            )

            result = service.process_desired_config(payload)
            service.publish_report(result)

        self.assertIsNone(client.published[-1][1]["client_id"])
        self.assertIsNone(client.published[-1][1]["venue_id"])

    def test_rental_rejects_non_null_venue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._rental_service(Path(tmp))
            desired = self._desired_config()
            issued = datetime.now(timezone.utc).isoformat()
            payload = {
                "type": "config.desired",
                "device_id": "edge-rental-01",
                "client_id": None,
                "venue_id": "unexpected-venue",
                "schema_version": 1,
                "config_version": 2,
                "desired_hash": hash_config({**desired, "version": 2, "updatedAt": issued}),
                "correlation_id": "corr-rental",
                "issued_at": issued,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "desired_config": desired,
            }
            payload["signature"] = sign_desired_config_payload(payload=payload, device_secret="secret-rental")

            with self.assertRaisesRegex(Exception, "venue_id divergente"):
                service.process_desired_config(payload)

    def test_start_publishes_state_snapshot_when_client_is_connected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = _FakeMQTTClient()
            client.is_connected = True
            (base / "config.json").write_text(
                json.dumps(self._desired_config()),
                encoding="utf-8",
            )
            service = self._service(base, client)

            self.assertTrue(service.start())

        self.assertEqual(client.published[-1][0], "grn/devices/edge-01/config/state")
        state_payload = client.published[-1][1]
        self.assertEqual(state_payload["type"], "config.state")
        self.assertEqual(state_payload["reported_config"]["capture"]["segmentSeconds"], 1)
        self.assertFalse(state_payload["has_pending_restart"])
        self.assertIsNone(state_payload["pending_version"])
        self.assertEqual(
            state_payload["signature"],
            sign_state_snapshot_payload(
                payload=state_payload,
                device_secret="secret-123",
            ),
        )

    def test_state_snapshot_normalizes_integer_like_floats_before_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = _FakeMQTTClient()
            (base / "config.json").write_text(
                json.dumps(self._desired_config()),
                encoding="utf-8",
            )
            service = self._service(base, client)

            self.assertTrue(service.publish_state_snapshot())

        state_payload = client.published[-1][1]
        reported_config = state_payload["reported_config"]
        self.assertEqual(reported_config["capture"]["rtsp"]["startupCheckSeconds"], 1)
        self.assertIsInstance(reported_config["capture"]["rtsp"]["startupCheckSeconds"], int)
        self.assertEqual(reported_config["triggers"]["gpio"]["debounceMs"], 300)
        self.assertIsInstance(reported_config["triggers"]["gpio"]["debounceMs"], int)
        self.assertEqual(reported_config["triggers"]["gpio"]["cooldownSeconds"], 120)
        self.assertIsInstance(reported_config["triggers"]["gpio"]["cooldownSeconds"], int)
        self.assertEqual(reported_config["processing"]["watermark"]["opacity"], 0.8)
        self.assertIsInstance(reported_config["processing"]["watermark"]["opacity"], float)

    def test_watermark_layout_stages_before_apply_and_preserves_state_on_failure(self):
        from tests.test_watermark_layout import fixture
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                initial = self._desired_config()
                (base / 'config.json').write_text(json.dumps(initial))
                env = base / '.env'
                env.write_text('GN_CLIENT_WATERMARK_ENABLED=1\n')
                service = self._service(base, env_path=env)
                desired = self._desired_config()
                desired['processing']['watermark']['layout'] = fixture()
                desired['processing']['watermark']['layout']['clientEnabled'] = False
                before = (base / 'config.json').read_bytes()
                with patch('src.services.watermark_catalog.prepare_assets', side_effect=ValueError('watermark_asset_integrity') if failure else None) as prepare:
                    if failure:
                        with self.assertRaisesRegex(ValueError, 'watermark_asset_integrity'):
                            service.process_desired_config(self._payload(desired))
                    else:
                        result = service.process_desired_config(self._payload(desired))
                prepare.assert_called_once()
                if failure:
                    self.assertEqual((base / 'config.json').read_bytes(), before)
                    self.assertEqual(env.read_text(), 'GN_CLIENT_WATERMARK_ENABLED=1\n')
                    self.assertFalse((base / 'config.pending.json').exists())
                else:
                    self.assertEqual(result.status, 'applied')
                    self.assertFalse(result.requires_restart)
                    self.assertIn('GN_CLIENT_WATERMARK_ENABLED=0', env.read_text())
                    self.assertEqual(json.loads((base / 'config.json').read_text())['processing']['watermark']['layout'], desired['processing']['watermark']['layout'])

    def test_journal_watermark_missing_file_preserves_current_config_at_boot(self):
        from tests.test_watermark_layout import fixture
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            current = base / 'config.json'
            current.write_text(json.dumps(self._desired_config()))
            desired = self._desired_config()
            desired['version'] = 2
            desired['processing']['watermark']['layout'] = fixture()
            journal = base / 'config.transaction.json'
            journal.write_text(json.dumps({'desired': desired, 'desired_hash': hash_config(desired), 'config_version': 2, 'correlation_id': 'interrupted'}))
            before = current.read_bytes()
            with patch('src.services.watermark_catalog.verify_local_assets', side_effect=ValueError('watermark_asset_integrity')):
                self.assertIsNone(apply_pending_config_on_startup(current))
            self.assertEqual(current.read_bytes(), before)
            self.assertTrue(journal.exists())

    def test_pending_watermark_missing_file_does_not_promote_at_boot(self):
        from tests.test_watermark_layout import fixture
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            initial = self._desired_config()
            (base / 'config.json').write_text(json.dumps(initial))
            desired = self._desired_config()
            desired['version'] = 2
            desired['processing']['watermark']['layout'] = fixture()
            (base / 'config.pending.json').write_text(json.dumps(desired))
            before = (base / 'config.json').read_bytes()
            with patch('src.services.watermark_catalog.verify_local_assets', side_effect=ValueError('watermark_asset_integrity')):
                self.assertIsNone(apply_pending_config_on_startup(base / 'config.json'))
            self.assertEqual((base / 'config.json').read_bytes(), before)
            self.assertTrue((base / 'config.pending.json').exists())

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
        self.assertEqual(
            client.published[-1][1]["reported_config"]["operationWindow"]["start"],
            "08:00",
        )
        self.assertEqual(client.published[-1][1]["signature_version"], "hmac-sha256-v1")
        self.assertEqual(
            client.published[-1][1]["signature"],
            sign_reported_config_payload(
                payload=client.published[-1][1],
                device_secret="secret-123",
            ),
        )

    def test_persists_remote_config_to_env_and_survives_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env_path = base / ".env"
            env_path.write_text(
                "# identidade preservada\nDEVICE_SECRET=do-not-change\nGN_MQTT_PASSWORD=secret\n",
                encoding="utf-8",
            )
            current = self._desired_config()
            (base / "config.json").write_text(json.dumps(current), encoding="utf-8")
            service = self._service(base, env_path=env_path)
            payload = self._payload(
                self._desired_config({"operationWindow": {"start": "08:00", "end": "22:00"}})
            )

            result = service.process_desired_config(payload)
            env_content = env_path.read_text(encoding="utf-8")
            regenerated = base / "regenerated.json"
            subprocess.run(
                ["bash", "env_to_config.sh", str(env_path), str(regenerated)],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            regenerated_config = json.loads(regenerated.read_text(encoding="utf-8"))

        self.assertEqual(result.status, "applied")
        self.assertIn("DEVICE_SECRET=do-not-change", env_content)
        self.assertIn("GN_MQTT_PASSWORD=secret", env_content)
        self.assertIn("GN_START_TIME=08:00", env_content)
        self.assertEqual(regenerated_config["version"], 2)
        self.assertEqual(regenerated_config["operationWindow"]["start"], "08:00")
        self.assertEqual(regenerated_config["capture"]["v4l2"]["device"], "/dev/video0")

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

    def test_signed_restart_request_is_bound_to_desired_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            service = self._service(base)
            payload = self._payload(
                self._desired_config({"capture": {"segmentSeconds": 2}})
            )
            payload["restart_after_apply"] = True
            payload["signature"] = sign_desired_config_payload(
                payload=payload,
                device_secret="secret-123",
            )

            result = service.process_desired_config(payload)

        self.assertTrue(result.restart_after_apply)
        tampered = dict(payload)
        tampered["restart_after_apply"] = False
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(Exception, "assinatura"):
                self._service(Path(tmp)).process_desired_config(tampered)

    def test_boot_promotes_pending_config_before_runtime_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            desired_config = self._desired_config({"capture": {"segmentSeconds": 2}})
            prepared = {
                **desired_config,
                "version": 2,
                "updatedAt": "2026-04-08T12:00:00+00:00",
            }
            (base / "config.pending.json").write_text(
                json.dumps(prepared),
                encoding="utf-8",
            )
            (base / "config.state.json").write_text(
                json.dumps(
                    {
                        "pendingVersion": 2,
                        "pendingHash": hash_config(prepared),
                        "pendingCorrelationId": "corr-boot-01",
                        "lastStatus": "pending_restart",
                    }
                ),
                encoding="utf-8",
            )

            result = apply_pending_config_on_startup(base / "config.json")

            config_data = json.loads((base / "config.json").read_text(encoding="utf-8"))
            state_data = json.loads((base / "config.state.json").read_text(encoding="utf-8"))

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "applied")
        self.assertEqual(result.config_version, 2)
        self.assertFalse(result.requires_restart)
        self.assertEqual(config_data["capture"]["segmentSeconds"], 2)
        self.assertFalse((base / "config.pending.json").exists())
        self.assertEqual(state_data["lastAppliedVersion"], 2)
        self.assertIsNone(state_data["pendingVersion"])
        self.assertEqual(state_data["lastStatus"], "applied")

    def test_startup_report_waits_for_connect_and_is_published_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = _FakeMQTTClient()
            client.allow_publish_when_disconnected = False
            service = self._service(base, client)

            desired_config = self._desired_config({"capture": {"segmentSeconds": 2}})
            prepared = {
                **desired_config,
                "version": 2,
                "updatedAt": "2026-04-08T12:00:00+00:00",
            }
            (base / "config.pending.json").write_text(
                json.dumps(prepared),
                encoding="utf-8",
            )
            (base / "config.state.json").write_text(
                json.dumps(
                    {
                        "pendingVersion": 2,
                        "pendingHash": hash_config(prepared),
                        "pendingCorrelationId": "corr-boot-01",
                        "lastStatus": "pending_restart",
                    }
                ),
                encoding="utf-8",
            )
            startup_report = apply_pending_config_on_startup(base / "config.json")

            self.assertFalse(service.queue_startup_report(startup_report))
            self.assertEqual(client.published, [])

            self.assertTrue(service.start())
            self.assertEqual(len(client.connect_listeners), 1)

            client.is_connected = True
            client.connect_listeners[0]()
            published_topics = [topic for topic, _payload in client.published]
            self.assertIn("grn/devices/edge-01/config/state", published_topics)
            self.assertIn("grn/devices/edge-01/config/reported", published_topics)
            first_publish_count = len(client.published)

            client.connect_listeners[0]()
            self.assertEqual(len(client.published), first_publish_count + 1)
            self.assertEqual(client.published[-1][0], "grn/devices/edge-01/config/state")

    def test_process_config_request_publishes_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = _FakeMQTTClient()
            (base / "config.json").write_text(
                json.dumps(
                    self._desired_config(
                        {
                            "cameras": [
                                {
                                    "id": "cam01",
                                    "name": "Principal",
                                    "enabled": True,
                                    "sourceType": "rtsp",
                                    "rtspUrl": "env:GN_CAM01_RTSP_URL",
                                }
                            ]
                        }
                    )
                ),
                encoding="utf-8",
            )
            service = self._service(base, client)

            self.assertTrue(service.process_config_request(self._request_payload()))

        self.assertEqual(client.published[-1][0], "grn/devices/edge-01/config/state")
        state_payload = client.published[-1][1]
        self.assertEqual(state_payload["request_id"], "req-01")
        self.assertEqual(
            state_payload["reported_config"]["cameras"][0]["rtspUrl"],
            "env:GN_CAM01_RTSP_URL",
        )
        self.assertIsNone(state_payload["pending_version"])
        self.assertEqual(state_payload["signature_version"], "hmac-sha256-v1")
        self.assertEqual(
            state_payload["signature"],
            sign_state_snapshot_payload(
                payload=state_payload,
                device_secret="secret-123",
            ),
        )

    def test_publish_state_snapshot_includes_pending_version_only_when_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            client = _FakeMQTTClient()
            (base / "config.json").write_text(
                json.dumps(self._desired_config()),
                encoding="utf-8",
            )
            (base / "config.pending.json").write_text(
                json.dumps(self._desired_config({"capture": {"segmentSeconds": 2}})),
                encoding="utf-8",
            )
            (base / "config.state.json").write_text(
                json.dumps({"lastAppliedVersion": 2, "pendingVersion": 3}),
                encoding="utf-8",
            )
            service = self._service(base, client)

            self.assertTrue(service.publish_state_snapshot())

        state_payload = client.published[-1][1]
        self.assertTrue(state_payload["has_pending_restart"])
        self.assertEqual(state_payload["last_applied_version"], 2)
        self.assertEqual(state_payload["pending_version"], 3)

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

    def test_rejects_old_version_without_overwriting_current_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            original = {**self._desired_config(), "version": 3, "updatedAt": "2026-04-08T12:00:00+00:00"}
            (base / "config.json").write_text(json.dumps(original))
            (base / "config.state.json").write_text(json.dumps({"lastAppliedVersion": 3}))
            service = self._service(base)
            with self.assertRaisesRegex(RemoteConfigError, "stale_config_version"):
                service.process_desired_config(self._payload(self._desired_config(), version=2))
            self.assertEqual(original, json.loads((base / "config.json").read_text()))

    def test_duplicate_version_returns_effective_result_without_reapplying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            service = self._service(base)
            payload = self._payload(self._desired_config({"processing": {"additionalWindows": []}}))
            first = service.process_desired_config(payload)
            current = (base / "config.pending.json") if first.status == "pending_restart" else (base / "config.json")
            timestamp = current.stat().st_mtime_ns
            repeated = service.process_desired_config(payload)
            self.assertEqual(first.status, repeated.status)
            self.assertEqual(timestamp, current.stat().st_mtime_ns)

    def test_additional_windows_are_hot_and_enable_requires_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            current = self._desired_config({"processing": {"deferredEnabled": False, "additionalWindows": []}})
            (base / "config.json").write_text(json.dumps(current))
            service = self._service(base)
            windows = [{"weekdays": [1, 7], "start": "23:00", "end": "07:00"}]
            desired = self._desired_config({"processing": {"deferredEnabled": False, "additionalWindows": windows}})
            self.assertEqual("applied", service.process_desired_config(self._payload(desired)).status)
            desired["processing"]["deferredEnabled"] = True
            self.assertEqual("pending_restart", service.process_desired_config(self._payload(desired, version=3)).status)
            self.assertFalse(json.loads((base / "config.json").read_text())["processing"]["deferredEnabled"])

    def test_conflicting_duplicate_and_invalid_windows_preserve_last_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text(json.dumps(self._desired_config()))
            service = self._service(base)
            desired = self._desired_config({"processing": {"additionalWindows": []}})
            service.process_desired_config(self._payload(desired))
            before = (base / "config.json").read_bytes()
            desired["processing"]["additionalWindows"] = [{"weekdays": [1], "start": "07:00", "end": "08:00"}]
            with self.assertRaisesRegex(RemoteConfigError, "hash_conflict"):
                service.process_desired_config(self._payload(desired))
            desired["processing"]["additionalWindows"][0]["end"] = "24:00"
            with self.assertRaises(RemoteConfigError):
                service.process_desired_config(self._payload(desired, version=3))
            self.assertEqual(before, (base / "config.json").read_bytes())

    def test_interrupted_promotion_recovers_from_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text(json.dumps(self._desired_config()))
            service = self._service(base)
            payload = self._payload(self._desired_config({"processing": {"additionalWindows": []}}))
            with patch.object(service, "_write_state", side_effect=KeyboardInterrupt("power loss")):
                with self.assertRaises(KeyboardInterrupt):
                    service.process_desired_config(payload)
            self.assertTrue((base / "config.transaction.json").exists())
            result = apply_pending_config_on_startup(base / "config.json")
            self.assertEqual("applied", result.status)
            self.assertFalse((base / "config.transaction.json").exists())
            self.assertEqual(2, json.loads((base / "config.state.json").read_text())["lastAppliedVersion"])

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
