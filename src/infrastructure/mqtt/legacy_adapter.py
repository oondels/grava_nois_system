from __future__ import annotations

from typing import Any

from src.application.ports.messaging import MessageHandler


class LegacyMqttAdapter:
    """Thin structural adapter; intentionally avoids importing Paho or legacy types."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def publish_json(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        retain: bool = False,
    ) -> bool:
        return bool(self._client.publish_json(topic, payload, retain=retain))

    def subscribe(self, topic: str, handler: MessageHandler) -> bool:
        return bool(self._client.subscribe(topic, handler))
