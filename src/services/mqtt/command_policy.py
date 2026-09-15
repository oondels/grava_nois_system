from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Any

class CommandPolicy:
    allowed_commands = {"restart_container", "reboot_host", "pull_and_recreate", "change_wifi"}
    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = enabled if enabled is not None else os.getenv("GN_REMOTE_DEVICE_COMMANDS_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
    def is_allowed(self, command_name: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if not self.enabled: return False, "remote commands disabled"
        if command_name not in self.allowed_commands: return False, "command not allowed"
        try: expires = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError): return False, "invalid expiry"
        if expires <= datetime.now(timezone.utc): return False, "command expired"
        return True, "allowed"
