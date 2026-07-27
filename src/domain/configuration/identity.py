"""Immutable device identity, kept separate from operational configuration."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    client_id: str
    venue_id: str

    @property
    def is_identified(self) -> bool:
        return bool(self.device_id)
