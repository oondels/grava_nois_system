"""Filesystem admission budgets; 4 GB is an alert, not a blanket stop."""

import shutil
import threading
from pathlib import Path


class StorageBlocked(RuntimeError):
    pass


class StorageMonitor:
    ALERT_BYTES = 4_000_000_000
    RECOVERY_BYTES = 4_500_000_000
    RESERVE_BYTES = 256 * 1024 * 1024

    def __init__(self, paths, events, disk_usage=shutil.disk_usage):
        self.paths = tuple(Path(p) for p in paths)
        self.events = events
        self.usage = disk_usage
        self._lock = threading.RLock()
        self._reservations = {}
        self._alerts = {}
        self.volumes = []

    @staticmethod
    def existing(path):
        path = Path(path)
        while not path.exists():
            if path == path.parent:
                raise StorageBlocked("storage_missing")
            path = path.parent
        return path

    def check(self, path, required=0, owner=None):
        with self._lock:
            directory = self.existing(path)
            volume = directory.stat().st_dev
            reserved = sum(
                size
                for key, (dev, size) in self._reservations.items()
                if dev == volume and key != owner
            )
            if self.usage(directory).free < self.RESERVE_BYTES + reserved + required:
                raise StorageBlocked("insufficient_storage_reserve")

    def reserve(self, owner, path, required):
        with self._lock:
            self.check(path, required, owner)
            volume = self.existing(path).stat().st_dev
            self._reservations[owner] = (volume, required)

    def release(self, owner):
        with self._lock:
            self._reservations.pop(owner, None)

    def poll(self):
        volumes = {}
        for path in self.paths:
            try:
                directory = self.existing(path)
                volume = str(directory.stat().st_dev)
                if volume in volumes:
                    continue
                usage = self.usage(directory)
                opened, recovered = self._alerts.get(volume, (False, 0))
                if usage.free < self.ALERT_BYTES:
                    if not opened:
                        self.events.emit(
                            "storage.low_space",
                            stage="storage",
                            code="below_4gb",
                            incident_id=f"storage:{volume}",
                        )
                    opened, recovered = True, 0
                elif opened and usage.free >= self.RECOVERY_BYTES:
                    recovered += 1
                    if recovered >= 2:
                        self.events.emit(
                            "storage.recovered",
                            stage="storage",
                            code="storage_recovered",
                            severity="info",
                            situation="resolved",
                            incident_id=f"storage:{volume}",
                        )
                        opened, recovered = False, 0
                else:
                    recovered = 0
                self._alerts[volume] = opened, recovered
                volumes[volume] = {
                    "volume_id": volume,
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                    "low_space": opened,
                }
            except OSError:
                self.events.emit(
                    "storage.unavailable",
                    stage="storage",
                    code="storage_unavailable",
                    severity="error",
                )
        self.volumes = list(volumes.values())
        return self.volumes

    @staticmethod
    def memory_safe():
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024 >= 256 * 1024 * 1024
        except OSError:
            return False
        return False
