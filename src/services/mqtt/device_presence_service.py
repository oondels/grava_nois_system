from __future__ import annotations

import os
import shutil
import socket
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config.settings import MQTTConfig
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger

RuntimeSnapshotProvider = Callable[[], dict[str, Any]]


class DevicePresenceService:
    """Publishes retained presence plus periodic heartbeat/state snapshots."""

    def __init__(
        self,
        mqtt_client: MQTTClient,
        config: MQTTConfig,
        *,
        device_id: str,
        client_id: str,
        venue_id: str,
        runtime_snapshot_provider: RuntimeSnapshotProvider,
    ):
        self.mqtt_client = mqtt_client
        self.config = config
        self.device_id = device_id
        self.client_id = client_id
        self.venue_id = venue_id
        self.runtime_snapshot_provider = runtime_snapshot_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._presence_topic = self.config.topic_for(self.device_id, "presence")
        self._heartbeat_topic = self.config.topic_for(self.device_id, "heartbeat")
        self._state_topic = self.config.topic_for(self.device_id, "state")

    @property
    def presence_topic(self) -> str:
        return self._presence_topic

    @property
    def heartbeat_topic(self) -> str:
        return self._heartbeat_topic

    @property
    def state_topic(self) -> str:
        return self._state_topic

    def start(self) -> bool:
        self.mqtt_client.add_on_connect_listener(self.publish_online)
        self.mqtt_client.configure_last_will(
            self._presence_topic,
            self.build_presence_payload(status="offline", disconnect_reason="broker_disconnect"),
            retain=self.config.retain_presence,
        )
        if not self.mqtt_client.start():
            return False
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.publish_offline(disconnect_reason="clean_shutdown")
        self.mqtt_client.stop()

    def publish_online(self) -> None:
        self.mqtt_client.publish_json(
            self._presence_topic,
            self.build_presence_payload(status="online"),
            retain=self.config.retain_presence,
        )
        self.publish_state()

    def publish_offline(self, *, disconnect_reason: str) -> None:
        self.mqtt_client.publish_json(
            self._presence_topic,
            self.build_presence_payload(
                status="offline",
                disconnect_reason=disconnect_reason,
            ),
            retain=self.config.retain_presence,
        )

    def publish_state(self) -> None:
        self.mqtt_client.publish_json(
            self._state_topic,
            self.build_state_payload(),
            retain=False,
        )

    def publish_heartbeat(self) -> None:
        payload = self.build_presence_payload(status="online")
        self.mqtt_client.publish_json(self._heartbeat_topic, payload, retain=False)

    def _safe_snapshot(self) -> dict[str, Any]:
        try:
            return self.runtime_snapshot_provider()
        except Exception:
            mqtt_logger.exception("Falha ao obter runtime snapshot; usando fallback vazio")
            return {"queue_size": 0, "health": {}, "cameras": [], "runtime": {}}

    def build_presence_payload(
        self,
        *,
        status: str,
        disconnect_reason: str | None = None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        snapshot = self._safe_snapshot()
        payload = {
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "status": status,
            "agent_version": self.config.agent_version,
            "timestamp": now,
            "last_seen": now,
            "queue_size": snapshot.get("queue_size", 0),
            "hostname": socket.gethostname(),
        }
        if disconnect_reason:
            payload["disconnect_reason"] = disconnect_reason
        health = snapshot.get("health")
        if health is not None:
            payload["health"] = health
        return payload

    def build_state_payload(self) -> dict[str, Any]:
        now = self._now_iso()
        snapshot = self._safe_snapshot()
        return {
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "status": "online",
            "agent_version": self.config.agent_version,
            "timestamp": now,
            "last_seen": now,
            "queue_size": snapshot.get("queue_size", 0),
            "health": snapshot.get("health", {}),
            "cameras": snapshot.get("cameras", []),
            "runtime": snapshot.get("runtime", {}),
        }

    def _heartbeat_loop(self) -> None:
        interval = max(5, self.config.heartbeat_interval_sec)
        mqtt_logger.info("Heartbeat MQTT iniciado com intervalo de %ss", interval)
        while not self._stop.wait(interval):
            try:
                self.publish_heartbeat()
                self.publish_state()
            except Exception:
                mqtt_logger.exception("Erro no ciclo de heartbeat MQTT")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


def build_runtime_snapshot(
    *,
    runtimes: list[Any],
    light_mode: bool,
    dev_mode: bool,
    trigger_source: str,
    camera_stale_after_sec: float = 10.0,
) -> dict[str, Any]:
    cameras: list[dict[str, Any]] = []
    total_queue_size = 0

    for runtime in runtimes:
        try:
            queue_size = sum(1 for _ in runtime.cfg.queue_dir.glob("*.mp4"))
        except Exception:
            queue_size = 0
        total_queue_size += queue_size
        ffmpeg_alive = (
            runtime.proc is not None and runtime.proc.poll() is None
        )
        diagnostics = None
        if getattr(runtime, "segbuf", None) is not None:
            try:
                diagnostics = runtime.segbuf.diagnostics(
                    stale_after_sec=camera_stale_after_sec
                )
            except Exception:
                diagnostics = None
        buffer_status = (
            diagnostics.buffer_status
            if diagnostics is not None
            else ("NO_BUFFER" if getattr(runtime, "segbuf", None) is None else "UNKNOWN")
        )
        buffer_fresh = diagnostics.buffer_fresh if diagnostics is not None else False
        effective_camera_status = (
            "OK"
            if ffmpeg_alive and buffer_fresh
            else getattr(runtime, "camera_status", "UNKNOWN")
        )
        if not ffmpeg_alive or not buffer_fresh:
            effective_camera_status = "UNAVAILABLE"
        cameras.append(
            {
                "camera_id": runtime.cfg.camera_id,
                "camera_name": runtime.cfg.camera_name,
                "source_type": runtime.cfg.source_type,
                "queue_size": queue_size,
                "capture_busy": runtime.capture_lock.locked(),
                "ffmpeg_alive": ffmpeg_alive,
                "camera_status": effective_camera_status,
                "last_error": getattr(runtime, "last_error", ""),
                "last_error_at": getattr(runtime, "last_error_at", ""),
                "restart_attempts": getattr(runtime, "restart_attempts", 0),
                "buffer_status": buffer_status,
                "buffer_fresh": buffer_fresh,
                "segment_age_sec": diagnostics.segment_age_sec if diagnostics else None,
                "last_segment_at": diagnostics.last_segment_at if diagnostics else None,
                "buffer_segment_count": diagnostics.segment_count if diagnostics else 0,
            }
        )

    # --- Métricas de fila e armazenamento ---
    base_dir = Path(__file__).resolve().parent.parent.parent.parent
    failed_clips_count = 0
    upload_failed_count = 0
    try:
        failed_dir = base_dir / "failed_clips"
        if failed_dir.is_dir():
            failed_clips_count = sum(1 for _ in failed_dir.glob("*.mp4"))
        upload_failed_dir = failed_dir / "upload_failed"
        if upload_failed_dir.is_dir():
            upload_failed_count = sum(1 for _ in upload_failed_dir.glob("*.mp4"))
    except Exception:
        pass

    disk_free_bytes = 0
    disk_total_bytes = 0
    storage_status = "UNKNOWN"
    try:
        usage = shutil.disk_usage(base_dir)
        disk_free_bytes = usage.free
        disk_total_bytes = usage.total
        if disk_free_bytes < 200_000_000:
            storage_status = "CRITICAL"
        elif disk_free_bytes < 1_000_000_000:
            storage_status = "LOW_SPACE"
        else:
            storage_status = "OK"
    except Exception:
        pass

    return {
        "queue_size": total_queue_size,
        "health": {
            "camera_count": len(runtimes),
            "online_cameras": sum(
                1 for cam in cameras if cam["ffmpeg_alive"] and cam["buffer_fresh"]
            ),
            "trigger_source": trigger_source,
            "failed_clips_count": failed_clips_count,
            "upload_failed_count": upload_failed_count,
            "disk_free_bytes": disk_free_bytes,
            "disk_total_bytes": disk_total_bytes,
            "storage_status": storage_status,
        },
        "cameras": cameras,
        "runtime": {
            "light_mode": light_mode,
            "dev_mode": dev_mode,
            "mqtt_enabled": os.getenv("GN_MQTT_ENABLED", "0"),
        },
    }
