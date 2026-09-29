"""Local liveness/readiness evidence written by the actual main loop, not a watchdog."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from src.security.durable_state import private_json


def health_path(base: Path | None = None) -> Path:
    root = Path(
        os.getenv(
            "GN_RUNTIME_CONFIG_DIR",
            str((base or Path(__file__).resolve().parents[2]) / "runtime_config"),
        )
    )
    return root / "runtime-health.json"


def process_start(pid: int) -> str:
    # comm may contain spaces/parentheses; fields after the final ')' start at 3.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class RuntimeHealth:
    def __init__(self, base: Path, boot_id: str, camera_stale_after_sec: float = 10.0):
        self.path = health_path(base)
        self.boot_id = boot_id
        self.camera_stale_after_sec = camera_stale_after_sec
        self._last = float("-inf")
        self.pid = os.getpid()
        self.process_start = process_start(self.pid)

    def tick(self, runtimes: list[Any], *, stopped: bool = False) -> None:
        now = time.monotonic()
        if not stopped and now - self._last < 5:
            return
        self._last = now
        ready = 0
        for runtime in runtimes:
            try:
                if (
                    runtime.proc is not None
                    and runtime.proc.poll() is None
                    and runtime.segbuf is not None
                    and runtime.segbuf.diagnostics(
                        stale_after_sec=self.camera_stale_after_sec
                    ).buffer_fresh
                ):
                    ready += 1
            except Exception:
                pass
        private_json(
            self.path,
            {
                "schema_version": 1,
                "pid": self.pid,
                "process_start": self.process_start,
                "boot_id": self.boot_id,
                "updated_monotonic": now,
                "stopped": stopped,
                "cameras_expected": len(runtimes),
                "cameras_ready": ready,
            },
        )


def healthy(path: Path | None = None, *, require_cameras: bool = False) -> bool:
    try:
        value = json.loads((path or health_path()).read_text())
        if value.get("schema_version") != 1 or value.get("stopped") is not False:
            return False
        pid = value["pid"]
        if type(pid) is not int or pid <= 0 or value.get("process_start") != process_start(pid):
            return False
        age = time.monotonic() - value["updated_monotonic"]
        if not 0 <= age <= 20:
            return False
        if require_cameras:
            return (
                type(value.get("cameras_expected")) is int
                and type(value.get("cameras_ready")) is int
                and value["cameras_expected"] >= 0
                and value["cameras_ready"] == value["cameras_expected"]
            )
        return True
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False
