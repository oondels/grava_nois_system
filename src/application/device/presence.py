from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import DeviceIdentity, RuntimeDiagnostics


class BuildDevicePresence:
    """Pure application service for the established MQTT wire payloads."""

    def __init__(
        self,
        identity: DeviceIdentity,
        *,
        now_iso: Callable[[], str],
        hostname: Callable[[], str],
    ) -> None:
        self._identity = identity
        self._now_iso = now_iso
        self._hostname = hostname

    def presence(
        self,
        diagnostics: RuntimeDiagnostics,
        *,
        status: str,
        heartbeat_seq: int,
        disconnect_reason: str | None = None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        payload: dict[str, Any] = {
            **self._common(status, heartbeat_seq, now),
            "queue_size": diagnostics.queue_size,
            "hostname": self._hostname(),
        }
        if disconnect_reason:
            payload["disconnect_reason"] = disconnect_reason
        payload["health"] = diagnostics.health
        return payload

    def state(
        self,
        diagnostics: RuntimeDiagnostics,
        *,
        heartbeat_seq: int,
    ) -> dict[str, Any]:
        now = self._now_iso()
        return {
            **self._common("online", heartbeat_seq, now),
            "queue_size": diagnostics.queue_size,
            "health": diagnostics.health,
            "cameras": diagnostics.cameras,
            "runtime": diagnostics.runtime,
        }

    def _common(self, status: str, heartbeat_seq: int, now: str) -> dict[str, Any]:
        return {
            "device_id": self._identity.device_id,
            "client_id": self._identity.client_id,
            "venue_id": self._identity.venue_id,
            "status": status,
            "agent_version": self._identity.agent_version,
            "boot_id": self._identity.boot_id,
            "heartbeat_seq": heartbeat_seq,
            "timestamp": now,
            "last_seen": now,
        }
