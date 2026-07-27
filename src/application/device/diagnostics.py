from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import RuntimeDiagnostics


class CollectRuntimeDiagnostics:
    """Isolates runtime snapshot failures from presence publication."""

    def __init__(self, provider: Callable[[], dict[str, Any]]) -> None:
        self._provider = provider

    def __call__(self) -> RuntimeDiagnostics:
        try:
            return RuntimeDiagnostics.from_mapping(self._provider())
        except Exception:
            return RuntimeDiagnostics()
