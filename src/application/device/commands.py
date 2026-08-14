from __future__ import annotations

import json
from typing import Any

from src.application.ports.messaging import JsonMessagePublisher

from .models import CommandRequest


class ValidateDeviceCommand:
    """Phase-one policy: parse commands but reject all remote execution."""

    disabled_reason = "remote commands are not enabled in phase 1"

    def parse(self, source_topic: str, raw_payload: bytes) -> CommandRequest:
        try:
            decoded = json.loads(raw_payload.decode("utf-8"))
            payload = decoded if isinstance(decoded, dict) else {"value": decoded}
        except Exception:
            payload = {"raw": raw_payload.decode("utf-8", errors="ignore")}
        command = str(payload.get("command") or payload.get("type") or "unknown")
        return CommandRequest(source_topic, command, payload)

    def rejection(self, device_id: str, request: CommandRequest) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "command": request.command,
            "status": "rejected",
            "reason": self.disabled_reason,
            "source_topic": request.source_topic,
        }


class HandleDeviceCommand:
    def __init__(
        self,
        publisher: JsonMessagePublisher,
        *,
        device_id: str,
        output_topic: str,
        validator: ValidateDeviceCommand | None = None,
    ) -> None:
        self._publisher = publisher
        self._device_id = device_id
        self._output_topic = output_topic
        self._validator = validator or ValidateDeviceCommand()

    def __call__(self, topic: str, raw_payload: bytes) -> bool:
        request = self._validator.parse(topic, raw_payload)
        response = self._validator.rejection(self._device_id, request)
        return self._publisher.publish_json(self._output_topic, response, retain=False)
