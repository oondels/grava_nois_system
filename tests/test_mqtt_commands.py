from __future__ import annotations
import base64, hashlib, hmac, json, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.services.mqtt.command_dispatcher import CommandDispatcher, canonical_json
from src.services.mqtt.command_policy import CommandPolicy

class _FakeMQTTClient:
    is_enabled = True
    def __init__(self): self.subscriptions, self.published = [], []
    def subscribe(self, topic, handler, *, qos=None): self.subscriptions.append((topic, handler)); return True
    def publish_json(self, topic, payload, *, retain=False, qos=None): self.published.append((topic, payload)); return True

class _Executor:
    def __init__(self): self.calls = []
    def execute(self, command, payload):
        self.calls.append((command, payload))
        return {"device_id": payload["device_id"], "request_id": payload["request_id"],
                "command": command, "status": "accepted", "error_code": None}

def signed(secret: str, **overrides):
    payload = {"type": "device.operation.request", "device_id": "edge-01", "request_id": "req-1",
               "command": "restart_container", "issued_at": datetime.now(timezone.utc).isoformat(),
               "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
               "parameters": {}, "signature_version": "hmac-sha256-v1", **overrides}
    digest = hmac.new(secret.encode(), canonical_json(payload).encode(), hashlib.sha256).digest()
    payload["signature"] = base64.b64encode(digest).decode()
    return payload

class CommandTests(unittest.TestCase):
    def test_policy_is_disabled_by_default(self):
        allowed, reason = CommandPolicy(enabled=False).is_allowed("restart_container", signed("x"))
        self.assertFalse(allowed); self.assertEqual(reason, "remote commands disabled")

    def test_valid_command_is_executed_once_and_signed(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, executor = _FakeMQTTClient(), _Executor()
            dispatcher = CommandDispatcher(client, device_id="edge-01", device_secret="secret",
                command_in_topic="in", command_out_topic="out", policy=CommandPolicy(enabled=True),
                executor=executor, ledger_path=Path(tmp) / "ledger.json", result_path=Path(tmp) / "last.json")
            dispatcher.start(); handler = client.subscriptions[0][1]
            raw = json.dumps(signed("secret")).encode()
            handler("in", raw); handler("in", raw)
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(client.published[0][1]["status"], "accepted")
            self.assertTrue(client.published[0][1]["signature"])
            dispatcher.stop()

    def test_invalid_signature_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeMQTTClient()
            dispatcher = CommandDispatcher(client, device_id="edge-01", device_secret="secret",
                command_in_topic="in", command_out_topic="out", policy=CommandPolicy(enabled=True),
                ledger_path=Path(tmp) / "ledger.json", result_path=Path(tmp) / "last.json")
            dispatcher.start()
            client.subscriptions[0][1]("in", json.dumps(signed("wrong")).encode())
            self.assertEqual(client.published, [])
            dispatcher.stop()

if __name__ == "__main__": unittest.main()
