from __future__ import annotations

import heapq
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.infrastructure.filesystem.deferred_repository import atomic_json
from src.security.hmac import hmac_sha256_base64
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger

_SIGNATURE_VERSION = "hmac-sha256-v1"
_EVENT_TYPE_TRIGGER_REJECTED = "capture.trigger_rejected"
_EVENT_TYPE_CAMERA_RECONNECTING = "camera.reconnecting"
_EVENT_TYPE_CAMERA_RECONNECTED = "camera.reconnected"
_EVENT_TYPE_CAMERA_RESTART_FAILED = "camera.restart_failed"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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

    OUTBOX_LIMIT = 64 * 1024 * 1024
    CRITICAL_RESERVE = 8 * 1024 * 1024
    PUBLISH_INTERVAL_SEC = 1.0
    REPEAT_INTERVAL_SEC = 300.0

    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        topic: str,
        device_id: str,
        client_id: str | None,
        venue_id: str | None,
        device_secret: str,
        agent_version: str,
        outbox_dir: Path,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.mqtt_client = mqtt_client
        self.topic = topic
        self.device_id = device_id
        self.client_id = client_id
        self.venue_id = venue_id
        self.device_secret = device_secret
        self.agent_version = agent_version
        self.outbox_dir = outbox_dir
        self.status_path = outbox_dir / "status"
        self._lock = threading.RLock()
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: list[tuple[float, str, Path]] = []
        self._sizes: dict[Path, int] = {}
        self._used_bytes = 0
        self._indexed = False
        self._inflight: tuple[Path, Any] | None = None
        self._next_publish_at = 0.0
        self._recent: dict[tuple[str, str], tuple[str, float]] = {}
        try:
            self._status = json.loads(self.status_path.read_text(encoding="utf-8"))
            if not isinstance(self._status, dict):
                raise ValueError("invalid outbox status")
            if type(self._status.get("suppressed")) is not int or self._status["suppressed"] < 0:
                raise ValueError("invalid suppression counter")
        except (OSError, ValueError):
            self._status = {"suppressed": 0, "saturated": False}
        if type(self._status.get("coalesced")) is not int or self._status["coalesced"] < 0:
            self._status["coalesced"] = 0

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

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._sender_loop,
                daemon=True,
                name="capture-event-outbox",
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _sender_loop(self) -> None:
        while not self._stop.wait(0.25):
            try:
                self.flush_outbox()
            except Exception as exc:
                mqtt_logger.warning("Capture outbox retry deferred: %s", type(exc).__name__)

    @staticmethod
    def _event_time(payload: dict[str, Any], path: Path) -> float:
        try:
            return datetime.fromisoformat(payload["occurred_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError, AttributeError):
            return path.stat().st_mtime

    def _index_outbox(self) -> None:
        """Read the legacy UUID files once, ordered by occurrence instead of UUID."""
        if self._indexed:
            return
        queue = []
        sizes = {}
        for path in self.outbox_dir.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("invalid event")
            except (ValueError, UnicodeError):
                mqtt_logger.warning("Capture event outbox inválido removido: %s", path.name)
                path.unlink(missing_ok=True)
                continue
            sizes[path] = path.stat().st_size
            queue.append((self._event_time(payload, path), path.name, path))
        heapq.heapify(queue)
        self._queue, self._sizes = queue, sizes
        self._used_bytes = sum(sizes.values())
        self._indexed = True

    def flush_outbox(self) -> None:
        """One nonblocking sender tick; at most one QoS 1 message awaits PUBACK."""
        with self._lock:
            if self._inflight is not None:
                path, receipt = self._inflight
                if not receipt.is_published():
                    return  # Paho owns reconnection/redelivery of this same message.
                path.unlink(missing_ok=True)
                self._used_bytes -= self._sizes.pop(path, 0)
                self._inflight = None
                if (
                    self._status.get("saturated")
                    and self._used_bytes < self.OUTBOX_LIMIT - self.CRITICAL_RESERVE
                ):
                    self._status["saturated"] = False
                    atomic_json(self.status_path, self._status)
            if not self.mqtt_client.is_connected or self._clock() < self._next_publish_at:
                return
            self._index_outbox()
            if not self._queue:
                return
            entry = self._queue[0]
            path = entry[2]
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("invalid event")
            except (ValueError, UnicodeError, FileNotFoundError):
                path.unlink(missing_ok=True)
                heapq.heappop(self._queue)
                self._used_bytes -= self._sizes.pop(path, 0)
                return
            self._next_publish_at = self._clock() + self.PUBLISH_INTERVAL_SEC
            receipt = self.mqtt_client.publish_json_tracked(
                self.topic, payload, retain=False, qos=1
            )
            if receipt is not None:
                heapq.heappop(self._queue)
                self._inflight = (path, receipt)
                mqtt_logger.debug(
                    "Capture event queued for MQTT acknowledgement: type=%s camera=%s",
                    payload.get("type"),
                    payload.get("camera_id"),
                )

    def _publish_or_store(self, payload: dict[str, Any]) -> None:
        # Producers never drain backlog or perform network I/O.
        with self._lock:
            event_type = payload.get("type")
            camera = str(payload.get("camera_id") or "")
            key = (camera, str(event_type))
            now = self._clock()
            if event_type == _EVENT_TYPE_CAMERA_RECONNECTED:
                self._recent.pop((camera, _EVENT_TYPE_CAMERA_RECONNECTING), None)
                self._recent.pop((camera, _EVENT_TYPE_CAMERA_RESTART_FAILED), None)
            if event_type in {_EVENT_TYPE_CAMERA_RECONNECTING, _EVENT_TYPE_CAMERA_RESTART_FAILED}:
                previous = self._recent.get(key)
                if (
                    previous
                    and previous[0] == payload.get("reason")
                    and now - previous[1] < self.REPEAT_INTERVAL_SEC
                ):
                    self._status["coalesced"] = int(self._status.get("coalesced", 0)) + 1
                    try:
                        atomic_json(self.status_path, self._status)
                    except OSError:
                        mqtt_logger.warning("Capture outbox suppression counter could not be saved")
                    return
            if self._store(payload) and event_type in {
                _EVENT_TYPE_CAMERA_RECONNECTING,
                _EVENT_TYPE_CAMERA_RESTART_FAILED,
            }:
                self._recent[key] = (str(payload.get("reason") or ""), now)

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

    def _store(self, payload: dict[str, Any]) -> bool:
        try:
            with self._lock:
                self._index_outbox()
                self.outbox_dir.mkdir(parents=True, exist_ok=True)
                size = len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")) + 1
                limit = (
                    self.OUTBOX_LIMIT
                    if payload.get("severity") == "error"
                    or payload.get("type") == _EVENT_TYPE_CAMERA_RECONNECTED
                    else self.OUTBOX_LIMIT - self.CRITICAL_RESERVE
                )
                if self._used_bytes + size > limit:
                    self._status["suppressed"] = int(self._status.get("suppressed", 0)) + 1
                    was_saturated = self._status.get("saturated")
                    self._status["saturated"] = True
                    self._status["last_saturated_at"] = _now_iso()
                    atomic_json(self.status_path, self._status)
                    if not was_saturated:
                        mqtt_logger.error("Capture event outbox saturated; event suppressed")
                    return False
                path = self.outbox_dir / f"{payload['event_id']}.json"
                atomic_json(path, payload)
                actual_size = path.stat().st_size
                self._sizes[path] = actual_size
                self._used_bytes += actual_size
                heapq.heappush(self._queue, (self._event_time(payload, path), path.name, path))
                return True
        except Exception as exc:
            mqtt_logger.warning(
                "Falha ao armazenar evento de captura no outbox: %s", type(exc).__name__
            )
            return False


CaptureEventPublisher = Callable[..., None]
