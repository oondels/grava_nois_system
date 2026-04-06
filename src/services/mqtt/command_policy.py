from __future__ import annotations

from typing import Any


class CommandPolicy:
    """Phase 1 keeps command/control explicitly disabled."""

    disabled_reason = "remote commands are not enabled in phase 1"

    def is_allowed(self, command_name: str, payload: dict[str, Any]) -> tuple[bool, str]:
        _ = command_name, payload
        return False, self.disabled_reason
