"""Standard-library task scheduler adapter."""

from collections.abc import Callable
from concurrent.futures import Executor


class ExecutorTaskScheduler:
    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    def submit(self, task: Callable[[], None]) -> None:
        self._executor.submit(task)
