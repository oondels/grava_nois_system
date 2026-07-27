"""Device-management use cases (introduced incrementally during migration)."""

from .commands import HandleDeviceCommand, ValidateDeviceCommand
from .diagnostics import CollectRuntimeDiagnostics
from .models import CommandRequest, DeviceIdentity, RuntimeDiagnostics
from .presence import BuildDevicePresence

__all__ = [
    "BuildDevicePresence",
    "CommandRequest",
    "CollectRuntimeDiagnostics",
    "DeviceIdentity",
    "HandleDeviceCommand",
    "RuntimeDiagnostics",
    "ValidateDeviceCommand",
]
