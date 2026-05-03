from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.security.hmac import hmac_sha256_base64
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger

_SIGNATURE_VERSION = "hmac-sha256-v1"
_EVENT_TYPE_TRIGGER_REJECTED = "capture.trigger_rejected"
_EVENT_TYPE_CAMERA_RECONNECTING = "camera.reconnecting"
_EVENT_TYPE_CAMERA_RECONNECTED = "camera.reconnected"
_EVENT_TYPE_CAMERA_RESTART_FAILED = "camera.restart_failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_capture_event_payload(payload: dict[str, Any]) -> str:
    return ":".join(
        [
            str(payload.get("type") or ""),
            str(payload.get("event_id") or ""),
            str(payload.get("device_id") or ""),
            str(payload.get("client_id") or ""),
            str(payload.get("venue_id") or ""),
            str(payload.get("camera_id") or ""),
            str(payload.get("trigger_id") or ""),
            str(payload.get("trigger_source") or ""),
            str(payload.get("reason") or ""),
            str(payload.get("occurred_at") or ""),
        ]
    )


def sign_capture_event_payload(*, payload: dict[str, Any], device_secret: str) -> str:
    return hmac_sha256_base64(device_secret, _canonical_capture_event_payload(payload))


class CaptureEventService:
    """Publishes capture operational events and keeps a local outbox for retries."""

    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        topic: str,
        device_id: str,
        client_id: str,
        venue_id: str,
        device_secret: str,
        agent_version: str,
        outbox_dir: Path,
    ):
        self.mqtt_client = mqtt_client
        self.topic = topic
        self.device_id = device_id
        self.client_id = client_id
        self.venue_id = venue_id
        self.device_secret = device_secret
        self.agent_version = agent_version
        self.outbox_dir = outbox_dir

    def publish_trigger_rejected(
        self,
        *,
        camera_id: str,
        trigger_id: str,
        trigger_source: str,
        reason: str,
        camera_status: str,
        ffmpeg_alive: bool,
        buffer_status: str,
        segment_age_sec: float | None,
        last_segment_at: str | None,
    ) -> None:
        payload = {
            "type": _EVENT_TYPE_TRIGGER_REJECTED,
            "event_id": str(uuid.uuid4()),
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "camera_id": camera_id,
            "trigger_id": trigger_id,
            "trigger_source": trigger_source,
            "reason": reason,
            "severity": "warning",
            "occurred_at": _now_iso(),
            "camera_status": camera_status,
            "ffmpeg_alive": ffmpeg_alive,
            "buffer_status": buffer_status,
            "segment_age_sec": segment_age_sec,
            "last_segment_at": last_segment_at,
            "agent_version": self.agent_version,
            "signature_version": _SIGNATURE_VERSION,
        }
        if self.device_secret:
            payload["signature"] = sign_capture_event_payload(
                payload=payload,
                device_secret=self.device_secret,
            )
        self._publish_or_store(payload)

    def publish_camera_reconnecting(
        self,
        *,
        camera_id: str,
        reason: str,
        restart_attempts: int,
        ffmpeg_alive: bool,
        buffer_status: str,
        segment_age_sec: float | None,
        last_segment_at: str | None,
    ) -> None:
        self._publish_camera_runtime_event(
            event_type=_EVENT_TYPE_CAMERA_RECONNECTING,
            camera_id=camera_id,
            reason=reason,
            severity="warning",
            camera_status="RECONNECTING",
            restart_attempts=restart_attempts,
            ffmpeg_alive=ffmpeg_alive,
            buffer_status=buffer_status,
            segment_age_sec=segment_age_sec,
            last_segment_at=last_segment_at,
        )

    def publish_camera_reconnected(
        self,
        *,
        camera_id: str,
        reason: str,
        restart_attempts: int,
    ) -> None:
        self._publish_camera_runtime_event(
            event_type=_EVENT_TYPE_CAMERA_RECONNECTED,
            camera_id=camera_id,
            reason=reason,
            severity="info",
            camera_status="OK",
            restart_attempts=restart_attempts,
            ffmpeg_alive=True,
            buffer_status="FRESH",
            segment_age_sec=None,
            last_segment_at=None,
        )

    def publish_camera_restart_failed(
        self,
        *,
        camera_id: str,
        reason: str,
        restart_attempts: int,
        buffer_status: str,
        segment_age_sec: float | None,
        last_segment_at: str | None,
    ) -> None:
        self._publish_camera_runtime_event(
            event_type=_EVENT_TYPE_CAMERA_RESTART_FAILED,
            camera_id=camera_id,
            reason=reason,
            severity="error",
            camera_status="UNAVAILABLE",
            restart_attempts=restart_attempts,
            ffmpeg_alive=False,
            buffer_status=buffer_status,
            segment_age_sec=segment_age_sec,
            last_segment_at=last_segment_at,
        )

    def flush_outbox(self) -> None:
        for path in sorted(self.outbox_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                mqtt_logger.warning("Capture event outbox inválido removido: %s", path)
                path.unlink(missing_ok=True)
                continue
            if self._publish(payload):
                path.unlink(missing_ok=True)

    def _publish_or_store(self, payload: dict[str, Any]) -> None:
        self.flush_outbox()
        if self._publish(payload):
            return
        self._store(payload)

    def _publish_camera_runtime_event(
        self,
        *,
        event_type: str,
        camera_id: str,
        reason: str,
        severity: str,
        camera_status: str,
        restart_attempts: int,
        ffmpeg_alive: bool,
        buffer_status: str,
        segment_age_sec: float | None,
        last_segment_at: str | None,
    ) -> None:
        payload = {
            "type": event_type,
            "event_id": str(uuid.uuid4()),
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "camera_id": camera_id,
            "reason": reason,
            "severity": severity,
            "occurred_at": _now_iso(),
            "camera_status": camera_status,
            "ffmpeg_alive": ffmpeg_alive,
            "buffer_status": buffer_status,
            "segment_age_sec": segment_age_sec,
            "last_segment_at": last_segment_at,
            "restart_attempts": restart_attempts,
            "agent_version": self.agent_version,
            "signature_version": _SIGNATURE_VERSION,
        }
        if self.device_secret:
            payload["signature"] = sign_capture_event_payload(
                payload=payload,
                device_secret=self.device_secret,
            )
        self._publish_or_store(payload)

    def _publish(self, payload: dict[str, Any]) -> bool:
        published = self.mqtt_client.publish_json(self.topic, payload, retain=False)
        if published:
            mqtt_logger.info(
                "Evento de captura publicado: type=%s camera=%s reason=%s",
                payload.get("type"),
                payload.get("camera_id"),
                payload.get("reason"),
            )
        return published

    def _store(self, payload: dict[str, Any]) -> None:
        try:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            path = self.outbox_dir / f"{payload['event_id']}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            mqtt_logger.warning(
                "Evento de captura armazenado no outbox: %s", path
            )
        except Exception as exc:
            mqtt_logger.warning("Falha ao armazenar evento de captura no outbox: %s", exc)


CaptureEventPublisher = Callable[..., None]
