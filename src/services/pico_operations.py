from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class MaintenanceMode:
    def __init__(self, duration_sec: float = 900.0) -> None:
        self.duration_sec = duration_sec
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            if self._expires_at and time.monotonic() >= self._expires_at:
                self._expires_at = 0.0
            return self._expires_at > 0.0

    def toggle(self) -> bool:
        with self._lock:
            if self._expires_at > time.monotonic():
                self._expires_at = 0.0
                return False
            self._expires_at = time.monotonic() + self.duration_sec
            return True


class ConfirmedActionArm:
    def __init__(
        self,
        window_sec: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_sec = window_sec
        self.clock = clock
        self._armed_until = 0.0
        self._lock = threading.Lock()

    def arm(self, enabled: bool) -> bool:
        with self._lock:
            self._armed_until = self.clock() + self.window_sec if enabled else 0.0
            return enabled

    def consume(self) -> bool:
        with self._lock:
            armed = self._armed_until > 0.0 and self.clock() <= self._armed_until
            self._armed_until = 0.0
            return armed


def camera_state(snapshot: dict[str, Any]) -> str:
    cameras = snapshot.get("cameras") or []
    if not cameras:
        return "ERROR"
    ready = [
        camera
        for camera in cameras
        if camera.get("ffmpeg_alive") and camera.get("buffer_fresh")
    ]
    if len(ready) == len(cameras):
        return "READY"
    if ready:
        return "DEGRADED"
    if any(
        camera.get("camera_status") in {"STARTING", "RECONNECTING"}
        for camera in cameras
    ):
        return "STARTING"
    return "ERROR"


class PicoOperationalMonitor:
    def __init__(
        self,
        *,
        send: Callable[[str], bool],
        snapshot_provider: Callable[[], dict[str, Any]],
        mqtt_enabled: bool,
        mqtt_connected: Callable[[], bool],
        maintenance: MaintenanceMode,
        serial_healthy: Callable[[], bool],
        logger: Any,
    ) -> None:
        self.send = send
        self.snapshot_provider = snapshot_provider
        self.mqtt_enabled = mqtt_enabled
        self.mqtt_connected = mqtt_connected
        self.maintenance = maintenance
        self.serial_healthy = serial_healthy
        self.logger = logger
        self._last: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="pico-operational-monitor",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def publish_feedback(self, category: str, result: str) -> None:
        self.send(f"FEEDBACK:{category}:{result}")

    def run_diagnostic(self) -> bool:
        self.publish_feedback("DIAG", "RUNNING")
        snapshot = self.snapshot_provider()
        if not self.serial_healthy():
            self.publish_feedback("DIAG", "FAIL:pico_no_heartbeat")
            return False
        state = camera_state(snapshot)
        if state != "READY":
            self.publish_feedback("DIAG", f"FAIL:camera_{state.lower()}")
            return False
        self.publish_feedback("DIAG", "OK")
        return True

    def _publish_changed(self, key: str, value: str) -> None:
        if self._last.get(key) == value:
            return
        self._last[key] = value
        self.send(f"STATE:{key}:{value}")

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                snapshot = self.snapshot_provider()
                self._publish_changed("CAMERA", camera_state(snapshot))
                if not self.mqtt_enabled:
                    mqtt_state = "DISABLED"
                else:
                    mqtt_state = "CONNECTED" if self.mqtt_connected() else "DISCONNECTED"
                self._publish_changed("MQTT", mqtt_state)
                cameras = snapshot.get("cameras") or []
                health = snapshot.get("health") or {}
                pending = any(
                    int(camera.get("queue_size") or 0) > 0
                    or bool(camera.get("capture_busy"))
                    for camera in cameras
                ) or int(snapshot.get("queue_size") or 0) > 0 or int(
                    health.get("upload_failed_count") or 0
                ) > 0
                self._publish_changed("UPLOAD", "PENDING" if pending else "IDLE")
                self._publish_changed(
                    "MAINTENANCE", "ON" if self.maintenance.active else "OFF"
                )
            except Exception as exc:
                self.logger.warning("[Pico] Falha ao atualizar estado operacional: %s", exc)
