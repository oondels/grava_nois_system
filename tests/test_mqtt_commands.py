from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src.services.docker_action_request import DockerActionRequestService
from src.services.mqtt.command_dispatcher import CommandDispatcher, canonical_json
from src.services.mqtt.command_executor import CommandExecutor
from src.services.mqtt.command_policy import CommandPolicy


class _FakeMQTTClient:
    is_enabled = True

    def __init__(self):
        self.subscriptions, self.published = [], []

    def subscribe(self, topic, handler, *, qos=None):
        self.subscriptions.append((topic, handler))
        return True

    def publish_json(self, topic, payload, *, retain=False, qos=None):
        self.published.append((topic, payload))
        return True


def signed(secret: str, **overrides):
    now = datetime.now(UTC)
    payload = {
        "type": "device.operation.request",
        "device_id": "edge-01",
        "request_id": str(uuid.uuid4()),
        "command": "restart_container",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
        "parameters": {},
        "signature_version": "hmac-sha256-v1",
        **overrides,
    }
    digest = hmac.new(secret.encode(), canonical_json(payload).encode(), hashlib.sha256).digest()
    return {**payload, "signature": base64.b64encode(digest).decode()}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = _FakeMQTTClient()
        self.service = DockerActionRequestService(
            enabled=True,
            request_path=self.root / "docker-action.request.json",
            pull_token="PULL",
            restart_token="RESTART",
        )
        self.dispatcher = self.build()

    def build(self):
        return CommandDispatcher(
            self.client,
            device_id="edge-01",
            device_secret="secret",
            command_in_topic="commands/in",
            command_out_topic="commands/out",
            policy=CommandPolicy(enabled=True),
            executor=CommandExecutor(self.service),
            ledger_path=self.root / "ledger.json",
            result_path=self.root / "last.json",
        )

    def send(self, payload, dispatcher=None):
        (dispatcher or self.dispatcher)._handle_message("commands/in", json.dumps(payload).encode())

    def report(self):
        return self.client.published[-1][1]

    def ack(self, report, **overrides):
        payload = {
            "type": "device.operation.ack",
            "device_id": "edge-01",
            "request_id": report["request_id"],
            "report_id": report["report_id"],
            "status": "persisted",
            "signature_version": "hmac-sha256-v1",
            **overrides,
        }
        payload["signature"] = self.dispatcher._sign(payload)
        self.dispatcher._handle_ack("commands/ack", json.dumps(payload).encode())

    def test_policy_is_disabled_by_default(self):
        self.assertFalse(
            CommandPolicy(enabled=False).is_allowed("restart_container", signed("x"))[0]
        )

    def test_valid_command_is_persisted_once_across_restarts(self):
        payload = signed("secret")
        self.send(payload)
        first = self.report()
        self.send(payload, self.build())
        self.assertEqual(first["report_id"], self.report()["report_id"])
        self.assertEqual(self.report()["status"], "accepted")
        self.assertEqual(len(list((self.service.action_root / "requests").glob("*.json"))), 1)
        unsigned = {k: v for k, v in first.items() if k != "signature"}
        self.assertEqual(first["signature"], self.dispatcher._sign(unsigned))

    def test_disabled_and_busy_never_report_accepted(self):
        self.service.enabled = False
        self.send(signed("secret"))
        self.assertEqual(self.report()["status"], "failed")
        self.service.enabled = True
        self.service.request_path.write_text('{"request_id":"previous"}')
        payload = signed("secret")
        self.send(payload)
        self.assertEqual(self.report()["error_code"], "host_busy")
        self.service.request_path.unlink()
        self.send(payload)
        self.assertEqual(self.report()["status"], "failed")
        self.assertFalse((self.service.action_root / "requests").exists())

    def test_invalid_signature_type_and_request_path_ignored(self):
        for payload in (
            signed("wrong"),
            signed("secret", type="other"),
            signed("secret", request_id="../../bad"),
        ):
            self.send(payload)
        self.assertEqual(self.client.published, [])

    def test_expired_and_future_command_never_creates_intent(self):
        self.send(
            signed("secret", issued_at="2020-01-01T00:00:00Z", expires_at="2020-01-01T00:02:00Z")
        )
        self.assertEqual(self.report()["status"], "expired")
        future = datetime.now(UTC) + timedelta(minutes=5)
        self.send(
            signed(
                "secret",
                issued_at=future.isoformat(),
                expires_at=(future + timedelta(seconds=120)).isoformat(),
            )
        )
        self.assertEqual(self.report()["status"], "failed")
        self.assertFalse((self.service.action_root / "requests").exists())

    def test_publish_success_is_not_ack_and_wrong_ack_does_not_remove(self):
        self.send(signed("secret"))
        report = self.report()
        path = self.dispatcher.outbox / f"{report['report_id']}.json"
        self.assertTrue(path.exists())
        self.ack(report, request_id=str(uuid.uuid4()))
        self.assertTrue(path.exists())
        self.ack(report)
        self.assertFalse(path.exists())
        self.build().poll_once()
        self.assertFalse(path.exists())

    def test_host_results_retained_until_durable_copy_and_mqtt_ack(self):
        payload = signed("secret")
        self.send(payload)
        accepted = self.report()
        folder = self.service.action_root / "results"
        folder.mkdir(parents=True)
        result_path = folder / f"{payload['request_id']}.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "request_id": payload["request_id"],
                    "action": "restart_container",
                    "status": "ok",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "stage": "ready",
                }
            )
        )
        self.dispatcher.poll_once()
        final = self.report()
        self.assertEqual(final["status"], "succeeded")
        self.assertTrue((self.service.action_root / "receipts" / result_path.name).exists())
        self.assertEqual(len(list(self.dispatcher.outbox.glob("*.json"))), 2)
        self.ack(accepted)
        self.ack(final)
        self.build().poll_once()
        self.assertEqual(list(self.dispatcher.outbox.glob("*.json")), [])

    def test_crash_after_intent_before_report_recovers_without_reexecution(self):
        payload = signed("secret")
        execute = self.dispatcher.executor.execute

        def crash(command, data):
            execute(command, data)
            raise OSError("crash")

        with patch.object(self.dispatcher.executor, "execute", side_effect=crash):
            self.send(payload)
        other = self.build()
        with patch.object(other.executor, "execute") as repeated:
            self.send(payload, other)
            repeated.assert_not_called()
        self.assertEqual(self.report()["status"], "accepted")

    def test_interrupted_admission_is_unknown_without_resubmission(self):
        payload = signed("secret")
        with patch.object(self.dispatcher.executor, "execute", side_effect=OSError("disk error")):
            self.send(payload)
        self.send(payload, self.build())
        self.assertEqual(self.report()["status"], "unknown")
        self.assertFalse((self.service.action_root / "requests").exists())

    def test_start_and_stop_join_reporter(self):
        self.assertTrue(self.dispatcher.start())
        self.assertEqual(len(self.client.subscriptions), 2)
        self.dispatcher.stop()
        self.assertFalse(self.dispatcher._thread.is_alive())
