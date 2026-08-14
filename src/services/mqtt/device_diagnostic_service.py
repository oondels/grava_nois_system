from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from src.security.hmac import hmac_sha256_base64
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger

_SIGNATURE_VERSION = "hmac-sha256-v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_diagnostic_payload(payload: dict[str, Any]) -> str:
    return ":".join(
        [
            str(payload.get("type") or ""),
            str(payload.get("event_id") or ""),
            str(payload.get("device_id") or ""),
            str(payload.get("client_id") or ""),
            str(payload.get("venue_id") or ""),
            str(payload.get("boot_id") or ""),
            str(payload.get("sequence") or ""),
            str(payload.get("reason_code") or ""),
            str(payload.get("occurred_at") or ""),
        ]
    )


def sign_diagnostic_event_payload(*, payload: dict[str, Any], device_secret: str) -> str:
    return hmac_sha256_base64(device_secret, _canonical_diagnostic_payload(payload))


class DeviceDiagnosticEventService:
    """Publishes signed device lifecycle and MQTT diagnostic events."""

    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        topic: str,
        device_id: str,
        client_id: str | None,
        venue_id: str | None,
        device_secret: str,
        boot_id: str,
        agent_version: str,
    ):
        self.mqtt_client = mqtt_client
        self.topic = topic
        self.device_id = device_id
        self.client_id = client_id
        self.venue_id = venue_id
        self.device_secret = device_secret
        self.boot_id = boot_id
        self.agent_version = agent_version
        self.sequence = 0
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.mqtt_client.add_on_connect_listener(self.publish_mqtt_connected)
        self.mqtt_client.add_on_disconnect_listener(self.publish_mqtt_disconnected)

    def publish_boot(self) -> None:
        self.publish_event(
            "device.boot",
            severity="info",
            reason_code="process_started",
        )

    def publish_shutdown_clean(self) -> None:
        self.publish_event(
            "device.shutdown_clean",
            severity="info",
            reason_code="process_shutdown",
        )

    def publish_mqtt_connected(self) -> None:
        self.publish_event(
            "mqtt.connected",
            severity="info",
            reason_code="mqtt_connected",
        )
        if self.sequence == 1:
            self.publish_boot()

    def publish_mqtt_disconnected(self, reason: str) -> None:
        self.publish_event(
            "mqtt.disconnected",
            severity="warning",
            reason_code=reason,
        )

    def publish_network_probe_failed(self, reason_code: str, details: dict[str, Any] | None = None) -> None:
        self.publish_event(
            "network.probe_failed",
            severity="warning",
            reason_code=reason_code,
            details=details,
        )

    def publish_api_probe_failed(self, reason_code: str, details: dict[str, Any] | None = None) -> None:
        self.publish_event(
            "api.probe_failed",
            severity="warning",
            reason_code=reason_code,
            details=details,
        )

    def publish_event(
        self,
        event_type: str,
        *,
        severity: str,
        reason_code: str,
        details: dict[str, Any] | None = None,
    ) -> bool:
        self.sequence += 1
        payload: dict[str, Any] = {
            "type": event_type,
            "event_id": str(uuid.uuid4()),
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "occurred_at": _now_iso(),
            "severity": severity,
            "reason_code": reason_code,
            "agent_version": self.agent_version,
            "signature_version": _SIGNATURE_VERSION,
        }
        if details:
            payload["details"] = _sanitize_details(details)
        if self.device_secret:
            payload["signature"] = sign_diagnostic_event_payload(
                payload=payload,
                device_secret=self.device_secret,
            )
        published = self.mqtt_client.publish_json(self.topic, payload, retain=False)
        if not published:
            mqtt_logger.debug("Diagnóstico MQTT não publicado sem conexão ativa: type=%s", event_type)
        return published


def _sanitize_details(details: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if any(token in key.lower() for token in ("secret", "password", "token", "signature")):
            continue
        if isinstance(value, str):
            safe[key] = value.replace("rtsp://", "rtsp://***")
        else:
            safe[key] = value
    return safe
