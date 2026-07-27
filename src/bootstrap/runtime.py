"""Lifecycle orchestration for the edge process.

This module deliberately knows nothing about cameras, MQTT, FFmpeg, or worker
threads. Infrastructure components opt into the small ``LifecycleComponent``
contract and the composition root supplies them in dependency order.
"""

from __future__ import annotations

from enum import Enum
from threading import RLock
from typing import Protocol


class LifecycleComponent(Protocol):
    """A resource owned by the process runtime."""

    def start(self) -> None: ...

    def shutdown(self) -> None: ...


class RuntimeState(str, Enum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class RuntimeStartupError(RuntimeError):
    """Raised after a partial startup has been rolled back."""


class EdgeRuntime:
    """Start and stop explicitly owned resources in a deterministic order."""

    def __init__(self, components: tuple[LifecycleComponent, ...] = ()) -> None:
        self._components = components
        self._started: list[LifecycleComponent] = []
        self._state = RuntimeState.NEW
        self._lock = RLock()

    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    def start(self) -> None:
        with self._lock:
            if self._state is RuntimeState.RUNNING:
                return
            if self._state is not RuntimeState.NEW:
                raise RuntimeError(f"runtime cannot start from state {self._state.value}")
            self._state = RuntimeState.STARTING
            try:
                for component in self._components:
                    component.start()
                    self._started.append(component)
            except Exception as exc:
                self._shutdown_started()
                self._state = RuntimeState.FAILED
                raise RuntimeStartupError("edge runtime startup failed") from exc
            self._state = RuntimeState.RUNNING

    def shutdown(self) -> None:
        with self._lock:
            if self._state in {RuntimeState.STOPPED, RuntimeState.FAILED}:
                return
            if self._state is RuntimeState.NEW:
                self._state = RuntimeState.STOPPED
                return
            self._state = RuntimeState.STOPPING
            errors = self._shutdown_started()
            self._state = RuntimeState.STOPPED
            if errors:
                raise ExceptionGroup("edge runtime shutdown failed", errors)

    def __enter__(self) -> EdgeRuntime:
        self.start()
        return self

    def __exit__(self, *_error: object) -> None:
        self.shutdown()

    def _shutdown_started(self) -> list[Exception]:
        errors: list[Exception] = []
        while self._started:
            component = self._started.pop()
            try:
                component.shutdown()
            except Exception as exc:
                errors.append(exc)
        return errors
