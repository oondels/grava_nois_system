"""A single immutable input for constructing an application runtime."""

from dataclasses import dataclass

from src.domain.configuration import DeviceIdentity, OperationalConfigSnapshot


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    operational: OperationalConfigSnapshot
    identity: DeviceIdentity
