from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    client_id: str | None
    venue_id: str | None
    agent_version: str
    boot_id: str
    device_mode: str = "fixed"

    def __post_init__(self) -> None:
        if self.device_mode not in {"fixed", "rental"}:
            raise ValueError("device mode must be fixed or rental")
        if self.device_mode == "fixed" and not self.venue_id:
            raise ValueError("fixed device requires venue id")
        if self.device_mode == "fixed" and not self.client_id:
            raise ValueError("fixed device requires client id")
        if self.device_mode == "rental" and self.venue_id is not None:
            raise ValueError("rental device must not have venue id")
        if self.device_mode == "rental" and self.client_id is not None:
            raise ValueError("rental device must not have client id")


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
