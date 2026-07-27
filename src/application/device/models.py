from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    client_id: str
    venue_id: str
    agent_version: str
    boot_id: str


@dataclass(frozen=True)
class RuntimeDiagnostics:
    queue_size: int = 0
    health: dict[str, Any] = field(default_factory=dict)
    cameras: list[dict[str, Any]] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> RuntimeDiagnostics:
        return cls(
            queue_size=int(value.get("queue_size", 0)),
            health=value.get("health") or {},
            cameras=value.get("cameras") or [],
            runtime=value.get("runtime") or {},
        )


@dataclass(frozen=True)
class CommandRequest:
    source_topic: str
    command: str
    payload: dict[str, Any]
