from __future__ import annotations

import json
import unittest

from src.services.mqtt.command_dispatcher import CommandDispatcher
from src.services.mqtt.command_executor import CommandExecutor
from src.services.mqtt.command_policy import CommandPolicy


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


class CommandPhaseOneTests(unittest.TestCase):
    def test_policy_denies_all_commands(self) -> None:
        policy = CommandPolicy()
        allowed, reason = policy.is_allowed("restart_service", {"command": "restart_service"})
        self.assertFalse(allowed)
        self.assertIn("phase 1", reason)

    def test_executor_is_placeholder(self) -> None:
        executor = CommandExecutor()
        result = executor.execute("restart_service", {"command": "restart_service"})
        self.assertEqual(result["status"], "not_enabled")

    def test_dispatcher_rejects_remote_commands_and_publishes_response(self) -> None:
        client = _FakeMQTTClient()
        dispatcher = CommandDispatcher(
            client,
            device_id="edge-01",
            command_in_topic="grn/devices/edge-01/commands/in",
            command_out_topic="grn/devices/edge-01/commands/out",
        )

        dispatcher.start()
        self.assertEqual(len(client.subscriptions), 1)

        _, handler = client.subscriptions[0]
        handler(
            "grn/devices/edge-01/commands/in",
            json.dumps({"command": "restart_service"}).encode("utf-8"),
        )

        self.assertEqual(len(client.published), 1)
        topic, payload = client.published[0]
        self.assertEqual(topic, "grn/devices/edge-01/commands/out")
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["command"], "restart_service")


if __name__ == "__main__":
    unittest.main()
