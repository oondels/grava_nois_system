"""Subprocess-backed implementation of the capture process port."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Protocol


class ProcessHandle(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


SpawnProcess = Callable[..., ProcessHandle]


class SubprocessCaptureProcess:
    """Own one explicitly configured long-running capture command.

    Command construction stays outside the adapter. This makes the resolved
    camera configuration visible at composition time and prevents the adapter
    from reading environment variables or the global configuration loader.
    """

    def __init__(
        self,
        *,
        command: Sequence[str],
        log_path: Path,
        stop_timeout_seconds: float = 5,
        spawn: SpawnProcess = subprocess.Popen,
    ) -> None:
        normalized = tuple(str(part) for part in command)
        if not normalized or not normalized[0].strip():
            raise ValueError("capture command must not be empty")
        if stop_timeout_seconds <= 0:
            raise ValueError("stop_timeout_seconds must be positive")
        self._command = normalized
        self._log_path = Path(log_path)
        self._stop_timeout = stop_timeout_seconds
        self._spawn = spawn
        self._process: ProcessHandle | None = None

    def start(self) -> None:
        if self.is_alive():
            return

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file: IO[bytes] | None = None
        try:
            log_file = self._log_path.open("ab", buffering=0)
            self._process = self._spawn(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            self._process = None
            raise
        finally:
            if log_file is not None:
                log_file.close()

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=self._stop_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self._stop_timeout)

    def shutdown(self) -> None:
        """Expose the process through the bootstrap lifecycle contract."""

        self.stop()

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None
