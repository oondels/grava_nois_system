"""Small deterministic capabilities used by application services."""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_id(self) -> str: ...


class TaskScheduler(Protocol):
    def submit(self, task: Callable[[], None]) -> None: ...
