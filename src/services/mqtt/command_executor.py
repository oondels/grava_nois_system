from __future__ import annotations

from typing import Any

from src.services.docker_action_request import DockerActionRequestService


class CommandExecutor:
    def __init__(self, action_service: DockerActionRequestService | None = None) -> None:
        self.action_service = action_service or DockerActionRequestService.from_env()

    def execute(self, command_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.action_service.submit_action(
            command_name,
            source="mqtt",
            request_id=payload["request_id"],
            expires_at=payload["expires_at"],
            parameters=payload.get("parameters") or {},
        )
        return {
            "device_id": payload["device_id"],
            "request_id": payload["request_id"],
            "command": command_name,
            "status": "accepted"
            if result.accepted
            else ("unknown" if result.code == "uncertain" else "failed"),
            "error_code": None if result.accepted else "host_" + result.code,
        }
