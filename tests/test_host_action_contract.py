"""Real edge/host IPC with disposable state and mocked invasive host effects."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from src.security.durable_state import private_json
from src.services.docker_action_request import DockerActionRequestService
from src.services.mqtt.command_dispatcher import CommandDispatcher
from src.services.mqtt.command_executor import CommandExecutor
from src.services.mqtt.command_policy import CommandPolicy
from tests.test_device_env_service import SAMPLE_ENV, _make_mock_mqtt, _make_service, control
from tests.test_mqtt_commands import _FakeMQTTClient, signed

CONFIG_REPO = Path(
    os.environ.get("GN_CONFIG_REPO", Path(__file__).resolve().parents[2] / "grava_nois_config")
)
RUNNER_PATH = CONFIG_REPO / "scripts/host/device_action_runner.py"


@unittest.skipUnless(
    RUNNER_PATH.exists(), "sibling config checkout required for host IPC integration"
)
class HostActionContractTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("grn_host_contract_runner", RUNNER_PATH)
        self.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.host)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.runtime = self.base / "config/runtime"
        self.runtime.mkdir(parents=True)
        self.env = self.base / "config/.env"
        self.env.write_text("GN_PICO_DOCKER_ACTIONS_ENABLED=1\n")
        self.secret_dir = self.base / "secrets"
        self.service = DockerActionRequestService(
            enabled=True,
            request_path=self.runtime / "docker-action.request.json",
            pull_token="PULL",
            restart_token="RESTART",
        )
        self.client = _FakeMQTTClient()
        self.dispatcher = self.build_dispatcher()
        self.runner = self.host.Runner(
            SimpleNamespace(
                runtime=str(self.runtime),
                request=str(self.service.request_path),
                env=str(self.env),
                config=str(self.runtime / "config.json"),
                converter="/bin/true",
                secrets=str(self.secret_dir),
                compose=str(self.base / "compose.yml"),
                log=str(self.base / "runner.log"),
                netplan=str(self.base / "netplan.yaml"),
                shutdown_enabled=False,
                ready_timeout=0,
            )
        )

    def build_dispatcher(self):
        return CommandDispatcher(
            self.client,
            device_id="edge-01",
            device_secret="secret",
            command_in_topic="commands/in",
            command_out_topic="commands/out",
            policy=CommandPolicy(enabled=True),
            executor=CommandExecutor(self.service),
            ledger_path=self.runtime / "ledger.json",
            result_path=self.runtime / "docker-action.last.json",
        )

    def send(self, payload):
        self.dispatcher._handle_message("commands/in", json.dumps(payload).encode())

    def run_host(self):
        with (
            patch.object(self.runner, "run_command") as command,
            patch.object(self.runner, "ready"),
        ):
            self.runner.process()
        return command

    def ack(self, report):
        payload = {
            "type": "device.operation.ack",
            "device_id": "edge-01",
            "request_id": report["request_id"],
            "report_id": report["report_id"],
            "status": "persisted",
            "signature_version": "hmac-sha256-v1",
        }
        payload["signature"] = self.dispatcher._sign(payload)
        self.dispatcher._handle_ack("commands/ack", json.dumps(payload).encode())

    def result_paths(self, request_id):
        name = request_id + ".json"
        return (
            self.service.action_root / "results" / name,
            self.dispatcher.root / "host-results" / name,
            self.service.action_root / "receipts" / name,
        )

    def test_signed_command_real_host_result_and_reversed_ack_order(self):
        payload = signed("secret")
        self.send(payload)
        accepted = self.client.published[-1][1]
        self.assertEqual(accepted["status"], "accepted")
        intent = json.loads(
            (self.service.action_root / "requests" / (payload["request_id"] + ".json")).read_text()
        )
        self.assertEqual(intent["source"], "mqtt")
        self.assertEqual(intent["schema_version"], 2)
        commands = self.run_host()
        self.assertEqual(commands.call_count, 2)
        self.dispatcher.poll_once()
        final = self.client.published[-1][1]
        self.assertEqual(final["status"], "succeeded")
        host, local, receipt = self.result_paths(payload["request_id"])
        self.assertEqual(json.loads(host.read_text()), json.loads(local.read_text()))
        self.assertTrue(receipt.exists())
        self.ack(final)
        self.ack(accepted)
        self.build_dispatcher().poll_once()
        self.assertEqual(list(self.dispatcher.outbox.glob("*.json")), [])
        # Re-delivery after both receipts returns the final result and creates no intent.
        self.send(payload)
        self.assertEqual(self.client.published[-1][1]["report_id"], final["report_id"])
        self.assertFalse(list((self.service.action_root / "requests").glob("*.json")))

    def test_corrupt_records_never_starve_valid_result_or_outbox(self):
        payload = signed("secret")
        self.send(payload)
        self.run_host()
        for folder in (
            self.dispatcher.root,
            self.dispatcher.outbox,
            self.service.action_root / "results",
        ):
            (folder / (str(uuid4()) + ".json")).write_text("[]")
            (folder / (str(uuid4()) + ".json")).write_text("{broken")
        self.dispatcher.poll_once()
        self.assertTrue(self.result_paths(payload["request_id"])[2].exists())
        self.assertTrue(any(report["status"] == "succeeded" for _, report in self.client.published))
        self.dispatcher._sent.clear()
        count = len(self.client.published)
        self.dispatcher.poll_once()
        self.assertGreater(len(self.client.published), count)

    def test_corrupt_report_id_never_writes_outside_outbox(self):
        payload = signed("secret")
        self.send(payload)
        path = self.dispatcher.root / (payload["request_id"] + ".json")
        record = json.loads(path.read_text())
        record["report"]["report_id"] = "../../outside"
        private_json(path, record)
        self.dispatcher.poll_once()
        self.assertFalse((self.runtime / "outside.json").exists())

    def test_unknown_terminal_keeps_host_evidence_before_receipt(self):
        payload = signed("secret")
        self.send(payload)
        path = self.dispatcher.root / (payload["request_id"] + ".json")
        record = json.loads(path.read_text())
        self.dispatcher._record_report(
            path,
            record,
            {
                "device_id": "edge-01",
                "request_id": payload["request_id"],
                "command": "restart_container",
                "status": "unknown",
            },
        )
        self.run_host()
        self.dispatcher.poll_once()
        host, local, receipt = self.result_paths(payload["request_id"])
        self.assertTrue(receipt.exists())
        self.assertEqual(json.loads(local.read_text()), json.loads(host.read_text()))
        self.assertEqual(json.loads(path.read_text())["report"]["status"], "unknown")

    def test_copy_failure_never_acknowledges_host_result(self):
        payload = signed("secret")
        self.send(payload)
        self.run_host()
        host, local, receipt = self.result_paths(payload["request_id"])
        save = self.dispatcher._save

        def fail_copy(path, value):
            if path == local:
                raise OSError("fault injection")
            save(path, value)

        with patch.object(self.dispatcher, "_save", side_effect=fail_copy):
            self.dispatcher.poll_once()
        self.assertTrue(host.exists())
        self.assertFalse(receipt.exists())
        self.dispatcher.poll_once()
        self.assertTrue(local.exists())
        self.assertTrue(receipt.exists())

    def test_crash_after_ledger_before_outbox_recovers_both_reports(self):
        payload = signed("secret")
        save = self.dispatcher._save

        def fail_outbox(path, value):
            if path.parent == self.dispatcher.outbox:
                raise OSError("fault injection")
            save(path, value)

        with patch.object(self.dispatcher, "_save", side_effect=fail_outbox):
            self.send(payload)
        self.run_host()
        self.build_dispatcher().poll_once()
        reports = [json.loads(path.read_text()) for path in self.dispatcher.outbox.glob("*.json")]
        self.assertEqual({report["status"] for report in reports}, {"accepted", "succeeded"})

    def test_env_restart_result_has_durable_copy_without_api_command(self):
        request_id = str(uuid4())
        result = self.service.submit_action(
            "restart_container", source="admin_env", request_id=request_id
        )
        self.assertTrue(result.accepted)
        self.run_host()
        self.dispatcher.poll_once()
        host, local, receipt = self.result_paths(request_id)
        self.assertEqual(json.loads(host.read_text()), json.loads(local.read_text()))
        self.assertTrue(receipt.exists())
        self.assertFalse((self.dispatcher.root / (request_id + ".json")).exists())
        self.assertEqual(self.client.published, [])

    def test_invalid_generic_result_does_not_get_receipt(self):
        request_id = str(uuid4())
        host, local, receipt = self.result_paths(request_id)
        private_json(
            host,
            {
                "schema_version": 2,
                "request_id": request_id,
                "action": "change_wifi",
                "status": "unexpected",
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )
        self.dispatcher.poll_once()
        self.assertFalse(local.exists())
        self.assertFalse(receipt.exists())

    def test_fsync_failure_after_host_claim_is_uncertain_and_keeps_secret(self):
        request_id = str(uuid4())

        def claim_then_fail(path, value):
            private_json(path, value)
            if path.parent.name == "requests":
                processing = self.service.action_root / "processing" / path.name
                path.rename(processing)
                raise OSError("post-rename failure while host claims")

        with (
            patch.dict(os.environ, {"GN_HOST_ACTION_SECRET_DIR": str(self.secret_dir)}),
            patch("src.services.docker_action_request.atomic_json", side_effect=claim_then_fail),
        ):
            result = self.service.submit_action(
                "change_wifi",
                source="mqtt",
                request_id=request_id,
                parameters={"ssid": "fixture", "wifi_password": "fictional-password"},
            )
        self.assertEqual(result.code, "uncertain")
        self.assertTrue((self.secret_dir / (request_id + ".wifi.json")).exists())
        self.assertTrue((self.service.action_root / "processing" / (request_id + ".json")).exists())

    def test_wifi_64_hex_psk_roundtrips_into_real_host_secret_contract(self):
        request_id = str(uuid4())
        with patch.dict(os.environ, {"GN_HOST_ACTION_SECRET_DIR": str(self.secret_dir)}):
            result = self.service.submit_action(
                "change_wifi",
                source="mqtt",
                request_id=request_id,
                parameters={"ssid": "fixture", "wifi_password": "a1" * 32},
            )
        self.assertTrue(result.accepted)
        request = json.loads(
            (self.service.action_root / "requests" / (request_id + ".json")).read_text()
        )
        self.runner.validate(request)
        secret = self.secret_dir / request["parameters"]["secret_ref"]
        self.assertEqual(json.loads(secret.read_text())["password"], "a1" * 32)
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("a1" * 32, json.dumps(request))

    def test_host_shared_admission_lock_prevents_false_acceptance(self):
        with self.runner.admission():
            result = self.service.submit_action("restart_container", source="mqtt")
        self.assertEqual(result.code, "busy")
        self.assertFalse(list((self.service.action_root / "requests").glob("*.json")))


class EnvRecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_text(SAMPLE_ENV)
        self.client = _make_mock_mqtt()
        self.service = _make_service(self.env, self.client)

    def test_crash_after_env_rename_does_not_publish_conflicting_rejection(self):
        payload = control(content="KEY=changed\n")
        write = self.service._write_env_atomic

        def crash(content):
            write(content)
            raise OSError("post-rename crash")

        with patch.object(self.service, "_write_env_atomic", side_effect=crash):
            self.service._handle_message("env/desired", json.dumps(payload).encode())
        self.assertEqual(self.env.read_text(), "KEY=changed\n")
        self.client.publish_json.assert_not_called()
        self.service._handle_mqtt_connect()
        report = self.client.publish_json.call_args.args[1]
        self.assertEqual(report["status"], "applied_requires_restart")
        self.assertEqual(report["request_id"], payload["request_id"])

    def test_corrupt_env_record_does_not_stop_other_recovery(self):
        self.service.ledger_dir.mkdir()
        (self.service.ledger_dir / (str(uuid4()) + ".json")).write_text("[]")
        payload = control()
        self.service._handle_message("env/desired", json.dumps(payload).encode())
        self.client.reset_mock()
        self.service._handle_mqtt_connect()
        self.assertEqual(
            self.client.publish_json.call_args.args[1]["request_id"], payload["request_id"]
        )


if __name__ == "__main__":
    unittest.main()
