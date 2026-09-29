from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from src.security.env_control import timestamp


class CommandPolicy:
    allowed_commands = {"restart_container", "reboot_host", "pull_and_recreate", "change_wifi"}

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("GN_REMOTE_DEVICE_COMMANDS_ENABLED", "0").lower()
            in {"1", "true", "yes", "on"}
        )

    def is_allowed(self, command_name: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if not self.enabled:
            return False, "remote commands disabled"
        if command_name not in self.allowed_commands:
            return False, "command not allowed"
        try:
            issued, expires = timestamp(payload["issued_at"]), timestamp(payload["expires_at"])
        except (KeyError, TypeError, ValueError):
            return False, "invalid expiry"
        now = datetime.now(UTC)
        if expires <= now:
            return False, "command expired"
        if issued > now + timedelta(seconds=30) or not timedelta(0) < expires - issued <= timedelta(
            seconds=120
        ):
            return False, "invalid expiry"
        return True, "allowed"
