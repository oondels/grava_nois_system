"""Authenticated control, durable recovery and secret-free reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.security.env_control import sign_control, validate_control
from src.security.env_envelope import seal_env_envelope
from src.services.docker_action_request import ActionSubmission
from src.services.mqtt.device_env_service import DeviceEnvService, _parse_env_keys

DEVICE_SECRET = "test-device-secret-32-chars-long!"
DEVICE_ID = "device-test-001"
CLIENT_ID = "client-test-001"
VENUE_ID = "venue-test-001"
SAMPLE_ENV = "GN_API_URL=https://api.example.test\nDEVICE_SECRET=fake-only\n"


def _make_mock_mqtt():
    client = MagicMock()
    client.is_enabled = client.is_connected = True
    client.publish_json.return_value = True
    return client


def _make_service(env_path, mqtt_client=None, action_service=None):
    action_service = action_service or MagicMock()
    action_service.submit_action.return_value = ActionSubmission("created", "test")
    return DeviceEnvService(
        mqtt_client or _make_mock_mqtt(),
        device_id=DEVICE_ID,
        client_id=CLIENT_ID,
        venue_id=VENUE_ID,
        request_topic="env/request",
        desired_topic="env/desired",
        reported_topic="env/reported",
        env_path=env_path,
        device_secret=DEVICE_SECRET,
        ledger_dir=env_path.parent / "ledger",
        action_service=action_service,
    )


def control(kind="env.desired", content="KEY=new-value\n", **overrides):
    now = datetime.now(UTC)
    payload = {
        "type": kind,
        "device_id": DEVICE_ID,
        "request_id": str(uuid.uuid4()),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
    }
    payload.update(overrides)
    if kind == "env.desired":
        payload.setdefault("restart_after_apply", False)
        payload.setdefault(
            "envelope", seal_env_envelope(DEVICE_SECRET, payload["request_id"], DEVICE_ID, content)
        )
    return sign_control(DEVICE_SECRET, payload)


class EnvControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"
        self.path.write_text(SAMPLE_ENV)
        self.client = _make_mock_mqtt()
        self.service = _make_service(self.path, self.client)

    def send(self, payload, service=None):
        svc = service or self.service
        topic = svc.request_topic if payload["type"] == "env.request" else svc.desired_topic
        svc._handle_message(topic, json.dumps(payload).encode())

    def report(self):
        return self.client.publish_json.call_args.args[1]

    def test_parse_keys(self):
        self.assertEqual(_parse_env_keys("# c\nA=b\nB=c\n"), ["A", "B"])

    def test_start_requires_secret(self):
        self.service.device_secret = ""
        self.assertFalse(self.service.start())

    def test_start_subscribes_and_stop_joins(self):
        self.assertTrue(self.service.start())
        self.assertEqual(self.client.subscribe.call_count, 2)
        self.service.stop()
        self.assertFalse(self.service._thread.is_alive())

    def test_snapshot_is_encrypted_and_report_authenticated(self):
        payload = control("env.request")
        self.send(payload)
        report = self.report()
        validate_control(DEVICE_SECRET, report, DEVICE_ID, "env.reported")
        self.assertEqual(report["status"], "snapshot")
        self.assertEqual(report["envelope"]["version"], "v2")
        self.assertNotIn("fake-only", json.dumps(report))

    def test_apply_creates_private_backup_and_signed_result(self):
        payload = control()
        self.send(payload)
        self.assertEqual(self.path.read_text(), "KEY=new-value\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        backup = next(self.path.parent.glob(".env.bak.grn.*"))
        self.assertEqual(backup.read_text(), SAMPLE_ENV)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        validate_control(DEVICE_SECRET, self.report(), DEVICE_ID, "env.reported")
        self.assertEqual(self.report()["status"], "applied_requires_restart")
        self.assertEqual(self.report()["restart_status"], "not_requested")
        ledger = (self.service.ledger_dir / f"{payload['request_id']}.json").read_text()
        self.assertNotIn("new-value", ledger)

    def test_identical_content_does_not_create_backup(self):
        self.send(control(content=SAMPLE_ENV))
        self.assertEqual(self.path.read_text(), SAMPLE_ENV)
        self.assertEqual(list(self.path.parent.glob(".env.bak.grn.*")), [])
        self.assertEqual(self.report()["status"], "applied_requires_restart")

    def test_duplicate_after_restart_does_not_reapply_or_restart(self):
        payload = control(restart_after_apply=True)
        self.send(payload)
        self.assertEqual(self.service.action_service.submit_action.call_count, 1)
        second = _make_service(self.path, self.client)
        with patch.object(second, "_write_env_atomic") as write:
            self.send(payload, second)
            write.assert_not_called()
        second.action_service.submit_action.assert_not_called()
        self.assertEqual(self.report()["restart_status"], "queued")

    def test_changed_same_request_rejected(self):
        payload = control()
        self.send(payload)
        payload["restart_after_apply"] = True
        self.send(sign_control(DEVICE_SECRET, payload))
        self.assertEqual(self.report()["rejection_reason"], "request_id_conflict")
        self.service.action_service.submit_action.assert_not_called()

    def test_unsigned_tampered_old_and_future_messages_cannot_mutate(self):
        cases = []
        payload = control()
        payload["restart_after_apply"] = True
        cases.append(payload)
        payload = control()
        payload.pop("signature")
        cases.append(payload)
        cases.append(control(issued_at="2020-01-01T00:00:00Z", expires_at="2020-01-01T00:02:00Z"))
        future = datetime.now(UTC) + timedelta(minutes=10)
        cases.append(
            control(
                issued_at=future.isoformat(),
                expires_at=(future + timedelta(seconds=120)).isoformat(),
            )
        )
        payload = control()
        payload["signature_version"] = "hmac-sha256-v1"
        cases.append(payload)
        for candidate in cases:
            self.send(candidate)
            self.assertEqual(self.path.read_text(), SAMPLE_ENV)
        self.client.publish_json.assert_not_called()
        self.service.action_service.submit_action.assert_not_called()

    def test_inner_identity_mismatch_rejected(self):
        payload = control()
        payload["envelope"] = seal_env_envelope(
            DEVICE_SECRET, str(uuid.uuid4()), DEVICE_ID, "KEY=wrong\n"
        )
        self.send(sign_control(DEVICE_SECRET, payload))
        self.assertEqual(self.path.read_text(), SAMPLE_ENV)
        self.assertEqual(self.report()["status"], "rejected")

    def test_invalid_content_and_missing_mount_rejected(self):
        self.send(control(content="KEY=value\x00"))
        self.assertEqual(self.path.read_text(), SAMPLE_ENV)
        self.path.unlink()
        self.send(control())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.report()["status"], "rejected")

    def test_failed_host_admission_is_not_reported_queued(self):
        self.service.action_service.submit_action.return_value = ActionSubmission("busy", "test")
        self.send(control(restart_after_apply=True))
        self.assertEqual(self.report()["restart_status"], "rejected")
        self.assertEqual(self.report()["restart_error_code"], "busy")

    def test_recovery_after_file_write_does_not_write_again(self):
        payload = control(restart_after_apply=True)
        original = self.service._write_env_atomic

        def crash(content):
            original(content)
            raise OSError("simulated crash after rename")

        with patch.object(self.service, "_write_env_atomic", side_effect=crash):
            self.send(payload)
        second = _make_service(self.path, self.client)
        with patch.object(second, "_write_env_atomic") as write:
            self.send(payload, second)
            write.assert_not_called()
        self.assertEqual(self.report()["status"], "applied_requires_restart")
        second.action_service.submit_action.assert_called_once()

    def test_recovery_before_file_write_does_not_invent_application(self):
        payload = control()
        with patch.object(self.service, "_write_env_atomic", side_effect=OSError("disk full")):
            self.send(payload)
        self.send(payload, _make_service(self.path, self.client))
        self.assertEqual(self.report()["rejection_reason"], "interrupted_apply_requires_sync")
        self.assertEqual(self.path.read_text(), SAMPLE_ENV)

    def test_publish_failure_keeps_durable_result_for_reconnect(self):
        self.client.publish_json.return_value = False
        payload = control()
        self.send(payload)
        second = _make_service(self.path, self.client)
        self.client.reset_mock()
        second._handle_mqtt_connect()
        self.assertEqual(self.report()["request_id"], payload["request_id"])
        self.assertEqual(self.report()["status"], "applied_requires_restart")
