from __future__ import annotations

import json
from typing import Any

from src.services.mqtt.command_executor import CommandExecutor
from src.services.mqtt.command_policy import CommandPolicy
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger


class CommandDispatcher:
    """Receives reserved command messages without enabling real execution."""

    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        device_id: str,
        command_in_topic: str,
        command_out_topic: str,
        policy: CommandPolicy | None = None,
        executor: CommandExecutor | None = None,
    ):
        self.mqtt_client = mqtt_client
        self.device_id = device_id
        self.command_in_topic = command_in_topic
        self.command_out_topic = command_out_topic
        self.policy = policy or CommandPolicy()
        self.executor = executor or CommandExecutor()

    def start(self) -> bool:
        if not self.mqtt_client.is_enabled:
            return False
        return self.mqtt_client.subscribe(self.command_in_topic, self._handle_message)

    def stop(self) -> None:
        return None

    def _handle_message(self, topic: str, raw_payload: bytes) -> None:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except Exception:
            payload = {"raw": raw_payload.decode("utf-8", errors="ignore")}

        command_name = str(payload.get("command") or payload.get("type") or "unknown")
        allowed, reason = self.policy.is_allowed(command_name, payload)
        if allowed:
            response = self.executor.execute(command_name, payload)
        else:
            response = {
                "device_id": self.device_id,
                "command": command_name,
                "status": "rejected",
                "reason": reason,
                "source_topic": topic,
            }

        mqtt_logger.info(
            "Comando remoto bloqueado na fase 1: topic=%s command=%s",
            topic,
            command_name,
        )
        self.mqtt_client.publish_json(self.command_out_topic, response, retain=False)
