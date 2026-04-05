from __future__ import annotations

from typing import Any


class CommandExecutor:
    """Placeholder executor kept for future command/control phases."""

    def execute(self, command_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        _ = payload
        return {
            "command": command_name,
            "status": "not_enabled",
            "reason": "remote commands are not enabled in phase 1",
        }
